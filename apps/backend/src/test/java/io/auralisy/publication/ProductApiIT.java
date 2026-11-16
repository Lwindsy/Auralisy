package io.auralisy.publication;

import static io.auralisy.contracts.CanonicalJson.*;
import static org.junit.jupiter.api.Assertions.*;

import com.fasterxml.jackson.databind.node.ObjectNode;
import io.auralisy.discovery.application.Recommendations;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@EnabledIfEnvironmentVariable(named = "AURALIS_TEST_POSTGRES_URL", matches = ".+")
class ProductApiIT extends ProductApiFixture {
    @Test
    void curatedKeysAreSubjectScopedAndLegacyRunReceiptsStillReplay() throws Exception {
        int sends = peer.sends.get();
        var a = browser();
        var b = browser();
        String key = "shared-curated-key";
        var input = userInput("");
        input.putArray("liked_genres");
        input.putArray("disliked_genre_ids").add(ROCK);
        var body =
                JSON.createObjectNode().putNull("expected_session_id").put("expected_revision", 0);
        body.set("input", input);
        var first = a.request("POST", "/v1/discovery-sessions", body, key);
        var second = b.request("POST", "/v1/discovery-sessions", body, key);
        assertEquals(200, first.status(), first.body().toString());
        assertEquals(200, second.status(), second.body().toString());
        String original = first.body().path("run_id").asText();
        String other = second.body().path("run_id").asText();
        assertNotEquals(original, other);
        assertNotEquals(a.session(), b.session());
        assertEquals(
                original,
                a.request("POST", "/v1/discovery-sessions", body, key)
                        .body()
                        .path("run_id")
                        .asText());

        // Represent a persisted pre-change curated task. It has no Python dispatch record.
        String legacy = digest(JSON.getNodeFactory().textNode("curated:" + key));
        tx.write(
                () -> {
                    var scope =
                            sessions.current(
                                            subjects.authenticate(a.token())
                                                    .orElseThrow()
                                                    .subjectId())
                                    .orElseThrow()
                                    .scope();
                    sessions.lockActive(scope);
                    assertEquals(
                            1,
                            jdbc.update(
                                    "INSERT INTO discovery.discovery_intent(id,subject_id,session_id,round_id,operation,input_revision,request_digest,accepted_at,queue_deadline,status,revision) SELECT ?,subject_id,session_id,round_id,operation,input_revision,request_digest,accepted_at,queue_deadline,status,revision FROM discovery.discovery_intent WHERE id=?",
                                    legacy,
                                    original));
                    jdbc.update(
                            "UPDATE discovery.long_review SET generation_run=?,revision=revision+1 WHERE session_id=? AND generation_run=?",
                            legacy,
                            a.session(),
                            original);
                    jdbc.update(
                            "UPDATE discovery.publication_round SET run_id=? WHERE session_id=? AND run_id=?",
                            legacy,
                            a.session(),
                            original);
                    jdbc.update(
                            "UPDATE discovery.recovery_projection SET state=jsonb_set(jsonb_set(state,'{run_id}',to_jsonb(?::text)),'{admission,intent_id}',to_jsonb(?::text)) WHERE session_id=?",
                            legacy,
                            legacy,
                            a.session());
                    assertEquals(
                            1,
                            jdbc.update(
                                    "DELETE FROM discovery.discovery_intent WHERE id=?", original));
                    return null;
                });
        var replay = a.request("POST", "/v1/discovery-sessions", body, key);
        assertEquals(200, replay.status(), replay.body().toString());
        assertEquals(a.session(), replay.body().path("session_id").asText());
        assertEquals(legacy, replay.body().path("run_id").asText());
        var independent = b.request("POST", "/v1/discovery-sessions", body, key);
        assertEquals(200, independent.status(), independent.body().toString());
        assertEquals(other, independent.body().path("run_id").asText());
        assertEquals(sends, peer.sends.get());
    }

