package io.auralisy.discovery.adapters;

import static io.auralisy.contracts.CanonicalJson.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import io.auralisy.budget.api.BudgetAuthority;
import io.auralisy.contracts.MessageDefinitions;
import io.auralisy.discovery.api.*;
import io.auralisy.evidence.api.EvidenceAuthority;
import io.auralisy.persistence.*;
import io.auralisy.session.api.SessionStore;
import java.sql.Timestamp;
import java.time.*;
import java.util.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

@Service
public class PostgresDispatchQueue implements DispatchQueue {
    private final JdbcTemplate jdbc;
    private final SessionStore sessions;
    private final DiscoveryStore discovery;
    private final ShortTransactions tx;
    private final MessageDefinitions schema;
    private final BudgetAuthority budget;
    private final Clock clock;
    private final org.springframework.beans.factory.ObjectProvider<ExecutionPreparation>
            preparation;

    public PostgresDispatchQueue(
            JdbcTemplate jdbc,
            SessionStore sessions,
            DiscoveryStore discovery,
            ShortTransactions tx,
            MessageDefinitions schema,
            BudgetAuthority budget,
            Clock clock,
            org.springframework.beans.factory.ObjectProvider<ExecutionPreparation> preparation) {
        this.jdbc = jdbc;
        this.sessions = sessions;
        this.discovery = discovery;
        this.tx = tx;
        this.schema = schema;
        this.budget = budget;
        this.clock = clock;
        this.preparation = preparation;
    }

    private static void fail(String code) {
        throw new PersistenceRejected(code);
    }

    private JsonNode json(String text) {
        try {
            return JSON.readTree(text);
        } catch (java.io.IOException e) {
            throw new PersistenceRejected("INVALID_PAYLOAD");
        }
    }

    private <T> T convert(JsonNode n, Class<T> type) {
        try {
            return JSON.treeToValue(n, type);
        } catch (java.io.IOException e) {
            throw new PersistenceRejected("INVALID_DISPATCH_PLAN");
        }
    }

    private ObjectNode plan(Request r) {
        var start = json(r.template());
        if (!schema.validDefinition("RoundStart", start)
                || !start.path("session_id").asText().equals(r.scope().sessionId())
                || !start.path("round_id").asText().equals(r.round())
                || !start.path("run_kind").asText().equals(r.operation())
                || r.catalogDataset() < 1
                || r.demands().size() > 1000) fail("BINDING_MISMATCH");
        if (!contentDigest(start, "payload_digest").equals(start.path("payload_digest").asText()))
            fail("BINDING_MISMATCH");
        var p = JSON.createObjectNode().put("catalog_dataset", r.catalogDataset());
        p.set("start", start);
        p.set("prices", JSON.valueToTree(r.prices()));
        p.set("candidates", JSON.valueToTree(r.candidates()));
        return p;
    }

    @Override
    public Accepted accept(Request r, Limits limits) {
        var plan = plan(r);
        var intent =
                JSON.createObjectNode()
                        .put("session", r.scope().sessionId())
                        .put("round", r.round())
                        .put("operation", r.operation());
        intent.set("plan", plan);
        intent.set("demands", JSON.valueToTree(r.demands().stream().sorted().toList()));
        String hash = digest(intent);
        return tx.write(
                () -> {
                    discovery.lockRuntime();
                    sessions.lockActive(r.scope());
                    var prior =
                            jdbc.queryForList(
                                    "SELECT run_id,request_digest FROM discovery.dispatch_command WHERE subject_id=? AND operation=? AND command_id=?",
                                    r.scope().subjectId(),
                                    r.operation(),
                                    r.command());
                    if (!prior.isEmpty()) {
                        if (!hash.equals(prior.getFirst().get("request_digest")))
                            fail("IDEMPOTENCY_CONFLICT");
                        return accepted((String) prior.getFirst().get("run_id"));
                    }
                    if (Boolean.TRUE.equals(
                            jdbc.queryForObject(
                                    "SELECT maintenance FROM discovery.runtime_control WHERE singleton",
                                    Boolean.class))) fail("MAINTENANCE");
                    expire(r.scope().subjectId());
                    var existing =
                            jdbc.queryForList(
                                    "SELECT id,status,session_id,round_id,operation FROM discovery.discovery_intent WHERE subject_id=? AND status IN ('WAITING','DISPATCHING','RUNNING') ORDER BY accepted_at,id",
                                    r.scope().subjectId());
                    String run;
                    if (!existing.isEmpty()) {
                        var old = existing.getFirst();
                        if (!old.get("status").equals("WAITING")
                                || !old.get("session_id").equals(r.scope().sessionId())
                                || !old.get("round_id").equals(r.round())
                                || !old.get("operation").equals("SUPPLY")
                                || !r.operation().equals("SUPPLY")) fail("EXECUTOR_BUSY");
                        run = (String) old.get("id");
                        var demands = new TreeSet<String>(r.demands());
                        json(jdbc.queryForObject(
                                        "SELECT demand_ids::text FROM discovery.discovery_intent WHERE id=?",
                                        String.class,
                                        run))
                                .forEach(n -> demands.add(n.asText()));
                        if (demands.size() > 1000) fail("POLICY_OUT_OF_RANGE");
                        jdbc.update(
                                "UPDATE discovery.discovery_intent SET dispatch_plan=?::jsonb,scope_snapshot=?::jsonb,demand_ids=?::jsonb,revision=revision+1 WHERE id=?",
                                plan.toString(),
                                JSON.valueToTree(r.scope()).toString(),
                                JSON.valueToTree(demands).toString(),
                                run);
                    } else {
                        lockAdmission();
                        if (!hasAdmissionRoom(limits)) fail("QUEUE_FULL");
                        run = UUID.randomUUID().toString();
                        Instant now =
                                (r.originalAcceptedAt() == null
                                                ? clock.instant()
                                                : r.originalAcceptedAt())
                                        .truncatedTo(java.time.temporal.ChronoUnit.MILLIS);
                        discovery.createIntent(
                                r.scope(),
                                r.round(),
                                run,
                                r.operation(),
                                sessions.inputRevision(r.scope()),
                                hash,
                                now,
                                now.plusSeconds(limits.queueSeconds()));
                        Instant original =
                                Instant.parse(
                                        plan.path("start")
                                                .path("deadlines")
                                                .path("round_deadline_at")
                                                .asText());
                        Instant deadline =
                                original.isBefore(now.plusSeconds(limits.executionSeconds()))
                                        ? original
                                        : now.plusSeconds(limits.executionSeconds());
                        if (!deadline.isAfter(now)) fail("DEADLINE_EXCEEDED");
                        jdbc.update(
                                "UPDATE discovery.discovery_intent SET dispatch_plan=?::jsonb,scope_snapshot=?::jsonb,demand_ids=?::jsonb,technical_deadline=?,revision=revision+1 WHERE id=?",
                                plan.toString(),
                                JSON.valueToTree(r.scope()).toString(),
                                JSON.valueToTree(r.demands().stream().sorted().toList()).toString(),
                                Timestamp.from(
                                        deadline.truncatedTo(java.time.temporal.ChronoUnit.MILLIS)),
                                run);
                    }
                    jdbc.update(
                            "INSERT INTO discovery.dispatch_command VALUES(?,?,?,?,?,?)",
                            r.scope().subjectId(),
                            r.scope().sessionId(),
                            r.operation(),
                            r.command(),
                            hash,
                            run);
                    return accepted(run);
                });
    }

