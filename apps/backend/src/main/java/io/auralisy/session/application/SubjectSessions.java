package io.auralisy.session.application;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import io.auralisy.contracts.MessageDefinitions;
import io.auralisy.discovery.api.SessionExecutions;
import io.auralisy.persistence.PersistenceRejected;
import io.auralisy.persistence.ShortTransactions;
import io.auralisy.publication.api.RecoveryProjection;
import io.auralisy.session.api.SessionStore;
import io.auralisy.session.api.SubjectSession;
import io.auralisy.session.domain.SessionPolicy;
import io.auralisy.session.domain.SubjectCredentials;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.HashSet;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.springframework.stereotype.Service;

@Service
public class SubjectSessions implements SubjectSession {
    private static final Duration RETENTION = Duration.ofDays(30);
    private final SubjectRepository subjects;
    private final SessionStore sessions;
    private final SessionExecutions executions;
    private final RecoveryProjection projection;
    private final ShortTransactions tx;
    private final SubjectCredentials credentials;
    private final Clock clock;
    private final ObjectMapper json;
    private final MessageDefinitions protocol;
    private final SessionPolicy properties;
    private final java.util.List<io.auralisy.session.api.SessionDerivation> derivations;

    public SubjectSessions(
            SubjectRepository subjects,
            SessionStore sessions,
            SessionExecutions executions,
            RecoveryProjection projection,
            ShortTransactions tx,
            SubjectCredentials credentials,
            Clock clock,
            ObjectMapper json,
            MessageDefinitions protocol,
            SessionPolicy properties,
            java.util.List<io.auralisy.session.api.SessionDerivation> derivations) {
        this.subjects = subjects;
        this.sessions = sessions;
        this.executions = executions;
        this.projection = projection;
        this.tx = tx;
        this.credentials = credentials;
        this.clock = clock;
        this.json = json;
        this.protocol = protocol;
        this.properties = properties;
        this.derivations = List.copyOf(derivations);
    }

    private Optional<SubjectRepository.Subject> lookup(String token, boolean lock) {
        if (!credentials.valid(token)) return Optional.empty();
        return subjects.find(
                SubjectCredentials.digest(token), credentials.generation(), clock.instant(), lock);
    }

    private SubjectRepository.Subject require(String token, boolean lock) {
        return lookup(token, lock).orElseThrow(() -> new PersistenceRejected("UNAUTHENTICATED"));
    }

    @Override
    public Optional<Identity> authenticate(String token) {
        return tx.read(
                () ->
                        lookup(token, false)
                                .map(s -> new Identity(s.id(), s.epoch(), s.expiresAt())));
    }

    @Override
    public boolean validCsrf(String token, String csrf) {
        return credentials.valid(token) && SubjectCredentials.same(credentials.csrf(token), csrf);
    }

    private void csrf(String token, String csrf) {
        if (!validCsrf(token, csrf)) throw new PersistenceRejected("FORBIDDEN");
    }

    private ObjectNode parse(String value, String definition) {
        if (value == null) throw new PersistenceRejected("INVALID_PAYLOAD");
        try {
            JsonNode node =
                    json.reader()
                            .with(
                                    com.fasterxml.jackson.databind.DeserializationFeature
                                            .FAIL_ON_TRAILING_TOKENS)
                            .readTree(value);
            if (!node.isObject() || !protocol.validDefinition(definition, node))
                throw new PersistenceRejected("INVALID_PAYLOAD");
            return (ObjectNode) node;
        } catch (java.io.IOException e) {
            throw new PersistenceRejected("INVALID_PAYLOAD");
        }
    }

    private JsonNode canonical(JsonNode node) {
        if (node.isObject()) {
            var result = json.createObjectNode();
            var keys = new java.util.TreeSet<String>();
            node.fieldNames().forEachRemaining(keys::add);
            for (String key : keys) result.set(key, canonical(node.get(key)));
            return result;
        }
        if (node.isArray()) {
            var result = json.createArrayNode();
            node.forEach(item -> result.add(canonical(item)));
            return result;
        }
        return node;
    }