    @Test
    void subjectBlockedInSubmissionDoesNotBlockAnotherSubjectsAdmission() throws Exception {
        var blocked = browser();
        var independent = browser();
        String subject = subjects.authenticate(blocked.token()).orElseThrow().subjectId();
        var actual = app.getBean(Recommendations.class);
        var replayRead =
                org.mockito.Mockito.mock(
                        io.auralisy.session.api.SubjectSession.class,
                        org.mockito.AdditionalAnswers.delegatesTo(subjects));
        // Model a successful earlier read which found no receipt. The authoritative replace
        // must still acquire this subject's row lock inside the write transaction.
        org.mockito.Mockito.doReturn(Optional.empty())
                .when(replayRead)
                .replaySubmission(
                        org.mockito.ArgumentMatchers.eq(blocked.token()),
                        org.mockito.ArgumentMatchers.eq(blocked.csrf),
                        org.mockito.ArgumentMatchers.eq("blocked-subject"),
                        org.mockito.ArgumentMatchers.isNull(),
                        org.mockito.ArgumentMatchers.eq(0L),
                        org.mockito.ArgumentMatchers.anyString());
        org.mockito.Mockito.doAnswer(
                        invocation -> {
                            // Only lengthen this test's deliberately blocked acquisition, in the
                            // real
                            // submission transaction. Do not wrap submit in a caller-owned
                            // transaction.
                            jdbc.execute("SET LOCAL lock_timeout='4s'");
                            return subjects.replace(
                                    invocation.getArgument(0),
                                    invocation.getArgument(1),
                                    invocation.getArgument(2),
                                    invocation.getArgument(3),
                                    invocation.<Long>getArgument(4),
                                    invocation.getArgument(5),
                                    invocation.getArgument(6));
                        })
                .when(replayRead)
                .replace(
                        org.mockito.ArgumentMatchers.eq(blocked.token()),
                        org.mockito.ArgumentMatchers.eq(blocked.csrf),
                        org.mockito.ArgumentMatchers.eq("blocked-subject"),
                        org.mockito.ArgumentMatchers.isNull(),
                        org.mockito.ArgumentMatchers.eq(0L),
                        org.mockito.ArgumentMatchers.anyString(),
                        org.mockito.ArgumentMatchers.anyString());
        org.springframework.test.util.ReflectionTestUtils.setField(actual, "subjects", replayRead);
        var body =
                JSON.createObjectNode().putNull("expected_session_id").put("expected_revision", 0);
        body.set("input", userInput(""));
        peer.hold = true;
        try (var worker = Executors.newSingleThreadExecutor();
                var held = app.getBean(javax.sql.DataSource.class).getConnection()) {
            held.setAutoCommit(false);
            int blocker;
            try (var statement = held.createStatement();
                    var rows = statement.executeQuery("SELECT pg_backend_pid()")) {
                assertTrue(rows.next());
                blocker = rows.getInt(1);
            }
            try (var statement =
                    held.prepareStatement(
                            "SELECT id FROM discovery.subject WHERE id=? FOR UPDATE")) {
                statement.setString(1, subject);
                statement.executeQuery().close();
            }
            var waiting =
                    worker.submit(
                            () ->
                                    actual.submit(
                                            blocked.token(),
                                            blocked.csrf,
                                            "blocked-subject",
                                            body.toString()));
            try {
                long until = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
                boolean observed;
                do {
                    observed =
                            Boolean.TRUE.equals(
                                    jdbc.queryForObject(
                                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND ?=ANY(pg_blocking_pids(pid)))",
                                            Boolean.class,
                                            blocker));
                    if (!observed) Thread.sleep(10);
                } while (!observed && System.nanoTime() < until);
                assertTrue(observed, "The first submission is actually waiting on its subject row");
                var admitted = independent.submit(userInput(""), "other-subject");
                assertEquals(202, admitted.status(), admitted.body().toString());
                assertFalse(
                        waiting.isDone(),
                        "Unrelated admission must finish before the first subject unlocks");
            } finally {
                held.rollback();
            }
            assertTrue(
                    JSON.readTree(waiting.get(5, TimeUnit.SECONDS)).path("session_id").isTextual());
        } finally {
            org.springframework.test.util.ReflectionTestUtils.setField(
                    actual, "subjects", subjects);
            peer.hold = false;
            org.mockito.Mockito.framework().clearInlineMock(replayRead);
        }
    }

    @Test
    void concurrentIdenticalSubmissionsShareOneIntentAndExecution() throws Exception {
        var browser = browser();
        var body =
                JSON.createObjectNode().putNull("expected_session_id").put("expected_revision", 0);
        body.set("input", userInput(""));
        var start = new CountDownLatch(1);
        peer.hold = true;
        try (var workers = Executors.newFixedThreadPool(4)) {
            var requests = new ArrayList<java.util.concurrent.Future<Reply>>();
            for (int n = 0; n < 4; n++)
                requests.add(
                        workers.submit(
                                () -> {
                                    assertTrue(start.await(5, TimeUnit.SECONDS));
                                    return browser.request(
                                            "POST",
                                            "/v1/discovery-sessions",
                                            body,
                                            "same-in-flight-key");
                                }));
            start.countDown();
            String session = null, run = null;
            for (var pending : requests) {
                var response = pending.get(10, TimeUnit.SECONDS);
                assertEquals(202, response.status(), response.body().toString());
                if (session == null) {
                    session = response.body().path("session_id").asText();
                    run = response.body().path("run_id").asText();
                }
                assertEquals(session, response.body().path("session_id").asText());
                assertEquals(run, response.body().path("run_id").asText());
            }
            browser.awaitState(s -> s.path("admission").path("status").asText().equals("RUNNING"));
            assertEquals(
                    1,
                    jdbc.queryForObject(
                            "SELECT count(*) FROM discovery.discovery_intent WHERE session_id=?",
                            Integer.class,
                            session));
            assertEquals(
                    1,
                    jdbc.queryForObject(
                            "SELECT count(*) FROM discovery.execution WHERE run_id=?",
                            Integer.class,
                            run));
            assertEquals(
                    1,
                    jdbc.queryForObject(
                            "SELECT count(*) FROM discovery.command_receipt WHERE subject_id=(SELECT subject_id FROM discovery.session WHERE id=?) AND operation='SUBMIT' AND command_id='same-in-flight-key'",
                            Integer.class,
                            session));
        } finally {
            start.countDown();
            peer.hold = false;
        }
    }

    @Test
    void queueFullReplacementRollsBackTheExistingSessionAndCancellation() throws Exception {
        peer.hold = true;
        var owner = browser();
        assertEquals(202, owner.submit(userInput(""), "existing-running-session").status());
        owner.awaitState(s -> s.path("admission").path("status").asText().equals("RUNNING"));
        String original = owner.session();
        String oldRun = owner.state.path("run_id").asText();
        var oldInput =
                JSON.readTree(
                        jdbc.queryForObject(
                                "SELECT raw_input::text FROM discovery.session WHERE id=?",
                                String.class,
                                original));
        for (int n = 0; n < 2; n++) {
            var busy = browser();
            assertEquals(202, busy.submit(userInput(""), "occupied-" + n).status());
            busy.awaitState(s -> s.path("admission").path("status").asText().equals("RUNNING"));
        }
        var queued = new ArrayList<Browser>();
        for (int n = 0; n < 5; n++) {
            var waiting = browser();
            assertEquals(202, waiting.submit(userInput(""), "queued-existing-" + n).status());
            queued.add(waiting);
        }
        owner.refresh();
        var replacement =
                JSON.createObjectNode()
                        .put("expected_session_id", original)
                        .put("expected_revision", owner.state.path("revision").asLong());
        replacement.set(
                "input", userInput("changed input while the old recommendation is running"));
        var rejected =
                owner.request("POST", "/v1/discovery-sessions", replacement, "replace-when-full");
        assertEquals(429, rejected.status(), rejected.body().toString());
        assertEquals("QUEUE_FULL", rejected.body().path("code").asText());
        assertFalse(rejected.body().path("idempotency_consumed").asBoolean());
        assertEquals(
                "ACTIVE",
                jdbc.queryForObject(
                        "SELECT status FROM discovery.session WHERE id=?", String.class, original));
        assertEquals(
                original,
                jdbc.queryForObject(
                        "SELECT current_session_id FROM discovery.subject WHERE id=(SELECT subject_id FROM discovery.session WHERE id=?)",
                        String.class,
                        original));
        assertEquals(
                oldInput,
                JSON.readTree(
                        jdbc.queryForObject(
                                "SELECT raw_input::text FROM discovery.session WHERE id=?",
                                String.class,
                                original)));
        assertFalse(
                jdbc.queryForObject(
                        "SELECT cancellation_requested FROM discovery.execution WHERE run_id=?",
                        Boolean.class,
                        oldRun));
        assertEquals(
                "RUNNING",
                jdbc.queryForObject(
                        "SELECT status FROM discovery.discovery_intent WHERE id=?",
                        String.class,
                        oldRun));
        assertEquals(
                0,
                jdbc.queryForObject(
                        "SELECT count(*) FROM discovery.command_receipt WHERE subject_id=(SELECT subject_id FROM discovery.session WHERE id=?) AND command_id='replace-when-full'",
                        Integer.class,
                        original));
        var removed = queued.getFirst();
        subjects.erase(removed.token(), removed.csrf);
        var accepted =
                owner.request("POST", "/v1/discovery-sessions", replacement, "replace-when-full");
        assertEquals(202, accepted.status(), accepted.body().toString());
        assertNotEquals(original, accepted.body().path("session_id").asText());
    }

    @Test
    void committedSubmissionCanReplayAfterResponseAndCatalogFailures() throws Exception {
        var browser = browser();
        var body =
                JSON.createObjectNode().putNull("expected_session_id").put("expected_revision", 0);
        body.set("input", userInput(""));
        var actual = app.getBean(Recommendations.class);
        var failing = org.mockito.Mockito.spy(actual);
        var controller = app.getBean(io.auralisy.bootstrap.RecommendationHttpController.class);
        org.mockito.Mockito.doThrow(
                        new org.springframework.dao.DataAccessResourceFailureException(
                                "private infrastructure detail"))
                .doCallRealMethod()
                .when(failing)
                .state(
                        org.mockito.ArgumentMatchers.eq(browser.token()),
                        org.mockito.ArgumentMatchers.anyString(),
                        org.mockito.ArgumentMatchers.eq(false));
        org.springframework.test.util.ReflectionTestUtils.setField(
                controller, "recommendations", failing);
        peer.hold = true;
        try {
            var unavailable =
                    browser.request("POST", "/v1/discovery-sessions", body, "committed-response");
            assertEquals(503, unavailable.status(), unavailable.body().toString());
            assertEquals("DEPENDENCY_UNAVAILABLE", unavailable.body().path("code").asText());
            assertTrue(unavailable.body().path("idempotency_consumed").asBoolean());
            assertTrue(unavailable.body().path("retryable").asBoolean());
            assertEquals("RETRY_SAME_KEY", unavailable.body().path("retry_disposition").asText());
            assertFalse(unavailable.body().toString().contains("private infrastructure detail"));
            String subject = subjects.authenticate(browser.token()).orElseThrow().subjectId();
            String session =
                    jdbc.queryForObject(
                            "SELECT id FROM discovery.session WHERE subject_id=? AND status='ACTIVE'",
                            String.class,
                            subject);
            String run =
                    jdbc.queryForObject(
                            "SELECT id FROM discovery.discovery_intent WHERE session_id=?",
                            String.class,
                            session);
            assertEquals(
                    1,
                    jdbc.queryForObject(
                            "SELECT count(*) FROM discovery.command_receipt WHERE subject_id=? AND operation='SUBMIT' AND command_id='committed-response'",
                            Integer.class,
                            subject));

            // Existing receipts must remain usable when new catalog work is unavailable.
            io.auralisy.catalog.api.CatalogVersion unavailableCatalog =
                    () -> {
                        throw new io.auralisy.persistence.PersistenceRejected(
                                "RECOMMENDATION_UNAVAILABLE");
                    };
            org.springframework.test.util.ReflectionTestUtils.setField(
                    failing, "versions", unavailableCatalog);
            var replay =
                    browser.request("POST", "/v1/discovery-sessions", body, "committed-response");
            assertEquals(202, replay.status(), replay.body().toString());
            assertEquals(session, replay.body().path("session_id").asText());
            assertEquals(run, replay.body().path("run_id").asText());
            browser.awaitState(s -> s.path("admission").path("status").asText().equals("RUNNING"));
            assertEquals(
                    1,
                    jdbc.queryForObject(
                            "SELECT count(*) FROM discovery.execution WHERE run_id=?",
                            Integer.class,
                            run));
            assertEquals(
                    1,
                    jdbc.queryForObject(
                            "SELECT count(*) FROM discovery.discovery_intent WHERE session_id=?",
                            Integer.class,
                            session));

            var changed = body.deepCopy();
            ((ObjectNode) changed.path("input")).put("request_text", "different request");
            var conflict =
                    browser.request(
                            "POST", "/v1/discovery-sessions", changed, "committed-response");
            assertEquals(409, conflict.status());
            assertEquals("IDEMPOTENCY_CONFLICT", conflict.body().path("code").asText());
            var newRequest =
                    browser.request("POST", "/v1/discovery-sessions", body, "not-committed");
            assertEquals(503, newRequest.status());
            assertFalse(newRequest.body().path("idempotency_consumed").asBoolean());
            assertEquals(
                    0,
                    jdbc.queryForObject(
                            "SELECT count(*) FROM discovery.command_receipt WHERE subject_id=? AND operation='SUBMIT' AND command_id='not-committed'",
                            Integer.class,
                            subject));
        } finally {
            org.springframework.test.util.ReflectionTestUtils.setField(
                    controller, "recommendations", actual);
            peer.hold = false;
            org.mockito.Mockito.framework().clearInlineMock(failing);
        }
    }

    @Test
    void revocationAfterCommitIsTerminalRatherThanRetryable() throws Exception {
        var browser = browser();
        var actual = app.getBean(Recommendations.class);
        var revoked =
                org.mockito.Mockito.mock(
                        io.auralisy.session.api.SubjectSession.class,
                        org.mockito.AdditionalAnswers.delegatesTo(subjects));
        try (var eraser = Executors.newSingleThreadExecutor()) {
            org.mockito.Mockito.doAnswer(
                            invocation -> {
                                // Revoke on another transaction after submit committed, before its
                                // response
                                // takes the subject lock. The failed response must not undo that
                                // revocation.
                                eraser.submit(() -> subjects.erase(browser.token(), browser.csrf))
                                        .get(5, TimeUnit.SECONDS);
                                return subjects.authenticate(browser.token());
                            })
                    .when(revoked)
                    .authenticate(browser.token());
            org.springframework.test.util.ReflectionTestUtils.setField(actual, "subjects", revoked);
            peer.hold = true;
            var result = browser.submit(userInput(""), "revoked-after-commit");
            assertEquals(400, result.status(), result.body().toString());
            assertTrue(result.body().path("idempotency_consumed").asBoolean());
            assertFalse(result.body().path("retryable").asBoolean());
            assertEquals("STOP", result.body().path("retry_disposition").asText());
            assertFalse(result.body().has("session_id"));
            assertTrue(subjects.authenticate(browser.token()).isEmpty());
        } finally {
            org.springframework.test.util.ReflectionTestUtils.setField(
                    actual, "subjects", subjects);
            peer.hold = false;
            org.mockito.Mockito.framework().clearInlineMock(revoked);
        }
    }

    @Test
    void slowSubmitResponseDoesNotBlockAnotherSubjectsAdmission() throws Exception {
        var first = browser();
        var second = browser();
        var body =
                JSON.createObjectNode()
                        .put("expected_revision", first.state.path("revision").asLong());
        body.set("expected_session_id", first.state.path("session_id"));
        body.set("input", userInput(""));
        var rendering = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        var delayed = org.mockito.Mockito.spy(app.getBean(Recommendations.class));
        org.mockito.Mockito.doAnswer(
                        invocation -> {
                            rendering.countDown();
                            assertTrue(release.await(10, TimeUnit.SECONDS));
                            return invocation.callRealMethod();
                        })
                .when(delayed)
                .state(
                        org.mockito.ArgumentMatchers.eq(first.token()),
                        org.mockito.ArgumentMatchers.anyString(),
                        org.mockito.ArgumentMatchers.eq(false));
        peer.hold = true;
        try (var worker = Executors.newSingleThreadExecutor()) {
            var response =
                    worker.submit(
                            () ->
                                    delayed.submit(
                                            first.token(),
                                            first.csrf,
                                            "slow-response",
                                            body.toString()));
            try {
                assertTrue(rendering.await(5, TimeUnit.SECONDS));
                var admitted = second.submit(userInput(""), "independent-submission");
                assertEquals(202, admitted.status(), admitted.body().toString());
                assertFalse(response.isDone(), "The first HTTP response is still being rendered");
                assertEquals(
                        1,
                        jdbc.queryForObject(
                                "SELECT count(*) FROM discovery.discovery_intent WHERE session_id=?",
                                Integer.class,
                                second.session()));
            } finally {
                release.countDown();
            }
            var initial = JSON.readTree(response.get(5, TimeUnit.SECONDS));
            var replay = first.request("POST", "/v1/discovery-sessions", body, "slow-response");
            assertEquals(202, replay.status(), replay.body().toString());
            assertTrue(initial.path("run_id").isTextual());
            assertFalse(initial.path("run_id").asText().isBlank());
            assertEquals(initial.path("session_id"), replay.body().path("session_id"));
            assertEquals(initial.path("run_id"), replay.body().path("run_id"));
            assertEquals(
                    1,
                    jdbc.queryForObject(
                            "SELECT count(*) FROM discovery.discovery_intent WHERE session_id=?",
                            Integer.class,
                            first.session()));
        } finally {
            release.countDown();
            peer.hold = false;
            org.mockito.Mockito.framework().clearInlineMock(delayed);
        }
    }

    @Test
    void currentStateKeepsLiveReviewUpdatesAndUtf8WithoutRenewingTheSession() throws Exception {
        var b = browser();
        b.submit(userInput(""), "live-state-read");
        b.awaitState(
                s ->
                        s.path("slots").size() > 0
                                && s.path("slots")
                                        .get(0)
                                        .path("card")
                                        .path("long_review_state")
                                        .asText()
                                        .equals("READY"));
        for (var job : peer.jobs.values()) job.done.get(10, java.util.concurrent.TimeUnit.SECONDS);
        b.refresh();
        String candidate = b.state.path("slots").get(0).path("card").path("candidate_id").asText();
        String expiry = b.state.path("expires_at").asText();
        long revision = b.state.path("revision").asLong();
        assertEquals(
                1,
                jdbc.update(
                        "UPDATE discovery.long_review SET state='FAILED',content=NULL,reason='WRITE_INVALID_OUTPUT',revision=revision+1 WHERE session_id=? AND candidate_id=?",
                        b.session(),
                        candidate));
        var response =
                b.client.send(
                        java.net.http.HttpRequest.newBuilder(
                                        java.net.URI.create(base + "/v1/current-state"))
                                .timeout(java.time.Duration.ofSeconds(5))
                                .GET()
                                .build(),
                        java.net.http.HttpResponse.BodyHandlers.ofByteArray());
        assertEquals(200, response.statusCode());
        assertTrue(
                response.headers()
                        .firstValue("Content-Type")
                        .orElseThrow()
                        .startsWith("application/json"));
        var state = JSON.readTree(response.body());
        assertEquals(
                "FAILED",
                state.path("slots").get(0).path("card").path("long_review_state").asText());
        assertTrue(
                state.path("slots")
                        .get(0)
                        .path("card")
                        .path("identity")
                        .path("title")
                        .asText()
                        .contains("专辑"));
        assertEquals(expiry, state.path("expires_at").asText());
        assertEquals(revision, state.path("revision").asLong());
        b.connect();
        assertEquals(
                "FAILED",
                b.state.path("slots").get(0).path("card").path("long_review_state").asText());
        assertEquals(expiry, b.state.path("expires_at").asText());
    }

    @org.junit.jupiter.params.ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(strings = {"HEARD", "REPLACE"})
    void undoRestoresLastCardAndFeedbackAndSurvivesLateSupply(String action) throws Exception {
        var b = browser();
        b.submit(userInput(""), "undo-initial");
        b.awaitState(state -> state.path("status").asText().equals("SUCCEEDED"));
        for (var job : peer.jobs.values()) job.done.get(10, java.util.concurrent.TimeUnit.SECONDS);
        b.refresh();
        String original = b.state.path("slots").get(0).path("card").path("candidate_id").asText();
        peer.hold = true;
        try {
            var changed = b.action("cards/slot-0001/actions", action, "undo-change");
            assertTrue(Set.of(200, 202).contains(changed.status()), changed.body().toString());
            b.refresh();
            var body =
                    JSON.createObjectNode()
                            .put("expected_revision", b.state.path("revision").asLong());
            String endpoint = "/v1/discovery-sessions/" + b.session() + "/undo-last-card";
            var undone = b.request("POST", endpoint, body, "undo-once");
            // Restoring the card does not cancel an already queued supply intent.
            assertTrue(Set.of(200, 202).contains(undone.status()), undone.body().toString());
            assertEquals(
                    original,
                    b.state.path("slots").get(0).path("card").path("candidate_id").asText());
            assertEquals(
                    0,
                    jdbc.queryForObject(
                            "SELECT count(*) FROM discovery.heard_album WHERE subject_id=(SELECT subject_id FROM discovery.session WHERE id=?)",
                            Integer.class,
                            b.session()));
            var replay = b.request("POST", endpoint, body, "undo-once");
            assertEquals(undone.body(), replay.body());
            assertEquals(400, b.action("undo-last-card", null, "undo-again").status());
        } finally {
            peer.hold = false;
        }
        for (var job : peer.jobs.values()) job.done.get(10, java.util.concurrent.TimeUnit.SECONDS);
        b.refresh();
        assertEquals(
                original, b.state.path("slots").get(0).path("card").path("candidate_id").asText());
    }

    @org.junit.jupiter.params.ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(ints = {1, 5})
    void exhaustedContinuationContinuesThePendingBatchIntoANewRound(int sequence) throws Exception {
        var b = browser();
        var originalSubmission =
                JSON.createObjectNode().putNull("expected_session_id").put("expected_revision", 0);
        originalSubmission.set("input", userInput(""));
        b.request("POST", "/v1/discovery-sessions", originalSubmission, "drained-initial");
        b.awaitState(s -> s.path("status").asText().equals("SUCCEEDED"));
        String originalRound = b.state.path("round_id").asText();
        for (var job : peer.jobs.values()) job.done.get(10, java.util.concurrent.TimeUnit.SECONDS);
        jdbc.update(
                "UPDATE discovery.publication_round SET supply_state='SUFFICIENT' WHERE round_id=?",
                originalRound);
        jdbc.update(
                "UPDATE discovery.recommendation_round SET round_seq=? WHERE id=?",
                sequence,
                originalRound);
        peer.exhaustSupplyNext.set(true);
        b.refresh();
        var response = b.action("next-batch", null, "drained-batch");
        assertEquals(202, response.status(), response.body().toString());
        if (sequence == 5) {
            b.awaitState(s -> s.path("supply_state").asText().equals("FINAL_EXHAUSTED"));
            assertEquals("NO_MATCH", b.state.path("status").asText());
            assertEquals(originalRound, b.state.path("round_id").asText());
            assertTrue(
                    java.util.stream.StreamSupport.stream(
                                    b.state.path("slots").spliterator(), false)
                            .allMatch(slot -> slot.path("status").asText().equals("EMPTY")));
            assertTrue(peer.failures.isEmpty(), peer.failures.toString());
            return;
        }
        b.awaitState(
                s ->
                        !s.path("round_id").asText().equals(originalRound)
                                && s.path("status").asText().equals("SUCCEEDED"));
        assertEquals(
                2,
                java.util.stream.StreamSupport.stream(b.state.path("slots").spliterator(), false)
                        .filter(slot -> slot.path("status").asText().equals("READY"))
                        .count());
        var currentTask = b.state.deepCopy();
        var oldSubmission =
                b.request("POST", "/v1/discovery-sessions", originalSubmission, "drained-initial");
        assertEquals(400, oldSubmission.status(), oldSubmission.body().toString());
        assertEquals("RESOURCE_UNAVAILABLE", oldSubmission.body().path("code").asText());
        assertTrue(oldSubmission.body().path("idempotency_consumed").asBoolean());
        assertFalse(oldSubmission.body().path("retryable").asBoolean());
        assertEquals("STOP", oldSubmission.body().path("retry_disposition").asText());
        assertFalse(oldSubmission.body().has("run_id"));
        b.refresh();
        assertEquals(currentTask.path("round_id"), b.state.path("round_id"));
        assertEquals(currentTask.path("run_id"), b.state.path("run_id"));
        assertTrue(peer.failures.isEmpty(), peer.failures.toString());
    }

    @Test
    void explicitThresholdAlsoFiltersTheOtherwiseExemptCuratedPool() throws Exception {
        var b = browser();
        var input = userInput("").put("audience_min_listeners", 500);
        input.putArray("liked_genres");
        input.putArray("disliked_genre_ids").add(ROCK);
        assertTrue(Set.of(200, 202).contains(b.submit(input, "curated-500").status()));
        b.awaitState(s -> s.path("status").asText().equals("NO_MATCH"));
        input.put("audience_min_listeners", 0).put("audience_max_listeners", 10);
        assertTrue(Set.of(200, 202).contains(b.submit(input, "curated-0-10").status()));
        b.awaitState(s -> s.path("status").asText().equals("NO_MATCH"));
        input.put("audience_max_listeners", 1000);
        assertTrue(Set.of(200, 202).contains(b.submit(input, "curated-75").status()));
        b.awaitState(s -> s.path("status").asText().equals("SUCCEEDED"));
        assertEquals("CURATED", b.state.path("analysis_mode").asText());
    }

    @Test
    void requestedThresholdSurvivesRestoreAndCanBeChanged() throws Exception {
        var b = browser();
        var input =
                userInput("").put("audience_min_listeners", 75).put("audience_max_listeners", 500);
        assertEquals(202, b.submit(input, "threshold-75").status());
        b.awaitState(s -> s.path("status").asText().equals("SUCCEEDED"));
        b.connect();
        assertEquals(75, b.state.path("editable_input").path("audience_min_listeners").asInt());
        assertEquals(500, b.state.path("editable_input").path("audience_max_listeners").asInt());
        input.put("audience_min_listeners", 500);
        input.put("audience_max_listeners", 499);
        assertEquals(400, b.submit(input, "inverted-audience-range").status());
        input.remove("audience_max_listeners");
        assertEquals(202, b.submit(input, "threshold-500").status());
        b.awaitState(s -> s.path("status").asText().equals("NO_MATCH"));
        assertEquals(500, b.state.path("editable_input").path("audience_min_listeners").asInt());
        input.put("audience_min_listeners", -1);
        assertEquals(400, b.submit(input, "negative-audience-minimum").status());
    }

    @Test
    void changingOnlyConfigurationValueRecomputesDigestAndChangesDefault() throws Exception {
        var changed = settings.deepCopy();
        ((com.fasterxml.jackson.databind.node.ObjectNode) changed.path("agent_policy"))
                .put("audience_min_listeners", 500);
        try {
            java.nio.file.Files.writeString(configFile, changed.toPrettyString());
            bootProduct();
            var b = browser();
            assertEquals(202, b.submit(userInput(""), "configured-500").status());
            b.awaitState(s -> s.path("status").asText().equals("NO_MATCH"));
        } finally {
            java.nio.file.Files.writeString(configFile, settings.toPrettyString());
            bootProduct();
        }
    }

    @Test
    void rapidLongReviewOpensFollowTheirGeneratingRunDuringBatchSupply() throws Exception {
        bootProduct(Map.of("auralis.dispatch-execution-seconds", 180));
        peer.holdLongs = true;
        try {
            var b = browser();
            b.submit(userInput(""), "rapid-open-initial");
            var initial =
                    b.awaitState(
                            s ->
                                    s.path("slots").size() >= 2
                                            && s.path("slots")
                                                    .get(1)
                                                    .path("card")
                                                    .path("long_review_state")
                                                    .asText()
                                                    .equals("GENERATING"));
            String generatingRun = initial.path("run_id").asText();
            var first = initial.path("slots").get(0).path("card").path("candidate_id").asText();
            var second = initial.path("slots").get(1).path("card").path("candidate_id").asText();
            var window =
                    jdbc.queryForObject(
                            "SELECT extract(epoch FROM technical_deadline-accepted_at)::int FROM discovery.discovery_intent WHERE id=?",
                            Integer.class,
                            generatingRun);
            assertEquals(180, window);

            peer.hold = true;
            var batch = b.action("next-batch", null, "rapid-open-supply");
            assertEquals(202, batch.status(), batch.body().toString());
            b.awaitState(
                    s ->
                            jdbc.queryForObject(
                                            "SELECT count(*) FROM discovery.execution WHERE session_id=? AND physical_released_at IS NULL",
                                            Integer.class,
                                            b.session())
                                    == 2);

            var stale =
                    JSON.createObjectNode()
                            .put("expected_revision", initial.path("revision").asLong());
            String reviews = "/v1/discovery-sessions/" + b.session() + "/long-reviews/";
            var opened = b.request("POST", reviews + first + "/open", stale, "rapid-open-first");
            assertEquals(
                    "GENERATING", opened.body().path("status").asText(), opened.body().toString());
            opened = b.request("POST", reviews + second + "/open", stale, "rapid-open-second");
            assertEquals(
                    "GENERATING", opened.body().path("status").asText(), opened.body().toString());
            assertEquals(
                    List.of(generatingRun, generatingRun),
                    jdbc.queryForList(
                            "SELECT e.run_id FROM discovery.execution_command c JOIN discovery.execution e ON e.id=c.execution_id WHERE e.session_id=? AND c.payload->>'kind'='PRIORITIZE_LONG_REVIEW'",
                            String.class,
                            b.session()));

            peer.holdLongs = false;
            var initialJob =
                    peer.jobs.values().stream()
                            .filter(j -> j.start.path("run_id").asText().equals(generatingRun))
                            .findFirst()
                            .orElseThrow();
            initialJob.done.get(10, java.util.concurrent.TimeUnit.SECONDS);
            for (String candidate : List.of(first, second)) {
                var ready = b.request("GET", reviews + candidate, null, null);
                assertEquals(
                        "READY", ready.body().path("status").asText(), ready.body().toString());
            }
        } finally {
            peer.hold = false;
            peer.holdLongs = false;
            bootProduct();
        }
    }

    @Test
    void existingSessionRegenerationUsesUpdatedTokenLimitsAfterRestart() throws Exception {
        peer.failLongNext.set(true);
        var b = browser();
        b.submit(userInput(""), "before-limit-update");
        b.awaitState(
                s ->
                        s.path("slots")
                                .get(0)
                                .path("card")
                                .path("long_review_state")
                                .asText()
                                .equals("INTERRUPTED"));
        String candidate = b.state.path("slots").get(0).path("card").path("candidate_id").asText();
        var changed = settings.deepCopy();
        changed.path("model_bindings")
                .forEach(
                        binding ->
                                ((ObjectNode) binding)
                                        .put("max_input_tokens", 4000)
                                        .put("max_output_tokens", 2000));
        ((ObjectNode) changed.path("prices").path("fixture"))
                .put("maxInput", 4000)
                .put("maxOutput", 2000);
        try {
            java.nio.file.Files.writeString(configFile, changed.toPrettyString());
            bootProduct();
            assertEquals(
                    200,
                    b.action(
                                    "long-reviews/" + candidate + "/regenerate",
                                    null,
                                    "after-limit-update")
                            .status());
            b.awaitState(
                    s ->
                            s.path("slots")
                                    .get(0)
                                    .path("card")
                                    .path("long_review_state")
                                    .asText()
                                    .equals("READY"));
            var payload =
                    JSON.readTree(
                            jdbc.queryForObject(
                                    "SELECT e.start_payload::text FROM discovery.execution e JOIN discovery.discovery_intent i ON i.id=e.run_id WHERE e.session_id=? AND i.operation='RERUN' ORDER BY e.sent_at DESC LIMIT 1",
                                    String.class,
                                    b.state.path("session_id").asText()));
            assertEquals(
                    4000,
                    payload.path("model_bindings").path("screen").path("max_input_tokens").asInt());
            assertEquals(
                    2000,
                    payload.path("model_bindings")
                            .path("screen")
                            .path("max_output_tokens")
                            .asInt());
            assertEquals(
                    1000000,
                    payload.path("budget").path("limits").path("session_cap_microusd").asLong());
        } finally {
            java.nio.file.Files.writeString(configFile, settings.toPrettyString());
            bootProduct();
        }
    }

    @org.junit.jupiter.params.ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(strings = {"zh-CN", "en"})
    void interruptedPhysicalExecutionCanRegenerateLongReviewExplicitly(String language)
            throws Exception {
        peer.failLongNext.set(true);
        var b = browser();
        b.submit(userInput("").put("language", language), "interrupted-long");
        b.awaitState(
                s ->
                        s.path("slots")
                                .get(0)
                                .path("card")
                                .path("long_review_state")
                                .asText()
                                .equals("INTERRUPTED"));
        String candidate = b.state.path("slots").get(0).path("card").path("candidate_id").asText();
        int calls = peer.modelCalls.get();
        b.refresh();
        assertEquals(calls, peer.modelCalls.get());
        var result = b.action("long-reviews/" + candidate + "/regenerate", null, "explicit-retry");
        assertEquals(200, result.status(), result.body().toString());
        b.awaitState(
                s ->
                        s.path("slots")
                                .get(0)
                                .path("card")
                                .path("long_review_state")
                                .asText()
                                .equals("READY"));
        assertTrue(peer.modelCalls.get() > calls);
        var rerun =
                peer.jobs.values().stream()
                        .filter(j -> j.start.path("session_id").equals(b.state.path("session_id")))
                        .filter(j -> j.start.path("run_kind").asText().equals("RERUN"))
                        .findFirst()
                        .orElseThrow();
        assertTrue(rerun.start.path("input").path("raw").isNull());
        assertEquals(
                language.equals("en") ? "en" : "",
                rerun.start.path("input").path("language").asText());
        if (language.equals("en"))
            assertTrue(
                    b.state
                            .path("slots")
                            .get(0)
                            .path("card")
                            .path("short_review")
                            .path("text")
                            .asText()
                            .startsWith("Piano and quiet drums"));
    }

    @Test
    void completedCardsAndReviewsSurviveJavaRestartWithoutDispatch() throws Exception {
        var b = browser();
        assertEquals(202, b.submit(userInput("恢复原值"), "s".repeat(160)).status());
        var ready =
                b.awaitState(
                        s ->
                                java.util.stream.StreamSupport.stream(
                                                s.path("slots").spliterator(), false)
                                        .allMatch(
                                                slot ->
                                                        slot.path("card")
                                                                .path("long_review_state")
                                                                .asText()
                                                                .equals("READY")));
        long until = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(5);
        while (!app.getBean(io.auralisy.discovery.api.DispatchQueue.class).active().isEmpty()
                && System.nanoTime() < until) Thread.sleep(25);
        int sends = peer.sends.get();
        bootProduct();
        b.connect();
        assertEquals(ready.path("slots"), b.state.path("slots"));
        assertEquals(ready.path("editable_input"), b.state.path("editable_input"));
        assertEquals(ready.path("expires_at"), b.state.path("expires_at"));
        assertEquals(sends, peer.sends.get());
        String candidate = b.state.path("slots").get(0).path("card").path("candidate_id").asText();
        assertEquals(
                "READY",
                b.request(
                                "GET",
                                "/v1/discovery-sessions/"
                                        + b.session()
                                        + "/long-reviews/"
                                        + candidate,
                                null,
                                null)
                        .body()
                        .path("status")
                        .asText());
    }

    @Test
    void foreignSessionAndStaleRevisionCannotModifyCards() throws Exception {
        var owner = browser();
        owner.submit(userInput(""), "owner");
        owner.awaitState(s -> s.path("status").asText().equals("SUCCEEDED"));
        var foreign = browser();
        String path = "/v1/discovery-sessions/" + owner.session();
        assertEquals(400, foreign.request("GET", path, null, null).status());
        var body =
                JSON.createObjectNode()
                        .put("expected_revision", owner.state.path("revision").asLong())
                        .put("action", "HEARD");
        assertEquals(
                400,
                foreign.request("POST", path + "/cards/slot-0001/actions", body, "foreign")
                        .status());
        body.put("expected_revision", 0);
        assertEquals(
                409,
                owner.request("POST", path + "/cards/slot-0001/actions", body, "stale").status());
        assertEquals(
                0,
                jdbc.queryForObject(
                        "SELECT count(*) FROM discovery.heard_album WHERE subject_id=(SELECT subject_id FROM discovery.session WHERE id=?)",
                        Integer.class,
                        owner.session()));
    }

    @Test
    void submitRestoreReuseAndExplicitRegenerateUseRealHttp() throws Exception {
        var b = browser();
        var input = userInput("保留原文用于编辑预填");
        var response = b.submit(input, "initial");
        assertEquals(202, response.status(), response.body().toString());
        var ready =
                b.awaitState(
                        s ->
                                s.path("status").asText().equals("SUCCEEDED")
                                        && java.util.stream.StreamSupport.stream(
                                                        s.path("slots").spliterator(), false)
                                                .allMatch(
                                                        slot ->
                                                                slot.path("card")
                                                                        .path("long_review_state")
                                                                        .asText()
                                                                        .equals("READY")));
        assertEquals(input, ready.path("editable_input"));
        assertEquals(2, ready.path("recommendation_target_count").asInt());
        String expires = ready.path("expires_at").asText();
        b.refresh();
        assertEquals(expires, b.state.path("expires_at").asText());
        int sends = peer.sends.get();
        assertEquals(200, b.submit(input, "reuse").status());
        assertEquals(sends, peer.sends.get());
        assertTrue(
                Instant.parse(b.state.path("expires_at").asText()).isAfter(Instant.parse(expires)));
        String candidate = b.state.path("slots").get(0).path("card").path("candidate_id").asText();
        int calls = peer.modelCalls.get();
        assertEquals(200, b.action("long-reviews/" + candidate + "/open", null, "open").status());
        b.refresh();
        assertEquals(calls, peer.modelCalls.get());
        assertEquals(
                200,
                b.action("long-reviews/" + candidate + "/regenerate", null, "regenerate").status());
        b.awaitState(
                s ->
                        peer.modelCalls.get() > calls
                                && s.path("slots")
                                        .get(0)
                                        .path("card")
                                        .path("long_review_state")
                                        .asText()
                                        .equals("READY"));
        assertTrue(peer.failures.isEmpty(), peer.failures.toString());
    }

    @org.junit.jupiter.params.ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(strings = {"zh-CN", "en"})
    void replacementSupplyThenCacheAndNextRoundKeepHistoryAndNoRawReplay(String language)
            throws Exception {
        peer.holdReports = true;
        var b = browser();
        b.submit(userInput("原文只发送首轮").put("language", language), "start");
        b.awaitState(s -> s.path("status").asText().equals("SUCCEEDED"));
        String firstRound = b.state.path("round_id").asText();
        String first = b.state.path("slots").get(0).path("card").path("candidate_id").asText();
        assertEquals(202, b.action("cards/slot-0001/actions", "HEARD", "heard").status());
        b.awaitState(
                s ->
                        s.path("slots").get(0).path("status").asText().equals("READY")
                                && !s.path("slots")
                                        .get(0)
                                        .path("card")
                                        .path("candidate_id")
                                        .asText()
                                        .equals(first));
        b.awaitState(
                s ->
                        s.path("slots")
                                .get(0)
                                .path("card")
                                .path("long_review_state")
                                .asText()
                                .equals("READY"));
        int sends = peer.sends.get();
        assertEquals(200, b.action("cards/slot-0002/actions", "REPLACE", "cached").status());
        assertEquals(sends, peer.sends.get());
        b.refresh();
        peer.hold = true;
        var advanced = b.action("next-batch", null, "next-round");
        assertEquals(202, advanced.status(), advanced.body().toString());
        peer.holdReports = false;
        long drained = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(5);
        while (peer.jobs.values().stream()
                        .anyMatch(
                                j ->
                                        j.start.path("round_id").asText().equals(firstRound)
                                                && !j.released)
                && System.nanoTime() < drained) Thread.sleep(25);
        assertTrue(
                java.util.stream.StreamSupport.stream(
                                b.refresh().path("slots").spliterator(), false)
                        .allMatch(slot -> slot.path("status").asText().equals("SUPPLYING")));
        peer.hold = false;
        var next =
                b.awaitState(
                        s ->
                                !s.path("round_id").asText().equals(firstRound)
                                        && s.path("status").asText().equals("SUCCEEDED"));
        assertTrue(
                peer.jobs.values().stream()
                        .filter(j -> j.start.path("round_id").equals(next.path("round_id")))
                        .allMatch(
                                j ->
                                        j.start.path("input").path("raw").isNull()
                                                && j.start
                                                        .path("input")
                                                        .path("language")
                                                        .asText()
                                                        .equals(
                                                                language.equals("en")
                                                                        ? "en"
                                                                        : "")));
        assertTrue(
                peer.jobs.values().stream()
                        .anyMatch(
                                j ->
                                        j.start.path("round_id").equals(next.path("round_id"))
                                                && j.start
                                                        .path("input")
                                                        .path("exclusions")
                                                        .toString()
                                                        .contains(first)));
        assertEquals(
                1,
                tx.write(
                                () ->
                                        sessions.heardAlbums(
                                                sessions.current(
                                                                subjects.authenticate(b.token())
                                                                        .orElseThrow()
                                                                        .subjectId())
                                                        .orElseThrow()
                                                        .scope()))
                        .size());
        assertTrue(peer.failures.isEmpty(), peer.failures.toString());
    }

    @Test
    void basicRescreenUsesUnshownResultsAndNeverRunsAutomatically() throws Exception {
        peer.basicNext.set(true);
        var b = browser();
        b.submit(userInput(""), "basic");
        b.awaitState(s -> s.path("analysis_mode").asText().equals("BASIC"));
        var old = new HashSet<String>();
        b.state.path("slots").forEach(s -> old.add(s.path("card").path("candidate_id").asText()));
        int sends = peer.sends.get();
        b.refresh();
        assertEquals(sends, peer.sends.get());
        assertEquals(202, b.action("rescreen", null, "rescreen").status());
        var next =
                b.awaitState(
                        s ->
                                s.path("analysis_mode").asText().equals("PERSONALIZED")
                                        && s.path("status").asText().equals("SUCCEEDED"));
        for (var slot : next.path("slots"))
            assertFalse(old.contains(slot.path("card").path("candidate_id").asText()));
        assertTrue(peer.failures.isEmpty(), peer.failures.toString());
    }

    @Test
    void queueFullRollsBackInputAndSameKeyCanBeRetried() throws Exception {
        peer.hold = true;
        for (int n = 0; n < 3; n++) {
            var b = browser();
            assertEquals(202, b.submit(userInput(""), "running-" + n).status());
            b.awaitState(s -> s.path("status").asText().equals("RUNNING"));
        }
        for (int n = 0; n < 5; n++) {
            var b = browser();
            assertEquals(202, b.submit(userInput(""), "queued-" + n).status());
        }
        var refused = browser();
        var response = refused.submit(userInput(""), "retry");
        assertEquals(429, response.status());
        assertEquals("QUEUE_FULL", response.body().path("code").asText());
        assertFalse(response.body().path("idempotency_consumed").asBoolean());
        assertTrue(refused.refresh().path("session_id").isNull());
        peer.hold = false;
        long until = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(10);
        Reply retried;
        do {
            Thread.sleep(100);
            retried = refused.submit(userInput(""), "retry");
        } while (retried.status() == 429 && System.nanoTime() < until);
        assertTrue(Set.of(200, 202).contains(retried.status()), retried.body().toString());
    }

    @Test
    void noPositiveUsesInjectedFixedListAndModeDoesNotChooseDifferentAlbums() throws Exception {
        var b = browser();
        var input = userInput("");
        input.putArray("liked_genres");
        input.putArray("disliked_genre_ids").add(ROCK);
        var submitted = b.submit(input, "curated");
        assertTrue(Set.of(200, 202).contains(submitted.status()), submitted.body().toString());
        b.awaitState(s -> s.path("status").asText().equals("SUCCEEDED"));
        assertEquals("CURATED", b.state.path("analysis_mode").asText());
        assertEquals("SUFFICIENT", b.state.path("supply_state").asText());
        assertTrue(
                peer.jobs.values().stream()
                        .noneMatch(j -> j.start.path("session_id").asText().equals(b.session())));
        for (var slot : b.state.path("slots"))
            assertEquals("READY", slot.path("card").path("long_review_state").asText());
        assertTrue(peer.failures.isEmpty(), peer.failures.toString());
    }

    @Test
    void curatedModesKeepOrderAndAllExcludedProducesNoMatch() throws Exception {
        int sends = peer.sends.get();
        for (String mode : List.of("familiar", "explore", "surprise")) {
            var b = browser();
            var input = userInput("");
            input.put("mode", mode);
            input.putArray("liked_genres");
            input.putArray("disliked_genre_ids").add(ROCK);
            assertEquals(200, b.submit(input, "mode-" + mode).status());
            assertEquals(
                    album(1),
                    b.state.path("slots").get(0).path("card").path("candidate_id").asText());
            assertEquals(
                    album(2),
                    b.state.path("slots").get(1).path("card").path("candidate_id").asText());
        }
        var empty = browser();
        var input = userInput("");
        input.putArray("liked_genres");
        input.putArray("disliked_genre_ids").add(G);
        assertEquals(200, empty.submit(input, "all-excluded").status());
        assertEquals("NO_MATCH", empty.state.path("status").asText());
        assertEquals("FINAL_EXHAUSTED", empty.state.path("supply_state").asText());
        assertEquals(sends, peer.sends.get());
    }

    @Test
    void preparedCuratedBatchExhaustsAndRestoresWithoutPython() throws Exception {
        int sends = peer.sends.get();
        var b = browser();
        var input = userInput("");
        input.putArray("liked_genres");
        input.putArray("disliked_genre_ids").add(ROCK);
        assertEquals(200, b.submit(input, "prepared").status());
        assertEquals(
                album(1), b.state.path("slots").get(0).path("card").path("candidate_id").asText());
        assertEquals(200, b.action("cards/slot-0001/actions", "HEARD", "heard").status());
        assertEquals(
                album(3), b.state.path("slots").get(0).path("card").path("candidate_id").asText());
        bootProduct();
        b.connect();
        assertEquals(
                album(3), b.state.path("slots").get(0).path("card").path("candidate_id").asText());
        var batch = b.action("next-batch", null, "batch");
        assertEquals(200, batch.status(), batch.body().toString());
        assertEquals("PARTIAL", b.state.path("status").asText());
        assertEquals(
                album(4), b.state.path("slots").get(0).path("card").path("candidate_id").asText());
        assertEquals(200, b.action("cards/slot-0001/actions", "REPLACE", "last").status());
        assertEquals("NO_MATCH", b.state.path("status").asText());
        assertEquals("FINAL_EXHAUSTED", b.state.path("supply_state").asText());
        assertEquals(sends, peer.sends.get());
    }

    @Test
    void parsedNoPositiveHandsOffToPreparedContentWithoutSecondExecution() throws Exception {
        var b = browser();
        var input = userInput("不要摇滚");
        input.putArray("liked_genres");
        input.putArray("disliked_genre_ids").add(ROCK);
        int sends = peer.sends.get();
        assertEquals(202, b.submit(input, "parsed-curated").status());
        b.awaitState(s -> s.path("status").asText().equals("SUCCEEDED"));
        assertEquals("CURATED", b.state.path("analysis_mode").asText());
        assertEquals(sends + 1, peer.sends.get());
        assertTrue(
                peer.jobs.values().stream()
                        .noneMatch(
                                j -> j.start.path("operation").asText().equals("CURATED_REVIEWS")));
        assertTrue(peer.failures.isEmpty(), peer.failures.toString());
    }

    @Test
    void editingCreatesNewSessionAndDuplicateSubmissionDoesNotDispatchAgain() throws Exception {
        var b = browser();
        b.submit(userInput(""), "one");
        b.awaitState(s -> s.path("status").asText().equals("SUCCEEDED"));
        String old = b.session();
        var input = userInput("新的文字");
        var body =
                JSON.createObjectNode()
                        .put("expected_session_id", old)
                        .put("expected_revision", b.state.path("revision").asLong());
        body.set("input", input);
        var first = b.request("POST", "/v1/discovery-sessions", body, "edit");
        assertEquals(202, first.status());
        String current = b.session();
        assertNotEquals(old, current);
        var replay = b.request("POST", "/v1/discovery-sessions", body, "edit");
        assertEquals(current, replay.body().path("session_id").asText());
        assertEquals(400, b.request("GET", "/v1/discovery-sessions/" + old, null, null).status());
    }
}