    private Accepted accepted(String run) {
        return jdbc.queryForObject(
                "SELECT id,status,accepted_at,queue_deadline,error_code FROM discovery.discovery_intent WHERE id=?",
                (rs, n) ->
                        new Accepted(
                                rs.getString(1),
                                rs.getString(2),
                                rs.getTimestamp(3).toInstant(),
                                rs.getTimestamp(4).toInstant(),
                                rs.getString(5)),
                run);
    }

    private void expire(String subject) {
        jdbc.update(
                "UPDATE discovery.discovery_intent SET status='EXPIRED',error_code='QUEUE_TIMEOUT',revision=revision+1 WHERE subject_id=? AND status='WAITING' AND dispatch_plan IS NOT NULL AND (queue_deadline<=? OR technical_deadline<=?)",
                subject,
                Timestamp.from(clock.instant()),
                Timestamp.from(clock.instant()));
    }

    private void lockAdmission() {
        if (LockOrder.beforeAdmission())
            jdbc.queryForObject(
                    "SELECT singleton FROM discovery.admission_guard WHERE singleton FOR UPDATE",
                    Boolean.class);
    }

    private void requireCoordinator(String boot) {
        if (!Boolean.TRUE.equals(
                jdbc.queryForObject(
                        "SELECT coordinator_boot=? FROM discovery.runtime_control WHERE singleton",
                        Boolean.class,
                        boot))) fail("STALE_COORDINATOR");
    }

    @Override
    public Accepted status(SessionStore.Scope scope, String run) {
        return tx.write(
                () -> {
                    sessions.lockActive(scope);
                    expire(scope.subjectId());
                    if (jdbc.queryForObject(
                                    "SELECT count(*) FROM discovery.discovery_intent WHERE id=? AND session_id=?",
                                    Long.class,
                                    run,
                                    scope.sessionId())
                            != 1) fail("EXECUTION_GONE");
                    return accepted(run);
                });
    }

    @Override
    public Optional<String> submittedRun(SessionStore.Scope scope, String command) {
        return tx.write(
                () -> {
                    sessions.lockActive(scope);
                    return jdbc
                            .queryForList(
                                    "SELECT run_id FROM discovery.dispatch_command WHERE subject_id=? AND session_id=? AND operation='INITIAL' AND command_id=?",
                                    String.class,
                                    scope.subjectId(),
                                    scope.sessionId(),
                                    command)
                            .stream()
                            .findFirst();
                });
    }

