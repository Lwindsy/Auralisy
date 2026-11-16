package io.auralisy.persistence;

/** Transaction-scoped lifecycle barrier; acquiring it does not require READY state. */
public interface RuntimeBarrier {
    void shared();

    void exclusive();
}