    private ObjectNode empty(Instant expiry, String message) {
        ObjectNode state = json.createObjectNode();
        state.putNull("session_id");
        state.putNull("round_id");
        state.putNull("run_id");
        state.put("revision", 0);
        state.put("status", "IDLE");
        state.putNull("analysis_mode");
        state.put("recommendation_target_count", properties.recommendationTargetCount());
        state.putArray("slots");
        state.put("supply_state", "CLOSED");
        if (message == null) state.putNull("message_key");
        else state.put("message_key", message);
        state.putArray("input_issues");
        state.putNull("editable_input");
        state.put("expires_at", expiry.toString());
        state.put("restored", false);
        state.putNull("admission");
        return state;
    }

    private SubjectRepository.Session owned(SubjectRepository.Subject subject, String id) {
        if (id == null || !id.equals(subject.currentSession()))
            throw new PersistenceRejected("RESOURCE_UNAVAILABLE");
        var session =
                subjects.session(subject.id(), id)
                        .orElseThrow(() -> new PersistenceRejected("RESOURCE_UNAVAILABLE"));
        if (!session.status().equals("ACTIVE") || !session.expiresAt().isAfter(clock.instant()))
            throw new PersistenceRejected("RESOURCE_UNAVAILABLE");
        return session;
    }

    private ObjectNode render(SubjectRepository.Subject subject, String sessionId) {
        return rendered(subject, sessionId, true).state();
    }

    private record Rendered(SessionStore.Scope scope, ObjectNode state) {}

    private Rendered rendered(SubjectRepository.Subject subject, String sessionId, boolean live) {
        if (sessionId == null) return new Rendered(null, empty(subject.expiresAt(), null));
        var session = owned(subject, sessionId);
        var scope = session.scope(subject.epoch());
        var state =
                parse(
                        (live ? projection.read(scope) : projection.readStored(scope))
                                .orElseThrow(
                                        () -> new PersistenceRejected("DEPENDENCY_UNAVAILABLE")),
                        "ProductState");
        if (!session.id().equals(state.path("session_id").asText())
                || state.path("revision").asLong() != scope.revision())
            throw new PersistenceRejected("DEPENDENCY_UNAVAILABLE");
        if (session.input() == null) throw new PersistenceRejected("DEPENDENCY_UNAVAILABLE");
        state.set("editable_input", parse(session.input(), "DiscoveryInput"));
        state.put("expires_at", session.expiresAt().toString());
        state.put("restored", true);
        if (state.path("message_key").isNull()) state.put("message_key", "session.restored");
        return new Rendered(scope, state);
    }

    @Override
    public Bootstrap bootstrap(String token) {
        return tx.write(
                () -> {
                    var existing = lookup(token, true);
                    if (existing.isPresent()) {
                        var s = existing.get();
                        return new Bootstrap(
                                token,
                                credentials.csrf(token),
                                s.expiresAt(),
                                render(s, s.currentSession()).toString());
                    }
                    String fresh = credentials.issue(), id = UUID.randomUUID().toString();
                    Instant now = clock.instant(), expiry = now.plus(RETENTION);
                    sessions.createSubject(id, SubjectCredentials.digest(fresh), now, expiry);
                    subjects.generation(id, credentials.generation());
                    return new Bootstrap(
                            fresh,
                            credentials.csrf(fresh),
                            expiry,
                            empty(expiry, token == null ? null : "session.expired").toString());
                });
    }

    @Override
    public String restore(String token, String sessionId) {
        return tx.write(
                () -> {
                    var subject = require(token, true);
                    return render(subject, sessionId == null ? subject.currentSession() : sessionId)
                            .toString();
                });
    }

    @Override
    public Restoration restoreForRender(String token, String sessionId) {
        return tx.write(
                () -> {
                    var subject = require(token, true);
                    var view =
                            rendered(
                                    subject,
                                    sessionId == null ? subject.currentSession() : sessionId,
                                    false);
                    return new Restoration(
                            Optional.ofNullable(view.scope()), view.state().toString());
                });
    }