    @Override
    public void reconcile(String javaBoot, String runtimeText) {
        var runtime = json(runtimeText);
        if (!schema.validDefinition("RuntimeView", runtime)) fail("INVALID_PAYLOAD");
        String python = runtime.path("boot_id").asText();
        var remote = new HashSet<String>();
        runtime.path("active")
                .forEach(
                        n -> {
                            if (!n.path("boot_id").asText().equals(python)
                                    || !remote.add(n.path("execution_id").asText()))
                                fail("BINDING_MISMATCH");
                        });
        var jobs =
                tx.read(
                        () -> {
                            requireCoordinator(javaBoot);
                            return active();
                        });
        var known = new HashSet<String>();
        jobs.forEach(job -> known.add(job.execution()));
        boolean recovering =
                legacyExecutions()
                        || jobs.stream()
                                .anyMatch(
                                        job ->
                                                !job.coordinatorBoot().equals(javaBoot)
                                                        || !job.pythonBoot().equals(python))
                        || !known.containsAll(remote)
                        || !runtime.path("status").asText().equals("READY");
        updateRuntime(javaBoot, python, recovering);
        for (var job : jobs) {
            try {
                if (!job.coordinatorBoot().equals(javaBoot)) stop(javaBoot, job, "INTERRUPTED");
                if (!job.pythonBoot().equals(python)) {
                    release(javaBoot, job, "INTERRUPTED");
                } else if (!job.deadline().isAfter(clock.instant())) {
                    if (remote.contains(job.execution())) stop(javaBoot, job, "EXPIRED");
                    else release(javaBoot, job, "INTERRUPTED");
                }
            } catch (RuntimeException failure) {
                if (!ShortTransactions.isLockContention(failure)) throw failure;
                // One busy subject must not postpone every other subject's cleanup.
            }
        }
        expirePending(javaBoot);
        var remaining =
                tx.read(
                        () -> {
                            requireCoordinator(javaBoot);
                            return active();
                        });
        known.clear();
        remaining.forEach(job -> known.add(job.execution()));
        updateRuntime(
                javaBoot,
                python,
                legacyExecutions()
                        || remaining.stream()
                                .anyMatch(
                                        job ->
                                                !job.coordinatorBoot().equals(javaBoot)
                                                        || !job.pythonBoot().equals(python))
                        || !known.containsAll(remote)
                        || !runtime.path("status").asText().equals("READY"));
    }

    private boolean legacyExecutions() {
        return jdbc.queryForObject(
                        "SELECT count(*) FROM discovery.execution WHERE physical_released_at IS NULL AND start_payload IS NULL",
                        Long.class)
                > 0;
    }

    private void updateRuntime(String javaBoot, String python, boolean maintenance) {
        var current =
                jdbc.queryForMap(
                        "SELECT coordinator_boot,python_boot,maintenance FROM discovery.runtime_control WHERE singleton");
        if (!Objects.equals(current.get("coordinator_boot"), javaBoot)) fail("STALE_COORDINATOR");
        if (Objects.equals(current.get("python_boot"), python)
                && Objects.equals(current.get("maintenance"), maintenance)) return;
        tx.writeExclusive(
                () -> {
                    requireCoordinator(javaBoot);
                    jdbc.update(
                            "UPDATE discovery.runtime_control SET python_boot=?,maintenance=?,revision=revision+1 WHERE singleton AND coordinator_boot=? AND (python_boot IS DISTINCT FROM ? OR maintenance<>?)",
                            python,
                            maintenance,
                            javaBoot,
                            python,
                            maintenance);
                    return null;
                });
    }

    private void expirePending(String javaBoot) {
        var subjects =
                jdbc.queryForList(
                        "SELECT DISTINCT subject_id FROM discovery.discovery_intent WHERE status='WAITING' AND dispatch_plan IS NOT NULL AND (queue_deadline<=? OR technical_deadline<=?) ORDER BY subject_id LIMIT 32",
                        String.class,
                        Timestamp.from(clock.instant()),
                        Timestamp.from(clock.instant()));
        for (String subject : subjects) {
            try {
                tx.write(
                        () -> {
                            requireCoordinator(javaBoot);
                            if (sessions.lockOwner(subject)) expire(subject);
                            return null;
                        });
            } catch (RuntimeException failure) {
                if (!ShortTransactions.isLockContention(failure)) throw failure;
            }
        }
    }

    @Override
    public void unavailable(String javaBoot) {
        if (!Boolean.TRUE.equals(
                jdbc.queryForObject(
                        "SELECT coordinator_boot=? AND NOT maintenance FROM discovery.runtime_control WHERE singleton",
                        Boolean.class,
                        javaBoot))) return;
        tx.writeExclusive(
                () -> {
                    jdbc.update(
                            "UPDATE discovery.runtime_control SET maintenance=true,revision=revision+1 WHERE singleton AND coordinator_boot=? AND NOT maintenance",
                            javaBoot);
                    return null;
                });
    }

    @Override
    public void beginRecovery(String javaBoot) {
        tx.writeExclusive(
                () -> {
                    jdbc.update(
                            "UPDATE discovery.discovery_intent SET status='INTERRUPTED',error_code='INVALID_DISPATCH_PLAN',revision=revision+1 WHERE status='WAITING' AND dispatch_plan IS NULL");
                    jdbc.update(
                            "UPDATE discovery.runtime_control SET coordinator_boot=?,maintenance=true,revision=revision+1 WHERE singleton",
                            javaBoot);
                    return null;
                });
    }

    @Override
    public boolean ready(String boot) {
        return Boolean.TRUE.equals(
                jdbc.queryForObject(
                        "SELECT NOT maintenance AND coordinator_boot=? FROM discovery.runtime_control WHERE singleton",
                        Boolean.class,
                        boot));
    }

    private Job job(Map<String, Object> row) {
        var scope = convert(json((String) row.get("scope")), SessionStore.Scope.class);
        return new Job(
                (String) row.get("id"),
                (String) row.get("round_id"),
                (String) row.get("run_id"),
                scope,
                (String) row.get("coordinator_boot"),
                (String) row.get("python_boot"),
                (String) row.get("payload"),
                ((Number) row.get("dispatch_attempts")).intValue(),
                (Boolean) row.get("cancellation_requested"),
                ((Timestamp) row.get("technical_deadline")).toInstant());
    }

