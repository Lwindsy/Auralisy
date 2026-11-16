package io.auralisy.discovery.api;

/**
 * The submission committed, but its response could not be rendered; the original key is retained.
 */
public final class CommittedSubmissionFailure extends RuntimeException {
    public CommittedSubmissionFailure(String safeCode, RuntimeException cause) {
        super(safeCode, cause);
    }
}