    @Override
    public Optional<Replacement> replaySubmission(
            String token, String csrf, String key, String expected, long revision, String input) {
        var submission = submission(token, csrf, key, expected, revision, input);
        return tx.write(() -> replay(require(token, true), key, submission.digest()));
    }

    private record Submission(ObjectNode input, String digest) {}

    private Submission submission(
            String token, String csrf, String key, String expected, long revision, String input) {
        csrf(token, csrf);
        if (key == null
                || key.isBlank()
                || key.length() > 160
                || revision < 0
                || revision > Integer.MAX_VALUE) throw new PersistenceRejected("INVALID_PAYLOAD");
        var incoming = parse(input, "DiscoveryInput");
        if (incoming.path("liked_genres").isEmpty()
                && incoming.path("disliked_genre_ids").isEmpty()
                && incoming.path("liked_albums").isEmpty()
                && incoming.path("request_text").asText().isBlank())
            throw new PersistenceRejected("INVALID_PAYLOAD");
        String digest =
                SubjectCredentials.digest(
                        canonical(
                                        json.createObjectNode()
                                                .put("expected_session", expected)
                                                .put("revision", revision)
                                                .set("input", incoming))
                                .toString());
        return new Submission(incoming, digest);
    }

    private Optional<Replacement> replay(
            SubjectRepository.Subject subject, String key, String digest) {
        var receipt = subjects.command(subject.id(), key, digest);
        if (receipt.isEmpty()) return Optional.empty();
        try {
            var r = json.readTree(receipt.get());
            return Optional.of(
                    new Replacement(
                            new SessionStore.Scope(
                                    subject.id(),
                                    r.path("session_id").asText(),
                                    subject.epoch(),
                                    r.path("epoch").asLong(),
                                    r.path("revision").asLong()),
                            r.path("previous").isNull() ? null : r.path("previous").asText(),
                            true));
        } catch (java.io.IOException e) {
            throw new PersistenceRejected("DEPENDENCY_UNAVAILABLE");
        }
    }

    @Override
    public Replacement replace(
            String token,
            String csrf,
            String key,
            String expected,
            long revision,
            String input,
            String resolved) {
        var submission = submission(token, csrf, key, expected, revision, input);
        var incoming = submission.input();
        var canonical = parse(resolved, "RawInput");
        String digest = submission.digest();
        return tx.write(
                () -> {
                    var subject = require(token, true);
                    var receipt = replay(subject, key, digest);
                    if (receipt.isPresent()) return receipt.get();
                    if (!java.util.Objects.equals(subject.currentSession(), expected))
                        throw new PersistenceRejected("REVISION_CONFLICT");
                    if (expected == null && revision != 0)
                        throw new PersistenceRejected("REVISION_CONFLICT");
                    String id = UUID.randomUUID().toString();
                    subjects.lockSessions(
                            subject.id(), expected == null ? List.of(id) : List.of(id, expected));
                    if (expected != null) {
                        var old = owned(subject, expected);
                        if (old.revision() != revision)
                            throw new PersistenceRejected("REVISION_CONFLICT");
                        sessions.close(old.scope(subject.epoch()), "SUPERSEDED");
                        executions.invalidate(List.of(expected), "SUPERSEDED");
                    }
                    Instant now = clock.instant(), expiry = now.plus(RETENTION);
                    long inputRevision = subjects.nextInputRevision(subject.id());
                    sessions.createSession(
                            id, subject.id(), inputRevision, canonical.toString(), now, expiry);
                    subjects.editable(id, incoming.toString());
                    subjects.renew(subject.id(), null, now, expiry);
                    var created = subjects.session(subject.id(), id).orElseThrow();
                    var scope = created.scope(subject.epoch());
                    if (expected != null)
                        for (var derivation : derivations)
                            derivation.derive(expected, scope, canonical.toString());
                    var state = empty(expiry, null);
                    state.put("session_id", id);
                    state.put("revision", scope.revision());
                    state.set("editable_input", incoming);
                    projection.save(scope, state.toString());
                    String result =
                            json.createObjectNode()
                                    .put("session_id", id)
                                    .put("revision", scope.revision())
                                    .put("epoch", scope.sessionEpoch())
                                    .put("previous", expected)
                                    .toString();
                    subjects.command(subject.id(), key, digest, result, now);
                    return new Replacement(scope, expected, false);
                });
    }