    @Override
    public List<Job> active() {
        return jdbc
                .queryForList(
                        "SELECT e.*,e.start_payload::text AS payload,i.scope_snapshot::text AS scope,i.technical_deadline FROM discovery.execution e JOIN discovery.discovery_intent i ON i.id=e.run_id WHERE e.physical_released_at IS NULL AND e.start_payload IS NOT NULL ORDER BY e.id")
                .stream()
                .map(this::job)
                .toList();
    }

    private record Selection(String run, String subject, long revision) {}

    private List<Map<String, Object>> waitingHead() {
        return jdbc.queryForList(
                "SELECT id,subject_id FROM discovery.discovery_intent WHERE status='WAITING' AND dispatch_plan IS NOT NULL AND queue_deadline>? AND technical_deadline>? ORDER BY accepted_at,id LIMIT 1",
                Timestamp.from(clock.instant()),
                Timestamp.from(clock.instant()));
    }

    @Override
    public Job claim(String javaBoot, Limits limits) {
        var candidates = waitingHead();
        if (candidates.isEmpty()) return null;
        String candidate = (String) candidates.getFirst().get("id");
        String subject = (String) candidates.getFirst().get("subject_id");
        var selected = new java.util.concurrent.atomic.AtomicReference<Selection>();
        try {
            return tx.write(
                    () -> {
                        requireCoordinator(javaBoot);
                        if (!ready(javaBoot) || !sessions.lockOwner(subject)) return null;
                        expire(subject);
                        var head = waitingHead();
                        // Never lock a different owner while retaining the first owner's lock.
                        if (head.isEmpty() || !candidate.equals(head.getFirst().get("id")))
                            return null;
                        var rows =
                                jdbc.queryForList(
                                        "SELECT *,dispatch_plan::text AS plan,scope_snapshot::text AS scope FROM discovery.discovery_intent WHERE id=? AND subject_id=? AND status='WAITING'",
                                        candidate,
                                        subject);
                        if (rows.isEmpty()) return null;
                        var row = rows.getFirst();
                        String run = (String) row.get("id");
                        long revision = ((Number) row.get("revision")).longValue();
                        selected.set(new Selection(run, subject, revision));
                        var scope =
                                convert(json((String) row.get("scope")), SessionStore.Scope.class);
                        if (!scope.subjectId().equals(subject)) fail("BINDING_MISMATCH");
                        var current = sessions.current(subject);
                        if (current.isEmpty()
                                || !current.get().scope().sessionId().equals(scope.sessionId())
                                || current.get().scope().subjectEpoch() != scope.subjectEpoch()
                                || current.get().scope().sessionEpoch() != scope.sessionEpoch()) {
                            jdbc.update(
                                    "UPDATE discovery.discovery_intent SET status='SUPERSEDED',error_code='BINDING_MISMATCH',revision=revision+1 WHERE id=?",
                                    run);
                            return null;
                        }
                        scope = current.get().scope();
                        sessions.lockActive(scope);
                        String python =
                                jdbc.queryForObject(
                                        "SELECT python_boot FROM discovery.runtime_control WHERE singleton",
                                        String.class);
                        String execution = UUID.randomUUID().toString();
                        var plan = json((String) row.get("plan"));
                        var payload = (ObjectNode) plan.path("start").deepCopy();
                        payload.put("execution_id", execution).put("run_id", run);
                        if (payload.path("start_mode").asText().equals("CONTINUE")) {
                            var continuation = (ObjectNode) payload.path("continuation");
                            if (continuation.path("display_state").path("user_waiting").asBoolean())
                                ((ObjectNode) continuation.path("display_state"))
                                        .put("run_id", run);
                            if (!continuation.path("pending").path("rescreen_run_id").isNull())
                                ((ObjectNode) continuation.path("pending"))
                                        .put("rescreen_run_id", run);
                        }
                        payload.putObject("boot")
                                .put("coordinator_boot_id", javaBoot)
                                .put("python_boot_id", python);
                        ((ObjectNode) payload.path("deadlines"))
                                .put(
                                        "round_deadline_at",
                                        ((Timestamp) row.get("technical_deadline"))
                                                .toInstant()
                                                .toString());
                        LockOrder.beforePhysicalSlot();
                        var slots =
                                jdbc.queryForList(
                                        "SELECT slot_no FROM discovery.execution_slot WHERE state='FREE' AND slot_no<=? ORDER BY slot_no FOR UPDATE SKIP LOCKED LIMIT 1",
                                        Integer.class,
                                        limits.executions());
                        if (slots.isEmpty()) return null;
                        discovery.reserveExecution(
                                scope,
                                (String) row.get("round_id"),
                                run,
                                execution,
                                slots.getFirst(),
                                javaBoot,
                                python);
                        if (jdbc.update(
                                        "UPDATE discovery.discovery_intent SET status='DISPATCHING',scope_snapshot=?::jsonb,revision=revision+1 WHERE id=? AND status='WAITING' AND revision=? AND queue_deadline>? AND technical_deadline>?",
                                        JSON.valueToTree(scope).toString(),
                                        run,
                                        revision,
                                        Timestamp.from(clock.instant()),
                                        Timestamp.from(clock.instant()))
                                != 1) fail("QUEUE_TIMEOUT");
                        var demands = new HashSet<String>();
                        json(row.get("demand_ids").toString())
                                .forEach(n -> demands.add(n.asText()));
                        for (var prepare : preparation.orderedStream().toList())
                            payload =
                                    (ObjectNode)
                                            json(
                                                    prepare.prepare(
                                                            scope, payload.toString(), demands));
                        var context = context(scope, payload, plan);
                        budget.register(context.budget());
                        var authoritative = json(budget.state(context.budget()));
                        var issued =
                                ((ObjectNode) payload.path("budget"))
                                        .putObject("remaining_at_issue");
                        issued.put(
                                        "session",
                                        authoritative.path("session_remaining_microusd").asLong())
                                .put(
                                        "round",
                                        authoritative.path("round_remaining_microusd").asLong());
                        for (String lane : List.of("FOREGROUND", "BACKGROUND"))
                            issued.put(
                                    lane,
                                    Math.max(
                                            0,
                                            authoritative.path("lane_caps").path(lane).asLong()
                                                    - authoritative
                                                            .path("lanes")
                                                            .path(lane)
                                                            .path("reserved_microusd")
                                                            .asLong()
                                                    - authoritative
                                                            .path("lanes")
                                                            .path(lane)
                                                            .path("settled_microusd")
                                                            .asLong()));
                        payload.put("payload_digest", contentDigest(payload, "payload_digest"));
                        if (!schema.validDefinition("RoundStart", payload))
                            fail("INVALID_DISPATCH_PLAN");
                        jdbc.update(
                                "UPDATE discovery.execution SET start_payload=?::jsonb,revision=revision+1 WHERE id=?",
                                payload.toString(),
                                execution);
                        return new Job(
                                execution,
                                (String) row.get("round_id"),
                                run,
                                scope,
                                javaBoot,
                                python,
                                payload.toString(),
                                0,
                                false,
                                ((Timestamp) row.get("technical_deadline")).toInstant());
                    });
        } catch (PersistenceRejected | IllegalArgumentException failure) {
            if (selected.get() == null) throw failure;
            var invalid = selected.get();
            tx.write(
                    () -> {
                        requireCoordinator(javaBoot);
                        if (!sessions.lockOwner(invalid.subject())) return null;
                        expire(invalid.subject());
                        jdbc.update(
                                "UPDATE discovery.discovery_intent SET status='FAILED',error_code='INVALID_DISPATCH_PLAN',revision=revision+1 WHERE id=? AND subject_id=? AND status='WAITING' AND revision=?",
                                invalid.run(),
                                invalid.subject(),
                                invalid.revision());
                        return null;
                    });
            return null;
        }
    }

