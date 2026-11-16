package io.auralisy.catalog.adapters;

import static io.auralisy.catalog.application.CatalogJson.*;

import com.fasterxml.jackson.databind.JsonNode;
import java.nio.file.*;
import java.util.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * Immutable deployment artifact. Requests select a reference and read a band; no population scan.
 */
@Component
public final class GenreReachIndex {
    private final JsonNode document;
    private final Map<String, JsonNode> profiles = new HashMap<>();
    private final Map<String, Set<String>> ancestors = new HashMap<>();
    private final Set<String> visible = new HashSet<>();

    @org.springframework.beans.factory.annotation.Autowired
    public GenreReachIndex(
            @Value("${AURALIS_GENRE_REACH_INDEX:}") String path, GenreTaxonomy taxonomy)
            throws Exception {
        this(path.isBlank() ? null : JSON.readTree(Files.readAllBytes(Path.of(path))), taxonomy);
    }

    public GenreReachIndex(JsonNode document, GenreTaxonomy taxonomy) {
        this.document = document == null ? null : document.deepCopy();
        if (document == null) return;
        require("GenreReachIndex", document);
        if (!contentDigest(document, "digest").equals(document.path("digest").asText())
                || !document.path("source_manifest_sha256").asText().equals(taxonomy.manifest())
                || !document.path("taxonomy_sha256").asText().equals(taxonomy.digest()))
            throw new IllegalArgumentException("REACH_INDEX_MISMATCH");
        for (var row : document.path("genres")) {
            String id = row.path("genre_id").asText();
            if (profiles.putIfAbsent(id, row) != null)
                throw new IllegalArgumentException("REACH_DUPLICATE_GENRE");
            boolean normal = row.path("mode").asText().equals("NORMAL");
            if (row.path("bands").size() != (normal ? 101 : 0))
                throw new IllegalArgumentException("REACH_BANDS_INVALID");
        }
        if (!profiles.keySet().equals(taxonomy.nodes().keySet()))
            throw new IllegalArgumentException("REACH_GENRES_MISMATCH");
        for (String id : profiles.keySet()) {
            var found = new HashSet<String>();
            var pending = new ArrayDeque<>(taxonomy.nodes().get(id).parents());
            while (!pending.isEmpty()) {
                String parent = pending.removeFirst();
                if (found.add(parent)) pending.addAll(taxonomy.nodes().get(parent).parents());
            }
            ancestors.put(id, Set.copyOf(found));
            if (selectable(id)) {
                visible.add(id);
                visible.addAll(found);
            }
        }
    }

    public boolean enabled() {
        return document != null;
    }

    public String digestValue() {
        return enabled() ? document.path("digest").asText() : null;
    }

    public boolean selectable(String id) {
        return !enabled()
                || profiles.containsKey(id)
                        && !profiles.get(id).path("mode").asText().equals("HIDDEN");
    }

    public boolean visible(String id) {
        return !enabled() || visible.contains(id);
    }

    public void requirePolicy(JsonNode policy) {
        if (!enabled()
                || !digestValue().equals(policy.path("audience_reach_index_digest").asText())
                || !document.path("audience_binding").equals(policy.path("audience_binding")))
            throw new IllegalArgumentException("REACH_INDEX_MISMATCH");
    }

    private Set<String> specific(Collection<String> ids) {
        var result = new HashSet<String>();
        for (String id : ids) if (selectable(id) && profiles.containsKey(id)) result.add(id);
        for (String id : ids) result.removeAll(ancestors.getOrDefault(id, Set.of()));
        return result;
    }

    private Comparator<String> priority(Map<String, Double> weights) {
        return Comparator.<String>comparingDouble(g -> -weights.getOrDefault(g, 0.0))
                .thenComparing(g -> !profiles.get(g).path("mode").asText().equals("NORMAL"))
                .thenComparingDouble(g -> -profiles.get(g).path("lower").asDouble())
                .thenComparingDouble(g -> -profiles.get(g).path("head").asDouble())
                .thenComparing(g -> g);
    }

    public JsonNode proof(Collection<String> tags, JsonNode preferences, JsonNode policy) {
        requirePolicy(policy);
        int position = policy.path("audience_reach_percent").asInt(-1);
        if (position < 0 || position > 100)
            throw new IllegalArgumentException("REACH_POSITION_INVALID");
        var weights = new HashMap<String, Double>();
        var config = policy.path("recall").path("weights");
        for (var tag : preferences.path("tags")) {
            double weight = tag.path("weight").asDouble(1.0);
            if (tag.path("strength").asText().equals("special"))
                weight *= config.path("special").asDouble(2.0);
            if (tag.path("focused").asBoolean()) weight *= config.path("focus").asDouble(2.0);
            weights.merge(tag.path("id").asText(), weight, Math::max);
        }
        var matched = new HashSet<String>();
        for (String id : weights.keySet())
            if (selectable(id)
                    && tags.stream()
                            .anyMatch(
                                    t ->
                                            t.equals(id)
                                                    || ancestors
                                                            .getOrDefault(t, Set.of())
                                                            .contains(id))) matched.add(id);
        var choices = specific(matched.isEmpty() ? tags : matched);
        if (choices.isEmpty()) return null;
        String chosen = choices.stream().min(priority(weights)).orElseThrow();
        var profile = profiles.get(chosen);
        var result =
                JSON.createObjectNode()
                        .put("index_digest", digestValue())
                        .put("genre_id", chosen)
                        .put("mode", profile.path("mode").asText())
                        .put("requested_percent", position);
        if (profile.path("mode").asText().equals("NORMAL")) {
            var band = profile.path("bands").get(position);
            result.set("minimum", band.path("minimum"));
            result.set("maximum", band.path("maximum"));
        } else {
            result.put("minimum", 0).putNull("maximum");
        }
        return result;
    }
}