    private SubjectRepository.Subject authorize(
            String token, String csrf, SessionStore.Scope scope) {
        csrf(token, csrf);
        var s = require(token, true);
        if (!s.id().equals(scope.subjectId()) || s.epoch() != scope.subjectEpoch())
            throw new PersistenceRejected("RESOURCE_UNAVAILABLE");
        var owned = owned(s, scope.sessionId());
        if (!owned.scope(s.epoch()).equals(scope))
            throw new PersistenceRejected("REVISION_CONFLICT");
        sessions.lockActive(scope);
        return s;
    }

    @Override
    public Instant renew(String token, String csrf, SessionStore.Scope scope) {
        return tx.write(
                () -> {
                    var subject = authorize(token, csrf, scope);
                    var state = render(subject, scope.sessionId());
                    Instant now = clock.instant(), expiry = now.plus(RETENTION);
                    subjects.renew(subject.id(), scope.sessionId(), now, expiry);
                    var updated =
                            subjects.session(subject.id(), scope.sessionId())
                                    .orElseThrow()
                                    .scope(subject.epoch());
                    state.put("revision", updated.revision());
                    state.put("expires_at", expiry.toString());
                    state.put("restored", false);
                    projection.save(updated, state.toString());
                    return expiry;
                });
    }

    @Override
    public void heard(String token, String csrf, SessionStore.Scope scope, UUID verifiedAlbum) {
        tx.write(
                () -> {
                    authorize(token, csrf, scope);
                    sessions.recordHeard(scope, verifiedAlbum, clock.instant());
                    renew(token, csrf, scope);
                    return null;
                });
    }

    @Override
    public void erase(String token, String csrf) {
        csrf(token, csrf);
        tx.write(
                () -> {
                    var existing = lookup(token, true);
                    if (existing.isEmpty())
                        return null; // Valid old credential: repeated erase has no effect.
                    barrier(existing.get(), "REVOKED");
                    return null;
                });
    }

    private void barrier(SubjectRepository.Subject subject, String state) {
        subjects.tombstone(subject, clock.instant());
        if (subjects.barrier(subject, state)) {
            executions.invalidateSubject(
                    subject.id(), state.equals("EXPIRED") ? "EXPIRED" : "CANCELLED");
            subjects.enqueueSubject(subject.id(), subject.epoch() + 1, clock.instant());
        }
    }