    private Context context(SessionStore.Scope scope, JsonNode payload, JsonNode plan) {
        var prices = new HashMap<String, BudgetAuthority.Price>();
        plan.path("prices")
                .fields()
                .forEachRemaining(
                        e ->
                                prices.put(
                                        e.getKey(),
                                        convert(e.getValue(), BudgetAuthority.Price.class)));
        var candidates = new HashMap<String, String>();
        plan.path("candidates")
                .fields()
                .forEachRemaining(e -> candidates.put(e.getKey(), e.getValue().asText()));
        var b = payload.path("budget");
        var limits = b.path("limits");
        var e =
                new EvidenceAuthority.Context(
                        scope,
                        payload.path("round_id").asText(),
                        payload.path("execution_id").asText(),
                        payload.path("boot").path("coordinator_boot_id").asText(),
                        payload.path("boot").path("python_boot_id").asText(),
                        plan.path("catalog_dataset").asLong(),
                        limits.path("read_calls").asInt(),
                        candidates,
                        false);
        return new Context(
                e,
                new BudgetAuthority.Context(
                        e,
                        payload.path("run_id").asText(),
                        b.path("session_budget_scope_id").asText(),
                        b.path("round_budget_scope_id").asText(),
                        limits.path("session_cap_microusd").asLong(),
                        limits.path("round_cap_microusd").asLong(),
                        limits.path("lane_caps").path("FOREGROUND").asLong(),
                        limits.path("lane_caps").path("BACKGROUND").asLong(),
                        limits.path("model_calls").asInt(),
                        limits.path("search_calls").asInt(),
                        prices));
    }

    @Override
    public Context context(String execution) {
        return tx.write(
                () -> {
                    var rows =
                            jdbc.queryForList(
                                    "SELECT e.start_payload::text AS payload,i.dispatch_plan::text AS plan,i.scope_snapshot::text AS scope FROM discovery.execution e JOIN discovery.discovery_intent i ON i.id=e.run_id WHERE e.id=? AND e.start_payload IS NOT NULL",
                                    execution);
                    if (rows.size() != 1) fail("EXECUTION_GONE");
                    var row = rows.getFirst();
                    return context(
                            sessions.lockRefreshed(
                                    convert(
                                            json((String) row.get("scope")),
                                            SessionStore.Scope.class)),
                            json((String) row.get("payload")),
                            json((String) row.get("plan")));
                });
    }

