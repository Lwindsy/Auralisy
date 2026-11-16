package io.auralisy.discovery.api;

import io.auralisy.session.api.SessionStore.Scope;
import java.time.Instant;
import java.util.List;

public interface DiscoveryStore {
    record Waiting(
            String runId,
            String sessionId,
            String subjectId,
            Instant acceptedAt,
            Instant deadline) {}

    /** Reentrant shared lifecycle barrier; ordinary subjects do not exclude each other. */
    void lockRuntime();

    void createRound(
            Scope scope,
            String id,
            int sequence,
            String configuration,
            String recallBinding,
            Instant deadline);

    void createIntent(
            Scope scope,
            String round,
            String run,
            String operation,
            long inputRevision,
            String digest,
            Instant accepted,
            Instant deadline);

    List<Waiting> waiting(int limit);

    void reserveExecution(
            Scope scope,
            String round,
            String run,
            String execution,
            int slot,
            String coordinatorBoot,
            String pythonBoot);

    boolean transitionRun(Scope scope, String run, long revision, String expected, String next);

    boolean releaseExecution(
            Scope scope,
            String execution,
            String coordinatorBoot,
            String pythonBoot,
            Instant physicalRelease);

    record Round(String id, int sequence, String configuration, java.time.Instant deadline) {}

    java.util.Optional<Round> latestRound(Scope scope);

    /** Same ownership and latest-round ordering as latestRound, without loading configuration. */
    java.util.Optional<String> latestRoundId(Scope scope);

    Round roundInfo(Scope scope, String round);

    void configureRound(Scope scope, String round, String configuration);

    String runRound(Scope scope, String run);

    boolean physicalWork(Scope scope, String run);

    void extendDeadline(Scope scope, String round, Instant deadline);
}
