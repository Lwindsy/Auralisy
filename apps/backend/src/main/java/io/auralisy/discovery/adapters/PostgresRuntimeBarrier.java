package io.auralisy.discovery.adapters;

import io.auralisy.persistence.LockOrder;
import io.auralisy.persistence.PersistenceRejected;
import io.auralisy.persistence.RuntimeBarrier;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

@Repository
public class PostgresRuntimeBarrier implements RuntimeBarrier {
    // Match apps/runtime/deploy.sh. Two-int keys are separate from DispatchLifecycle's
    // single-bigint coordinator lease; the regular lock queue preserves writer priority.
    private static final int LOCK_NAMESPACE = 1096110668;
    private static final int LIFECYCLE_LOCK = 1;
    private final JdbcTemplate jdbc;

    public PostgresRuntimeBarrier(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Override
    public void shared() {
        if (!LockOrder.before(LockOrder.Level.RUNTIME, "singleton")) return;
        jdbc.execute(
                "SELECT pg_advisory_xact_lock_shared("
                        + LOCK_NAMESPACE
                        + ","
                        + LIFECYCLE_LOCK
                        + ")");
    }

    @Override
    public void exclusive() {
        if (!LockOrder.before(LockOrder.Level.RUNTIME, "singleton"))
            throw new PersistenceRejected("LIFECYCLE_LOCK_UPGRADE_FORBIDDEN");
        jdbc.execute("SELECT pg_advisory_xact_lock(" + LOCK_NAMESPACE + "," + LIFECYCLE_LOCK + ")");
    }
}
