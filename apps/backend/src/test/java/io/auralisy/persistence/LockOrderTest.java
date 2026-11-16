package io.auralisy.persistence;

import static org.junit.jupiter.api.Assertions.*;

import org.junit.jupiter.api.*;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

class LockOrderTest {
    @BeforeEach
    void begin() {
        TransactionSynchronizationManager.initSynchronization();
        TransactionSynchronizationManager.setActualTransactionActive(true);
    }

    @AfterEach
    void end() {
        TransactionSynchronizationManager.getSynchronizations()
                .forEach(
                        synchronization ->
                                synchronization.afterCompletion(
                                        TransactionSynchronization.STATUS_ROLLED_BACK));
        TransactionSynchronizationManager.clearSynchronization();
        TransactionSynchronizationManager.setActualTransactionActive(false);
    }

    @Test
    void repeatedGuardsDoNotReversePrivateLockOrder() {
        assertTrue(LockOrder.before(LockOrder.Level.RUNTIME, "singleton"));
        assertTrue(LockOrder.before(LockOrder.Level.SUBJECT, "a"));
        assertFalse(LockOrder.before(LockOrder.Level.RUNTIME, "singleton"));
        assertTrue(LockOrder.before(LockOrder.Level.ROUND, "r"));
        assertThrows(
                PersistenceRejected.class, () -> LockOrder.before(LockOrder.Level.SESSION, "s"));
    }

    @Test
    void admissionIsAnOwnerScopedLeafRatherThanAPrivateRowLevel() {
        assertThrows(PersistenceRejected.class, LockOrder::beforeAdmission);
        LockOrder.before(LockOrder.Level.SUBJECT, "a");
        LockOrder.before(LockOrder.Level.ROUND, "r");
        assertTrue(LockOrder.beforeAdmission());
        assertFalse(LockOrder.beforeAdmission());
        assertDoesNotThrow(() -> LockOrder.before(LockOrder.Level.EXECUTION, "e"));
        assertThrows(PersistenceRejected.class, LockOrder::beforePhysicalSlot);
        assertThrows(
                PersistenceRejected.class, () -> LockOrder.before(LockOrder.Level.SUBJECT, "b"));
    }

    @Test
    void physicalSlotCannotMixWithAdmissionOrChangeOwner() {
        LockOrder.before(LockOrder.Level.SUBJECT, "a");
        LockOrder.beforePhysicalSlot();
        assertDoesNotThrow(() -> LockOrder.before(LockOrder.Level.ROUND, "r"));
        assertThrows(PersistenceRejected.class, LockOrder::beforeAdmission);
        assertThrows(
                PersistenceRejected.class, () -> LockOrder.before(LockOrder.Level.SUBJECT, "b"));
    }

    @Test
    void sharedResourcesRejectTransactionsHoldingMultipleOwners() {
        LockOrder.before(LockOrder.Level.SUBJECT, "a");
        LockOrder.before(LockOrder.Level.SUBJECT, "b");
        assertThrows(PersistenceRejected.class, LockOrder::beforeAdmission);
        assertThrows(PersistenceRejected.class, LockOrder::beforePhysicalSlot);
    }
}
