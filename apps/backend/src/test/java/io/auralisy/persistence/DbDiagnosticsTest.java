package io.auralisy.persistence;

import static io.auralisy.persistence.DbDiagnostics.Phase.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.zaxxer.hikari.HikariDataSource;
import com.zaxxer.hikari.HikariPoolMXBean;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.sql.SQLException;
import java.time.Clock;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;

class DbDiagnosticsTest {
    @Test
    void formatsOnlyBoundedFieldsAndSqlStates() {
        var sql = new SQLException("private-sql", "55P03");
        for (var state : new String[] {"secret", "57014", "25P02", "08006", "22012", "08000"})
            sql.setNextException(new SQLException("private-parameter", state));
        var wrapped = new IllegalStateException("private-body", sql);
        assertEquals(
                "AURALIS_DB_DIAGNOSTIC phase=delivery outcome=failed "
                        + "exception=IllegalStateException sqlstate=55P03,57014,25P02,08006,22012 execution=-",
                DbDiagnostics.format(DELIVERY, "failed", wrapped, "private/invalid"));
        assertTrue(
                DbDiagnostics.format(
                                DELIVERY, "failed", wrapped, "01234567-89ab-cdef-0123-456789abcdef")
                        .endsWith("execution=01234567-89ab-cdef-0123-456789abcdef"));
        assertFalse(
                ShortTransactions.isLockContention(new SQLException("ordinary cancel", "57014")));
        assertFalse(ShortTransactions.isLockContention(new SQLException("unreachable", "08006")));
        assertFalse(ShortTransactions.isLockContention(new SQLException("aborted", "25P02")));
    }

    @Test
    void throttlesIndependentlyByPhaseForTenSeconds() {
        var clock = mock(Clock.class);
        var diagnostics =
                new DbDiagnostics(mock(HikariDataSource.class), new SimpleMeterRegistry(), clock);
        try {
            when(clock.millis()).thenReturn(0L, 9_999L, 9_999L, 10_000L);
            assertTrue(diagnostics.allowSkippedLog(OBSERVE));
            assertFalse(diagnostics.allowSkippedLog(OBSERVE));
            assertTrue(diagnostics.allowSkippedLog(RECONCILE));
            assertTrue(diagnostics.allowSkippedLog(OBSERVE));
        } finally {
            diagnostics.close();
        }
    }

    @Test
    void busyPoolSkipsSnapshotAndContentionStillCountsEachEvent() throws Exception {
        var pool = mock(HikariDataSource.class);
        var status = mock(HikariPoolMXBean.class);
        var checked = new CountDownLatch(1);
        when(pool.getHikariPoolMXBean()).thenReturn(status);
        when(status.getIdleConnections())
                .thenAnswer(
                        call -> {
                            checked.countDown();
                            return 0;
                        });
        var metrics = new SimpleMeterRegistry();
        try {
            var diagnostics = new DbDiagnostics(pool, metrics, Clock.systemUTC());
            try {
                var conflict = new SQLException("private", "55P03");
                diagnostics.skipped(OBSERVE, conflict, null);
                diagnostics.skipped(OBSERVE, conflict, null);
                assertTrue(checked.await(2, TimeUnit.SECONDS));
                var counter =
                        metrics.counter("auralisy.db.lock_snapshot", "result", "skipped_busy");
                long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
                while (counter.count() == 0 && System.nanoTime() < deadline) Thread.sleep(5);
                assertEquals(1, counter.count());
                assertEquals(
                        2,
                        metrics.counter("auralisy.db.lock_contention", "phase", "observe").count());
                verify(pool, never()).getConnection();
                verify(status, times(1)).getIdleConnections();
            } finally {
                diagnostics.close();
            }
        } finally {
            metrics.close();
        }
    }
}