    @Override
    public boolean markSending(String actingBoot, Job job) {
        return tx.write(
                () -> {
                    requireCoordinator(actingBoot);
                    if (!ready(actingBoot)
                            || !job.coordinatorBoot().equals(actingBoot)
                            || !sessions.lockOwner(job.scope().subjectId())) return false;
                    var current = sessions.current(job.scope().subjectId());
                    if (current.isEmpty()
                            || !current.get().scope().sessionId().equals(job.scope().sessionId())
                            || current.get().scope().subjectEpoch() != job.scope().subjectEpoch()
                            || current.get().scope().sessionEpoch() != job.scope().sessionEpoch())
                        return false;
                    sessions.lockActive(current.get().scope());
                    if (!job.deadline().isAfter(clock.instant())) return false;
                    return jdbc.update(
                                    "UPDATE discovery.execution SET dispatch_attempts=dispatch_attempts+1,sent_at=coalesce(sent_at,?),revision=revision+1 WHERE id=? AND coordinator_boot=? AND python_boot=? AND dispatch_attempts=? AND dispatch_attempts<2 AND NOT cancellation_requested AND physical_released_at IS NULL",
                                    Timestamp.from(clock.instant()),
                                    job.execution(),
                                    job.coordinatorBoot(),
                                    job.pythonBoot(),
                                    job.attempts())
                            == 1;
                });
    }

    private void stopLocked(Job job, String status) {
        LockOrder.beforePhysicalSlot();
        jdbc.update(
                "UPDATE discovery.discovery_intent SET status=?,error_code=?,revision=revision+1 WHERE id=? AND status IN ('WAITING','DISPATCHING','RUNNING')",
                status,
                status.equals("EXPIRED") ? "DEADLINE_EXCEEDED" : "EXECUTION_GONE",
                job.run());
        jdbc.update(
                "UPDATE discovery.execution SET cancellation_requested=true,cancel_reason='USER_CANCELLED',revision=revision+1 WHERE id=? AND physical_released_at IS NULL",
                job.execution());
        jdbc.update(
                "UPDATE discovery.execution_slot SET state='RELEASING' WHERE execution_id=?",
                job.execution());
    }

    @Override
    public void stop(String actingBoot, Job job, String status) {
        if (!Set.of("INTERRUPTED", "EXPIRED", "CANCELLED", "SUPERSEDED").contains(status))
            fail("INVALID_TERMINAL_STATE");
        tx.write(
                () -> {
                    if (lockJob(actingBoot, job).isEmpty()) return null;
                    stopLocked(job, status);
                    return null;
                });
    }

    private void releaseLocked(Job job, String status) {
        LockOrder.beforePhysicalSlot();
        jdbc.update(
                "UPDATE discovery.discovery_intent SET status=?,revision=revision+1 WHERE id=? AND status IN ('DISPATCHING','RUNNING')",
                status,
                job.run());
        jdbc.update(
                "UPDATE discovery.execution SET state='CLOSED',physical_released_at=?,revision=revision+1 WHERE id=? AND physical_released_at IS NULL",
                Timestamp.from(clock.instant()),
                job.execution());
        jdbc.update(
                "UPDATE discovery.execution_slot SET state='FREE',execution_id=NULL WHERE execution_id=?",
                job.execution());
    }

    @Override
    public void release(String actingBoot, Job job, String status) {
        if (!Set.of("INTERRUPTED", "CANCELLED", "EXPIRED").contains(status))
            fail("INVALID_TERMINAL_STATE");
        tx.write(
                () -> {
                    if (lockJob(actingBoot, job).isEmpty()) return null;
                    releaseLocked(job, status);
                    return null;
                });
    }

    @Override
    public void observe(String actingBoot, Job job, String text) {
        var view = json(text);
        if (!schema.validDefinition("ExecutionView", view)
                || !view.path("execution_id").asText().equals(job.execution())
                || !view.path("round_id").asText().equals(job.round())
                || !view.path("boot_id").asText().equals(job.pythonBoot()))
            fail("BINDING_MISMATCH");
        boolean released = view.path("slot_released").asBoolean();
        String state = view.path("status").asText();
        if (released && !Set.of("CLOSED", "FAILED", "CANCELLED").contains(state))
            fail("INVALID_PAYLOAD");
        tx.write(
                () -> {
                    var rows = lockJob(actingBoot, job);
                    if (rows.isEmpty()
                            || ((Number) rows.getFirst().get("observed_seq")).longValue()
                                    > view.path("updated_seq").asLong()) return null;
                    if (released) {
                        releaseLocked(job, "INTERRUPTED");
                        return null;
                    }
                    boolean cancelling = (Boolean) rows.getFirst().get("cancellation_requested");
                    LockOrder.beforePhysicalSlot();
                    jdbc.update(
                            "UPDATE discovery.execution SET state=?,observed_seq=?,revision=revision+1 WHERE id=?",
                            cancelling ? "CLOSING" : state,
                            view.path("updated_seq").asLong(),
                            job.execution());
                    jdbc.update(
                            "UPDATE discovery.execution_slot SET state=? WHERE execution_id=?",
                            cancelling ? "RELEASING" : "ACTIVE",
                            job.execution());
                    if (!cancelling)
                        jdbc.update(
                                "UPDATE discovery.discovery_intent SET status='RUNNING',revision=revision+1 WHERE id=? AND status='DISPATCHING'",
                                job.run());
                    return null;
                });
    }

    private List<Map<String, Object>> lockJob(String actingBoot, Job job) {
        requireCoordinator(actingBoot);
        if (!sessions.lockOwner(job.scope().subjectId())) return List.of();
        LockOrder.before(LockOrder.Level.EXECUTION, job.execution());
        return jdbc.queryForList(
                "SELECT e.observed_seq,e.cancellation_requested FROM discovery.execution e JOIN discovery.discovery_intent i ON i.id=e.run_id WHERE e.id=? AND e.session_id=? AND e.round_id=? AND e.coordinator_boot=? AND e.python_boot=? AND e.physical_released_at IS NULL AND i.subject_id=? FOR UPDATE OF e",
                job.execution(),
                job.scope().sessionId(),
                job.round(),
                job.coordinatorBoot(),
                job.pythonBoot(),
                job.scope().subjectId());
    }

