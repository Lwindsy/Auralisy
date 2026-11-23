package io.auralisy.discovery;

import static org.junit.jupiter.api.Assertions.*;

import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

@EnabledIfEnvironmentVariable(named = "AURALIS_TEST_POSTGRES_URL", matches = ".+")
class ProgressiveLockDiagnosticsIT {
    @Test
    void originalLongReviewSurvivesSubjectLockWithAndWithoutPolling() throws Exception {
        String url = System.getenv("AURALIS_TEST_POSTGRES_URL");
        assertTrue(url.matches("jdbc:postgresql://127\\.0\\.0\\.1:[0-9]+/auralis_core_verify"));
        String python = System.getenv("AURALIS_RECALL_PYTHON");
        assertNotNull(python, "Set AURALIS_RECALL_PYTHON to the product test interpreter");
        Path java = ProcessHandle.current().info().command().map(Path::of).orElseThrow();
        Path source = Path.of("../intelligence/src").toRealPath();
        Path jar = Path.of("target/auralisy-core-database-0.1.0-SNAPSHOT-online.jar").toRealPath();
        String receiptRoot = System.getenv("AURALIS_LOCK_TEST_RECEIPTS");
        Path outputParent =
                receiptRoot == null
                        ? Files.createTempDirectory(
                                Path.of("target").toRealPath(), "progressive-lock-")
                        : Path.of(receiptRoot).toAbsolutePath();
        Files.createDirectories(outputParent);
        Path output = outputParent.resolve("result");
        Path log = outputParent.resolve("host-forwarded.log");
        var command =
                new ProcessBuilder(
                        python,
                        source.getParent()
                                .resolve("tests/java_lock_diagnostic_probe.py")
                                .toString(),
                        "--pg-port",
                        Integer.toString(URI.create(url.substring(5)).getPort()),
                        "--java",
                        java.toString(),
                        "--jar",
                        jar.toString(),
                        "--output",
                        output.toString());
        command.environment().put("PYTHONPATH", source.toString());
        command.environment().put("PYTHONUTF8", "1");
        command.redirectErrorStream(true).redirectOutput(log.toFile());
        var process = command.start();
        try {
            assertTrue(process.waitFor(420, TimeUnit.SECONDS), "Lock probe timed out: " + log);
            assertEquals(0, process.exitValue(), () -> readLog(log));
            String diagnostics = readLog(log);
            assertTrue(diagnostics.contains("AURALIS_DB_DIAGNOSTIC phase=delivery outcome=failed"));
            assertTrue(diagnostics.contains("AURALIS_DB_LOCK_SNAPSHOT result=ok"));
        } finally {
            if (process.isAlive()) {
                process.descendants().forEach(ProcessHandle::destroyForcibly);
                process.destroyForcibly();
            }
        }
    }

    private static String readLog(Path log) {
        try {
            return Files.readString(log);
        } catch (java.io.IOException unavailable) {
            return log.toString();
        }
    }
}
