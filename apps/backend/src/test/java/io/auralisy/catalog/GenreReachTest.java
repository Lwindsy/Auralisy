package io.auralisy.catalog;

import static io.auralisy.catalog.application.CatalogJson.JSON;
import static org.junit.jupiter.api.Assertions.*;

import com.fasterxml.jackson.databind.node.ObjectNode;
import io.auralisy.catalog.adapters.GenreReachIndex;
import io.auralisy.catalog.adapters.GenreTaxonomy;
import io.auralisy.catalog.api.GenreReach;
import java.nio.file.Path;
import java.util.ArrayList;
import org.junit.jupiter.api.Test;

class GenreReachTest {
    static Path fixtures() {
        return Path.of(System.getProperty("basedir", "."))
                .toAbsolutePath()
                .resolveSibling("intelligence")
                .resolve("tests/fixtures/genre-reach");
    }

    @Test
    void consumesPythonDeploymentArtifactAndReferenceCases() throws Exception {
        var root = fixtures();
        var taxonomy = new GenreTaxonomy(root.resolve("taxonomy.json").toString());
        var index = new GenreReachIndex(root.resolve("index.json").toString(), taxonomy);
        assertFalse(index.selectable("empty"));
        assertFalse(index.visible("empty"));
        assertTrue(index.selectable("zero"));
        assertTrue(index.selectable("navigation"));
        for (var row : JSON.readTree(root.resolve("cases.json").toFile())) {
            var genres = new ArrayList<String>();
            row.path("genres").forEach(g -> genres.add(g.asText()));
            var proof = index.proof(genres, row.path("preferences"), row.path("policy"));
            if (row.path("expected").isNull()) {
                assertNull(proof);
                continue;
            }
            assertEquals(row.path("expected"), proof);
            var policy = row.path("policy").deepCopy();
            ((ObjectNode) policy).put("audience_min_listeners", 500);
            long minimum = proof.path("minimum").asLong();
            assertTrue(GenreReach.matches(policy, minimum, proof));
            assertFalse(GenreReach.matches(policy, null, proof));
            if (minimum > 0) assertFalse(GenreReach.matches(policy, minimum - 1, proof));
            if (!proof.path("maximum").isNull())
                assertFalse(GenreReach.matches(policy, proof.path("maximum").asLong() + 1, proof));
        }
    }

    @Test
    void rejectsModifiedIndexAndExecutionBinding() throws Exception {
        var root = fixtures();
        var taxonomy = new GenreTaxonomy(root.resolve("taxonomy.json").toString());
        var document = JSON.readTree(root.resolve("index.json").toFile());
        ((ObjectNode) document).put("recall_snapshot_id", "wrong");
        assertThrows(IllegalArgumentException.class, () -> new GenreReachIndex(document, taxonomy));
        var index = new GenreReachIndex(root.resolve("index.json").toString(), taxonomy);
        var policy = JSON.readTree(root.resolve("cases.json").toFile()).get(0).path("policy");
        ((ObjectNode) policy).put("audience_reach_index_digest", "sha256:" + "0".repeat(64));
        assertThrows(IllegalArgumentException.class, () -> index.requirePolicy(policy));
    }
}
