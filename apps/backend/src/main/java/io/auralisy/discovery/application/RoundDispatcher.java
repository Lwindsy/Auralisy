package io.auralisy.discovery.application;

import static io.auralisy.contracts.CanonicalJson.JSON;
import static io.auralisy.persistence.DbDiagnostics.Phase.*;

import com.fasterxml.jackson.databind.JsonNode;
import io.auralisy.discovery.api.*;
import io.auralisy.persistence.DbDiagnostics;
import io.auralisy.persistence.ShortTransactions;
import java.time.Clock;
import java.time.Instant;

/** One coordinator loop. Network calls never participate in a database transaction. */
public final class RoundDispatcher {
    private static final org.slf4j.Logger LOG =
            org.slf4j.LoggerFactory.getLogger(RoundDispatcher.class);
    private final DispatchQueue queue;
    private final PythonClient python;
    private final DispatchQueue.Limits limits;
    private final String boot;
    private final Clock clock;
    private final DbDiagnostics diagnostics;

    public RoundDispatcher(
            DispatchQueue queue,
            PythonClient python,
            DispatchQueue.Limits limits,
            String boot,
            Clock clock,
            DbDiagnostics diagnostics) {
        this.queue = queue;
        this.python = python;
        this.limits = limits;
        this.boot = boot;
        this.clock = clock;
        this.diagnostics = diagnostics;
        queue.beginRecovery(boot);
    }

    private JsonNode json(String text) {
        try {
            return JSON.readTree(text);
        } catch (java.io.IOException e) {
            throw new PythonClient.Unavailable();
        }
    }