    @Override
    public void cancel(SessionStore.Scope scope, String run) {
        tx.write(
                () -> {
                    discovery.lockRuntime();
                    sessions.lockActive(scope);
                    if (jdbc.queryForObject(
                                    "SELECT count(*) FROM discovery.discovery_intent WHERE id=? AND session_id=?",
                                    Long.class,
                                    run,
                                    scope.sessionId())
                            != 1) fail("EXECUTION_GONE");
                    jdbc.update(
                            "UPDATE discovery.discovery_intent SET status='CANCELLED',revision=revision+1 WHERE id=? AND status IN ('WAITING','DISPATCHING','RUNNING')",
                            run);
                    jdbc.update(
                            "UPDATE discovery.execution SET cancellation_requested=true,cancel_reason='USER_CANCELLED',revision=revision+1 WHERE run_id=? AND physical_released_at IS NULL",
                            run);
                    return null;
                });
    }

    @Override
    public Set<String> demands(SessionStore.Scope scope, String run) {
        return tx.write(
                () -> {
                    status(scope, run);
                    var result = new TreeSet<String>();
                    json(jdbc.queryForObject(
                                    "SELECT demand_ids::text FROM discovery.discovery_intent WHERE id=?",
                                    String.class,
                                    run))
                            .forEach(n -> result.add(n.asText()));
                    return Set.copyOf(result);
                });
    }

    @Override
    public void complete(SessionStore.Scope scope, String run, String outcome) {
        if (!Set.of("SUCCEEDED", "DEGRADED", "NO_MATCH", "FAILED").contains(outcome))
            fail("INVALID_TERMINAL_STATE");
        tx.write(
                () -> {
                    discovery.lockRuntime();
                    sessions.lockActive(scope);
                    jdbc.update(
                            "UPDATE discovery.discovery_intent SET status=?,revision=revision+1 WHERE id=? AND session_id=? AND status IN ('DISPATCHING','RUNNING')",
                            outcome,
                            run,
                            scope.sessionId());
                    return null;
                });
    }

    @Override
    public String startPayload(String execution) {
        var rows =
                jdbc.queryForList(
                        "SELECT start_payload::text FROM discovery.execution WHERE id=? AND start_payload IS NOT NULL",
                        String.class,
                        execution);
        if (rows.size() != 1) fail("EXECUTION_GONE");
        return rows.getFirst();
    }

    @Override
    public void bindCandidates(String execution, Map<String, String> candidates) {
        tx.write(
                () -> {
                    discovery.lockRuntime();
                    var c = context(execution);
                    sessions.lockActive(c.evidence().scope());
                    var s = c.evidence();
                    new PostgresExecutionReadAccess(jdbc)
                            .guard(
                                    s.scope(),
                                    s.round(),
                                    execution,
                                    s.coordinatorBoot(),
                                    s.pythonBoot());
                    jdbc.update(
                            "UPDATE discovery.discovery_intent SET dispatch_plan=jsonb_set(dispatch_plan,'{candidates}',?::jsonb),revision=revision+1 WHERE id=(SELECT run_id FROM discovery.execution WHERE id=?)",
                            JSON.valueToTree(candidates).toString(),
                            execution);
                    return null;
                });
    }

    @Override
    public void prioritize(SessionStore.Scope scope, String run, String candidate, String command) {
        tx.write(
                () -> {
                    discovery.lockRuntime();
                    sessions.lockActive(scope);
                    var now = clock.instant();
                    var jobs =
                            active().stream()
                                    .filter(
                                            j ->
                                                    j.scope().sessionId().equals(scope.sessionId())
                                                            && j.run().equals(run)
                                                            && !j.cancel()
                                                            && now.isBefore(j.deadline()))
                                    .toList();
                    for (var job : jobs) {
                        String id =
                                java.util
                                        .UUID
                                        .nameUUIDFromBytes(
                                                (command + job.execution())
                                                        .getBytes(
                                                                java.nio.charset.StandardCharsets
                                                                        .UTF_8))
                                        .toString();
                        var body =
                                JSON.createObjectNode()
                                        .put("command_id", id)
                                        .put("kind", "PRIORITIZE_LONG_REVIEW");
                        body.putObject("boot")
                                .put("coordinator_boot_id", job.coordinatorBoot())
                                .put("python_boot_id", job.pythonBoot());
                        body.putObject("payload").put("candidate_id", candidate);
                        body.put("payload_digest", digest(body.path("payload")));
                        if (!schema.validDefinition("RoundCommand", body)) fail("INVALID_PAYLOAD");
                        jdbc.update(
                                "INSERT INTO discovery.execution_command VALUES(?,?,?::jsonb,false) ON CONFLICT(id) DO NOTHING",
                                id,
                                job.execution(),
                                body.toString());
                    }
                    return null;
                });
    }

    @Override
    public List<Command> commands() {
        return jdbc.query(
                "SELECT c.id,c.execution_id,c.payload::text FROM discovery.execution_command c JOIN discovery.execution e ON e.id=c.execution_id WHERE NOT c.acknowledged AND e.physical_released_at IS NULL AND NOT e.cancellation_requested ORDER BY c.id LIMIT 3",
                (r, n) -> new Command(r.getString(1), r.getString(2), r.getString(3)));
    }