    @Override
    public int cleanup(int limit) {
        if (limit < 1 || limit > 100) throw new IllegalArgumentException("INVALID_BATCH_LIMIT");
        Instant now = clock.instant();
        var deferredOwners = new HashSet<String>();
        for (String id : subjects.expiredSubjects(now, credentials.generation(), limit)) {
            try {
                tx.write(
                        () -> {
                            var s = subjects.lockSubject(id);
                            if (s.isPresent()
                                    && s.get().lifecycle().equals("ACTIVE")
                                    && (!s.get().expiresAt().isAfter(now)
                                            || !credentials
                                                    .generation()
                                                    .equals(s.get().generation())))
                                barrier(s.get(), "EXPIRED");
                            return null;
                        });
            } catch (RuntimeException failure) {
                if (!ShortTransactions.isLockContention(failure)) throw failure;
                deferredOwners.add(id);
            }
        }
        for (var candidate : subjects.expiredSessions(now, limit)) {
            if (deferredOwners.contains(candidate.subject())) continue;
            try {
                tx.write(
                        () -> {
                            var subject = subjects.lockSubject(candidate.subject());
                            if (subject.isEmpty()) return null;
                            var s = subjects.session(candidate.subject(), candidate.id());
                            if (s.isPresent()
                                    && !s.get().expiresAt().isAfter(now)
                                    && java.util.Set.of("ACTIVE", "SUPERSEDED")
                                            .contains(s.get().status())) {
                                sessions.close(s.get().scope(subject.get().epoch()), "EXPIRED");
                                executions.invalidate(List.of(s.get().id()), "EXPIRED");
                                subjects.enqueueSession(
                                        subjects.session(candidate.subject(), candidate.id())
                                                .orElseThrow(),
                                        now);
                            }
                            return null;
                        });
            } catch (RuntimeException failure) {
                if (!ShortTransactions.isLockContention(failure)) throw failure;
                deferredOwners.add(candidate.subject());
            }
        }
        int completed = 0;
        for (int n = 0; n < limit; n++) {
            var candidate = subjects.nextJob(now, deferredOwners);
            if (candidate.isEmpty()) break;
            try {
                int result =
                        tx.write(
                                () -> {
                                    // All cleanup writers use owner -> job, matching erase/expiry.
                                    // The unlocked candidate may have been deferred or removed.
                                    var subject = subjects.lockSubject(candidate.get().subjectId());
                                    var next = subjects.lockJob(candidate.get(), now);
                                    if (next.isEmpty()) return 0;
                                    var job = next.get();
                                    if (subject.isEmpty()) {
                                        subjects.completed(job.id());
                                        return 1;
                                    }
                                    if (job.sessionId() == null) {
                                        if (subject.get().lifecycle().equals("ACTIVE")
                                                || subject.get().epoch() != job.epoch()) {
                                            subjects.defer(job.id(), now.plusSeconds(30));
                                            return 0;
                                        }
                                        var first = subjects.firstSession(job.subjectId());
                                        if (first.isPresent()) {
                                            var session = first.get();
                                            if (executions.unreleased(List.of(session.id()))) {
                                                subjects.defer(job.id(), now.plusSeconds(30));
                                                return 0;
                                            }
                                            if (!java.util.Set.of("DELETED", "EXPIRED")
                                                    .contains(session.status()))
                                                sessions.close(
                                                        session.scope(subject.get().epoch()),
                                                        "DELETED");
                                            var latest =
                                                    subjects.session(
                                                                    subject.get().id(),
                                                                    session.id())
                                                            .orElseThrow();
                                            sessions.purgeSession(
                                                    latest.scope(subject.get().epoch()));
                                            return 0;
                                        }
                                        if (subjects.trimSubjectRows(job.subjectId(), 100) > 0)
                                            return 0;
                                    } else if (executions.unreleased(List.of(job.sessionId()))) {
                                        subjects.defer(job.id(), now.plusSeconds(30));
                                        return 0;
                                    }
                                    boolean purged;
                                    if (job.sessionId() == null)
                                        purged =
                                                sessions.purgeSubject(
                                                        subject.get().id(), job.epoch());
                                    else {
                                        var session =
                                                subjects.session(
                                                        subject.get().id(), job.sessionId());
                                        purged =
                                                session.isEmpty()
                                                        || (session.get().epoch() == job.epoch()
                                                                && sessions.purgeSession(
                                                                        session.get()
                                                                                .scope(
                                                                                        subject.get()
                                                                                                .epoch())));
                                    }
                                    if (purged) {
                                        subjects.completed(job.id());
                                        return 1;
                                    }
                                    subjects.defer(job.id(), now.plusSeconds(30));
                                    return 0;
                                });
                completed += result;
            } catch (RuntimeException failure) {
                if (!ShortTransactions.isLockContention(failure)) throw failure;
                // Only skip this owner for this bounded pass. Preserve its original due time.
                deferredOwners.add(candidate.get().subjectId());
            }
        }
        tx.write(
                () -> {
                    subjects.pruneRevocations(now, limit);
                    return null;
                });
        return completed;
    }
}
