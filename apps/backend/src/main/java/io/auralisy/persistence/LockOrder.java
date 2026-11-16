package io.auralisy.persistence;

import java.util.HashSet;
import java.util.Set;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

/** Transaction-local verification only; each owning adapter performs its own SQL lock. */
public final class LockOrder {
    public enum Level {
        RUNTIME,
        SUBJECT,
        SESSION,
        ROUND,
        EXECUTION,
        BUDGET,
        INVOCATION,
        DELIVERY,
        SLOT
    }

    private static final Object KEY = new Object();

    private LockOrder() {}

    public static boolean before(Level level, String id) {
        ShortTransactions.requireWrite();
        var state = (State) TransactionSynchronizationManager.getResource(KEY);
        if (state == null) {
            state = new State();
            TransactionSynchronizationManager.bindResource(KEY, state);
            TransactionSynchronizationManager.registerSynchronization(
                    new TransactionSynchronization() {
                        @Override
                        public void afterCompletion(int status) {
                            TransactionSynchronizationManager.unbindResourceIfPossible(KEY);
                        }
                    });
        }
        String key = level.name() + ":" + id;
        if (state.held.contains(key)) return false;
        if (level == Level.SUBJECT && (state.admission || state.physicalSlot))
            throw new PersistenceRejected("RESOURCE_LOCK_OWNER_CHANGED");
        if (state.level != null
                && (level.ordinal() < state.level.ordinal()
                        || (level == state.level && id.compareTo(state.id) < 0)))
            throw new PersistenceRejected("LOCK_ORDER_VIOLATION");
        state.held.add(key);
        state.level = level;
        state.id = id;
        if (level == Level.SUBJECT) state.subjects.add(id);
        return true;
    }

    /** A capacity decision may follow private locks, but may not expand its owner scope. */
    public static boolean beforeAdmission() {
        var state = ownedResource();
        if (state.physicalSlot) throw new PersistenceRejected("CAPACITY_LOCK_ORDER_VIOLATION");
        if (state.admission) return false;
        state.admission = true;
        return true;
    }

    public static void beforePhysicalSlot() {
        var state = ownedResource();
        if (state.admission) throw new PersistenceRejected("CAPACITY_LOCK_ORDER_VIOLATION");
        state.physicalSlot = true;
    }

    private static State ownedResource() {
        ShortTransactions.requireWrite();
        var state = (State) TransactionSynchronizationManager.getResource(KEY);
        if (state == null || state.subjects.size() != 1)
            throw new PersistenceRejected("RESOURCE_LOCK_OWNER_REQUIRED");
        return state;
    }

    private static final class State {
        final Set<String> held = new HashSet<>();
        final Set<String> subjects = new HashSet<>();
        boolean admission, physicalSlot;
        Level level;
        String id;
    }
}