    @Override
    public void acknowledge(String actingBoot, Command c) {
        var owners =
                jdbc.queryForList(
                        "SELECT i.subject_id FROM discovery.execution e JOIN discovery.discovery_intent i ON i.id=e.run_id WHERE e.id=?",
                        String.class,
                        c.execution());
        if (owners.isEmpty()) return;
        tx.write(
                () -> {
                    requireCoordinator(actingBoot);
                    if (!sessions.lockOwner(owners.getFirst())) return null;
                    jdbc.update(
                            "UPDATE discovery.execution_command SET acknowledged=true WHERE id=? AND execution_id=?",
                            c.id(),
                            c.execution());
                    return null;
                });
    }

    @Override
    public void display(SessionStore.Scope scope, String round, String state, String command) {
        var payload = parseDisplay(state);
        tx.write(
                () -> {
                    discovery.lockRuntime();
                    sessions.lockActive(scope);
                    for (var job : active())
                        if (job.scope().sessionId().equals(scope.sessionId())
                                && job.round().equals(round)
                                && !job.cancel()) {
                            String id =
                                    UUID.nameUUIDFromBytes(
                                                    ("display:" + command + job.execution())
                                                            .getBytes(
                                                                    java.nio.charset
                                                                            .StandardCharsets
                                                                            .UTF_8))
                                            .toString();
                            var body =
                                    JSON.createObjectNode()
                                            .put("command_id", id)
                                            .put("kind", "DISPLAY_STATE");
                            body.putObject("boot")
                                    .put("coordinator_boot_id", job.coordinatorBoot())
                                    .put("python_boot_id", job.pythonBoot());
                            body.set("payload", payload);
                            body.put("payload_digest", digest(payload));
                            if (!schema.validDefinition("RoundCommand", body))
                                fail("INVALID_PAYLOAD");
                            jdbc.update(
                                    "INSERT INTO discovery.execution_command VALUES(?,?,?::jsonb,false) ON CONFLICT(id) DO NOTHING",
                                    id,
                                    job.execution(),
                                    body.toString());
                        }
                    return null;
                });
    }

    private JsonNode parseDisplay(String text) {
        var node = json(text);
        if (!schema.validDefinition("DisplayState", node)) fail("INVALID_PAYLOAD");
        return node;
    }

    @Override
    public Optional<Accepted> mergeSupply(
            SessionStore.Scope scope,
            String round,
            String command,
            Set<String> demands,
            Limits limits) {
        return tx.write(
                () -> {
                    discovery.lockRuntime();
                    sessions.lockActive(scope);
                    var rows =
                            jdbc.queryForList(
                                    "SELECT dispatch_plan::text AS plan,accepted_at FROM discovery.discovery_intent WHERE session_id=? AND round_id=? AND operation='SUPPLY' AND status='WAITING'",
                                    scope.sessionId(),
                                    round);
                    if (rows.isEmpty()) return Optional.empty();
                    var row = rows.getFirst();
                    var plan = json((String) row.get("plan"));
                    var prices = new HashMap<String, BudgetAuthority.Price>();
                    plan.path("prices")
                            .fields()
                            .forEachRemaining(
                                    e ->
                                            prices.put(
                                                    e.getKey(),
                                                    convert(
                                                            e.getValue(),
                                                            BudgetAuthority.Price.class)));
                    var mapping = new HashMap<String, String>();
                    plan.path("candidates")
                            .fields()
                            .forEachRemaining(e -> mapping.put(e.getKey(), e.getValue().asText()));
                    return Optional.of(
                            accept(
                                    new Request(
                                            scope,
                                            round,
                                            "SUPPLY",
                                            command,
                                            plan.path("start").toString(),
                                            prices,
                                            plan.path("catalog_dataset").asLong(),
                                            mapping,
                                            demands,
                                            ((java.sql.Timestamp) row.get("accepted_at"))
                                                    .toInstant()),
                                    limits));
                });
    }

    @Override
    public boolean canAccept(SessionStore.Scope scope, Limits limits) {
        sessions.lockActive(scope);
        expire(scope.subjectId());
        if (jdbc.queryForObject(
                        "SELECT maintenance FROM discovery.runtime_control WHERE singleton",
                        Boolean.class)
                || jdbc.queryForObject(
                                "SELECT count(*) FROM discovery.discovery_intent WHERE subject_id=? AND status IN ('WAITING','DISPATCHING','RUNNING')",
                                Long.class,
                                scope.subjectId())
                        != 0) return false;
        lockAdmission();
        return hasAdmissionRoom(limits);
    }

    private boolean hasAdmissionRoom(Limits limits) {
        // All arrivals pass through WAITING, including work about to claim an idle slot.
        return jdbc.queryForObject(
                "SELECT (SELECT count(*) FROM discovery.discovery_intent WHERE status='WAITING' AND dispatch_plan IS NOT NULL AND queue_deadline>? AND technical_deadline>?)"
                        + " < ?::bigint + (SELECT count(*) FROM discovery.execution_slot WHERE state='FREE' AND slot_no<=?)",
                Boolean.class,
                Timestamp.from(clock.instant()),
                Timestamp.from(clock.instant()),
                limits.waiting(),
                limits.executions());
    }
}
