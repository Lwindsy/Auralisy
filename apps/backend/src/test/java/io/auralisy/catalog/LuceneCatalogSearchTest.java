package io.auralisy.catalog;

import static org.junit.jupiter.api.Assertions.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.auralisy.catalog.adapters.*;
import io.auralisy.catalog.api.CatalogFailure;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class LuceneCatalogSearchTest {
    @TempDir Path temp;
    static final ObjectMapper JSON = new ObjectMapper();
    static final String ID = "11111111-1111-4111-8111-111111111111",
            OLD = "22222222-2222-4222-8222-222222222222";

    static String hash(byte[] bytes) throws Exception {
        return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
    }

    static String fixture(Path source) throws Exception {
        Files.createDirectories(source);
        var row =
                JSON.createObjectNode()
                        .put("id", ID)
                        .put("title", "Début")
                        .put("artist", "Björk")
                        .put("year", 1993)
                        .put("type", "Album");
        row.putArray("genres");
        row.putArray("aliases").add("比约克 首张专辑").add("!!!").add("🎵");
        row.putArray("redirects").add(OLD);
        return writeRows(source, List.of(row));
    }

    static String writeRows(Path source, List<com.fasterxml.jackson.databind.node.ObjectNode> rows)
            throws Exception {
        Files.createDirectories(source);
        var lines = new StringBuilder();
        for (var row : rows) lines.append(JSON.writeValueAsString(row)).append("\n");
        byte[] bytes = lines.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8);
        Files.write(source.resolve("albums.jsonl"), bytes);
        var manifest =
                JSON.createObjectNode()
                        .put("format_version", 1)
                        .put("scope", "ALL_MUSICBRAINZ_RELEASE_GROUPS")
                        .put("rows", rows.size())
                        .put("albums_sha256", hash(bytes))
                        .put("source_receipt_sha256", "a".repeat(64));
        manifest.putObject("source_tables")
                .putObject("release_group")
                .put("rows", rows.size())
                .put("sha256", "a".repeat(64));
        byte[] m = JSON.writeValueAsBytes(manifest);
        Files.write(source.resolve("manifest.json"), m);
        return hash(m);
    }

    @Test
    void realIndexSearchesUnicodeAccentsArtistAndAliasWithoutNetwork() throws Exception {
        Path source = temp.resolve("source"), index = temp.resolve("index");
        String digest = fixture(source);
        CatalogIndexBuild.build(source, index, digest);
        try (var search = new LuceneCatalogSearch(index, digest)) {
            for (String q : List.of("debut", "Björk", "Bjo", "比约克", "Björk Debut", "!!!", "🎵"))
                assertEquals(ID, search.search(q, 0, 10).albums().getFirst().id().toString(), q);
            assertTrue(search.search("no-such-album", 0, 10).albums().isEmpty());
            assertTrue(search.search("debut", 1, 10).albums().isEmpty());
            assertEquals(ID, search.resolve(UUID.fromString(OLD)).id().toString());
            assertTrue(search.resolve(UUID.fromString(ID)).genres().isEmpty());
            assertEquals(
                    404,
                    assertThrows(CatalogFailure.class, () -> search.resolve(UUID.randomUUID()))
                            .status());
        }
    }

    @Test
    void partialAlbumWordsAndArtistIntentKeepRelevantAlbumsVisible() throws Exception {
        Path source = temp.resolve("radiohead-source"), index = temp.resolve("radiohead-index");
        var rows = new ArrayList<com.fasterxml.jackson.databind.node.ObjectNode>();
        String[][] albums = {
            {"OK Computer", "Radiohead"},
            {"Kid A", "Radiohead"},
            {"Radiohead", "Unrelated Artist"},
            {"OK Computer", "Unrelated Artist"}
        };
        for (int i = 0; i < albums.length; i++) {
            var row =
                    JSON.createObjectNode()
                            .put("id", new UUID(0, i + 1).toString())
                            .put("title", albums[i][0])
                            .put("artist", albums[i][1])
                            .put("year", 1997)
                            .put("type", "Album");
            row.putArray("genres");
            row.putArray("aliases");
            row.putArray("redirects");
            rows.add(row);
        }
        String digest = writeRows(source, rows);
        CatalogIndexBuild.build(source, index, digest);
        try (var search = new LuceneCatalogSearch(index, digest)) {
            for (String q :
                    List.of(
                            "Radiohead O",
                            "Radiohead OK",
                            "Radiohead OK C",
                            "radiohead ok computer")) {
                var found = search.search(q, 0, 10).albums();
                assertEquals(1, found.size(), q);
                assertEquals(new UUID(0, 1), found.getFirst().id(), q);
            }
            var found = search.search("Radiohead", 0, 10).albums();
            assertEquals("Radiohead", found.get(0).artist());
            assertEquals("Radiohead", found.get(1).artist());
            assertEquals("Unrelated Artist", found.get(2).artist());
            assertEquals(
                    found.get(1).id(), search.search("Radiohead", 1, 1).albums().getFirst().id());
            assertTrue(search.search("Radiohead impossible", 0, 10).albums().isEmpty());
        }
        try (var search =
                new LuceneCatalogSearch(
                        index,
                        digest,
                        () ->
                                Map.of(
                                        new UUID(0, 1).toString(),
                                        10000L,
                                        new UUID(0, 2).toString(),
                                        100L))) {
            var found = search.search("Radiohead", 0, 10).albums();
            assertEquals(new UUID(0, 1), found.getFirst().id());
            assertEquals(3, found.size()); // Unknown audience does not exclude other releases.
            assertEquals(
                    found.get(1).id(), search.search("Radiohead", 1, 1).albums().getFirst().id());
            assertEquals(
                    "Unrelated Artist",
                    search.search("Unrelated", 0, 10).albums().getFirst().artist());
        }
    }

    @Test
    void popularityProjectionFollowsSnapshotReplacementAndKeepsUnknownAlbums() throws Exception {
        Path source = temp.resolve("popularity-source"), index = temp.resolve("popularity-index");
        var rows = new ArrayList<com.fasterxml.jackson.databind.node.ObjectNode>();
        for (int i = 1; i <= 3; i++) {
            var row =
                    JSON.createObjectNode()
                            .put("id", new UUID(0, i).toString())
                            .put("title", "Same Title")
                            .put("artist", "Same Artist")
                            .put("year", 2000)
                            .put("type", "Album");
            row.putArray("genres");
            row.putArray("aliases");
            row.putArray("redirects");
            rows.add(row);
        }
        String digest = writeRows(source, rows);
        CatalogIndexBuild.build(source, index, digest);
        String first = new UUID(0, 1).toString(), second = new UUID(0, 2).toString();
        var snapshot =
                new java.util.concurrent.atomic.AtomicReference<Map<String, Long>>(
                        Map.of(first, 10L, second, 100L));
        try (var search = new LuceneCatalogSearch(index, digest, snapshot::get)) {
            assertEquals(second, search.search("Same", 0, 10).albums().getFirst().id().toString());
            snapshot.set(Map.of(first, 200L, second, 0L));
            assertEquals(first, search.search("Same", 0, 10).albums().getFirst().id().toString());
            assertEquals(3, search.search("Same", 0, 10).albums().size());
            try (var executor = java.util.concurrent.Executors.newFixedThreadPool(4)) {
                var jobs = new ArrayList<java.util.concurrent.Callable<String>>();
                for (int i = 0; i < 40; i++)
                    jobs.add(() -> search.search("Same", 0, 1).albums().getFirst().id().toString());
                for (var answer : executor.invokeAll(jobs)) assertEquals(first, answer.get());
            }
            snapshot.set(Map.of());
            assertEquals(
                    List.of(new UUID(0, 1), new UUID(0, 2), new UUID(0, 3)),
                    search.search("Same", 0, 10).albums().stream().map(a -> a.id()).toList());
        }
    }

    @Test
    void changedInputAndUnfinishedIndexAreRejectedAndNeverOverwriteExistingData() throws Exception {
        Path source = temp.resolve("source"), index = temp.resolve("index");
        String digest = fixture(source);
        Files.writeString(source.resolve("albums.jsonl"), "\n", StandardOpenOption.APPEND);
        assertThrows(Exception.class, () -> CatalogIndexBuild.build(source, index, digest));
        assertFalse(Files.exists(index.resolve("manifest.sha256")));
        assertThrows(Exception.class, () -> new LuceneCatalogSearch(index, digest));
        assertThrows(Exception.class, () -> CatalogIndexBuild.build(source, index, digest));
    }

    @Test
    void manifestMismatchAndWrongBoundSnapshotFailBeforeServing() throws Exception {
        Path source = temp.resolve("source"), index = temp.resolve("index");
        String digest = fixture(source);
        assertThrows(Exception.class, () -> CatalogIndexBuild.build(source, index, "0".repeat(64)));
        assertFalse(Files.exists(index));
        CatalogIndexBuild.build(source, index, digest);
        assertThrows(Exception.class, () -> new LuceneCatalogSearch(index, "0".repeat(64)));
    }
}
