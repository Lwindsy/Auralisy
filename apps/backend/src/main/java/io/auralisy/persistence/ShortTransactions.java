package io.auralisy.persistence;

import java.util.function.Supplier;
import org.springframework.stereotype.Component;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Explicit composition boundary; callbacks contain database work only and are never retried here.
 */
@Component
public final class ShortTransactions {
    private final PlatformTransactionManager manager;
    private final RuntimeBarrier runtime;

    public ShortTransactions(PlatformTransactionManager manager, RuntimeBarrier runtime) {
        this.manager = manager;
        this.runtime = runtime;
    }

    public <T> T write(Supplier<T> action) {
        return execute(false, action);
    }

    public <T> T read(Supplier<T> action) {
        return execute(true, action);
    }

    /** Lifecycle transitions start their own outer transaction; shared locks never upgrade. */
    public <T> T writeExclusive(Supplier<T> action) {
        if (TransactionSynchronizationManager.isActualTransactionActive())
            throw new PersistenceRejected("LIFECYCLE_LOCK_UPGRADE_FORBIDDEN");
        var template = new TransactionTemplate(manager);
        template.setTimeout(5);
        return template.execute(
                status -> {
                    runtime.exclusive();
                    return action.get();
                });
    }

    private <T> T execute(boolean readOnly, Supplier<T> action) {
        var template = new TransactionTemplate(manager);
        template.setTimeout(5);
        template.setReadOnly(readOnly);
        return template.execute(
                status -> {
                    if (!readOnly) runtime.shared();
                    return action.get();
                });
    }

    public static void requireWrite() {
        if (!TransactionSynchronizationManager.isActualTransactionActive()
                || TransactionSynchronizationManager.isCurrentTransactionReadOnly())
            throw new PersistenceRejected("WRITE_TRANSACTION_REQUIRED");
    }

    public static <T> T outsideTransaction(Supplier<T> action) {
        if (TransactionSynchronizationManager.isActualTransactionActive())
            throw new PersistenceRejected("EXTERNAL_IO_INSIDE_TRANSACTION");
        return action.get();
    }

    public static boolean isLockContention(Throwable failure) {
        for (Throwable cause = failure; cause != null; cause = cause.getCause())
            if (cause instanceof java.sql.SQLException sql && "55P03".equals(sql.getSQLState()))
                return true;
        return false;
    }
}
