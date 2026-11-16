package io.auralisy.publication.api;

import io.auralisy.session.api.SessionStore.Scope;
import java.util.Optional;

public interface RecoveryProjection {
    Optional<String> read(Scope scope);

    /** Bound stored view, without runtime overlays; only for callers rebuilding a live view. */
    Optional<String> readStored(Scope scope);

    void save(Scope scope, String state);
}