    public synchronized void tick() {
        ShortTransactions.outsideTransaction(
                () -> {
                    var phase = RUNTIME;
                    String execution = null;
                    try {
                        String runtime = python.runtime();
                        if (json(runtime).path("execution_capacity").asInt() != limits.executions())
                            throw new PythonClient.Unavailable();
                        phase = RECONCILE;
                        queue.reconcile(boot, runtime);
                        phase = OBSERVE;
                        for (var job : queue.active()) {
                            execution = job.execution();
                            phase = OBSERVE;
                            try {
                                if (!job.coordinatorBoot().equals(boot))
                                    queue.stop(boot, job, "INTERRUPTED");
                                boolean stopping =
                                        job.cancel()
                                                || !job.coordinatorBoot().equals(boot)
                                                || !job.deadline().isAfter(clock.instant());
                                if (stopping && job.attempts() == 0) {
                                    queue.release(boot, job, "INTERRUPTED");
                                    continue;
                                }
                                if (job.attempts() == 0) {
                                    phase = SEND;
                                    send(job);
                                    continue;
                                }
                                JsonNode p = json(job.payload());
                                phase = ADMISSION;
                                var admission =
                                        json(
                                                python.admission(
                                                        job.execution(),
                                                        job.pythonBoot(),
                                                        p.path("payload_digest").asText(),
                                                        job.deadline().toString()));
                                if (!job.execution().equals(admission.path("execution_id").asText())
                                        || !job.pythonBoot()
                                                .equals(admission.path("boot_id").asText())
                                        || !p.path("payload_digest")
                                                .equals(admission.path("payload_digest")))
                                    throw new PythonClient.Unavailable();
                                phase = OBSERVE;
                                switch (admission.path("status").asText()) {
                                    case "ACTIVE", "TERMINAL" -> {
                                        if (admission.path("execution").isNull())
                                            throw new PythonClient.Unavailable();
                                        if (stopping
                                                && !admission
                                                        .path("execution")
                                                        .path("slot_released")
                                                        .asBoolean())
                                            queue.observe(
                                                    boot,
                                                    job,
                                                    python.cancel(
                                                            job.execution(),
                                                            job.coordinatorBoot(),
                                                            job.pythonBoot(),
                                                            "USER_CANCELLED"));
                                        else
                                            queue.observe(
                                                    boot,
                                                    job,
                                                    admission.path("execution").toString());
                                    }
                                    case "NOT_ACCEPTED", "CLEARED" -> {
                                        String until = admission.path("dedup_until").asText();
                                        if (until.isEmpty()
                                                || !Instant.parse(until).equals(job.deadline())
                                                || !job.deadline().isAfter(clock.instant()))
                                            throw new PythonClient.Unavailable();
                                        if (stopping
                                                || admission
                                                        .path("status")
                                                        .asText()
                                                        .equals("CLEARED")
                                                || job.attempts() >= 2)
                                            queue.release(boot, job, "INTERRUPTED");
                                        else {
                                            phase = SEND;
                                            send(job);
                                        }
                                    }
                                    case "UNKNOWN" -> {
                                        /* Keep the physical slot; absence is not retry permission. */
                                        if (stopping)
                                            queue.observe(
                                                    boot,
                                                    job,
                                                    python.cancel(
                                                            job.execution(),
                                                            job.coordinatorBoot(),
                                                            job.pythonBoot(),
                                                            "USER_CANCELLED"));
                                    }
                                    default -> throw new PythonClient.Unavailable();
                                }
                            } catch (RuntimeException failure) {
                                if (!ShortTransactions.isLockContention(failure)) throw failure;
                                diagnostics.skipped(phase, failure, execution);
                            }
                        }
                        phase = COMMAND;
                        execution = null;
                        if (queue.ready(boot))
                            for (var command : queue.commands()) {
                                execution = command.execution();
                                try {
                                    var accepted =
                                            json(
                                                    python.command(
                                                            command.execution(),
                                                            command.id(),
                                                            command.payload()));
                                    if (!accepted.path("accepted").asBoolean())
                                        throw new PythonClient.Unavailable();
                                    queue.acknowledge(boot, command);
                                } catch (RuntimeException failure) {
                                    if (!ShortTransactions.isLockContention(failure)) throw failure;
                                    diagnostics.skipped(phase, failure, execution);
                                }
                            }
                        phase = CLAIM;
                        execution = null;
                        if (queue.ready(boot)) {
                            var next = queue.claim(boot, limits);
                            if (next != null) {
                                phase = SEND;
                                execution = next.execution();
                                send(next);
                            }
                        }
                    } catch (RuntimeException failure) {
                        // PostgreSQL lock contention is local, not evidence of a lost Python
                        // runtime. The transaction rolled back; retain slots and retry observation
                        // on the next tick without disabling in-flight callbacks.
                        if (ShortTransactions.isLockContention(failure)) {
                            diagnostics.skipped(phase, failure, execution);
                            return null;
                        }
                        if (DbDiagnostics.isDatabaseFailure(failure))
                            diagnostics.failed(phase, failure, execution);
                        LOG.warn(
                                "AURALIS_DISPATCH_FAILED {} sqlstate={}",
                                failure.getClass().getSimpleName(),
                                DbDiagnostics.sqlStates(failure));
                        // A failed observation cannot authorize another execution or reset a paid
                        // attempt.
                        try {
                            queue.unavailable(boot, failure);
                        } catch (RuntimeException unavailable) {
                            diagnostics.failed(UNAVAILABLE, unavailable, null);
                            failure.addSuppressed(unavailable);
                            throw failure;
                        }
                    }
                    return null;
                });
    }

    private void send(DispatchQueue.Job job) {
        if (!queue.markSending(boot, job)) return;
        try {
            queue.observe(boot, job, python.start(job.execution(), job.payload()));
        } catch (PythonClient.Unavailable unavailable) {
            // The PUT may have committed. Check that exact admission before entering maintenance;
            // otherwise valid callbacks can be rejected during the next polling interval.
            var payload = json(job.payload());
            var admission =
                    json(
                            python.admission(
                                    job.execution(),
                                    job.pythonBoot(),
                                    payload.path("payload_digest").asText(),
                                    job.deadline().toString()));
            if (!job.execution().equals(admission.path("execution_id").asText())
                    || !job.pythonBoot().equals(admission.path("boot_id").asText())
                    || !payload.path("payload_digest").equals(admission.path("payload_digest"))
                    || !java.util.Set.of("ACTIVE", "TERMINAL")
                            .contains(admission.path("status").asText())
                    || admission.path("execution").isNull()) throw unavailable;
            queue.observe(boot, job, admission.path("execution").toString());
        }
    }
}
