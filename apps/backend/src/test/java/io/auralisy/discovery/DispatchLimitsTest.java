package io.auralisy.discovery;

import static org.junit.jupiter.api.Assertions.*;

import io.auralisy.discovery.api.DispatchQueue;
import org.junit.jupiter.api.Test;

class DispatchLimitsTest {
    @Test
    void supportsMeasuredDeploymentTuningWithinPhysicalCapacity() {
        assertEquals(3, new DispatchQueue.Limits(5, 30, 120).executions());
        assertDoesNotThrow(() -> new DispatchQueue.Limits(16, 20, 120, 300));
        assertThrows(
                IllegalArgumentException.class, () -> new DispatchQueue.Limits(17, 5, 30, 180));
        assertThrows(IllegalArgumentException.class, () -> new DispatchQueue.Limits(0, 5, 30, 180));
        assertThrows(IllegalArgumentException.class, () -> new DispatchQueue.Limits(6, 0, 30, 180));
        assertThrows(IllegalArgumentException.class, () -> new DispatchQueue.Limits(6, 5, 0, 180));
        assertThrows(
                IllegalArgumentException.class, () -> new DispatchQueue.Limits(6, 5, 181, 180));
    }
}
