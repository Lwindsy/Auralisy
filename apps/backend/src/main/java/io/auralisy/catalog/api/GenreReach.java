package io.auralisy.catalog.api;

import com.fasterxml.jackson.databind.JsonNode;

/** Shared admission predicate; the proof is independently selected from the deployed index. */
public final class GenreReach {
    private GenreReach() {}

    public static boolean matches(JsonNode policy, Long listeners, JsonNode proof) {
        if (listeners == null) return false;
        if (policy.has("audience_reach_percent"))
            return proof != null
                    && proof.path("index_digest")
                            .asText()
                            .equals(policy.path("audience_reach_index_digest").asText())
                    && proof.path("requested_percent").asInt()
                            == policy.path("audience_reach_percent").asInt()
                    && listeners >= proof.path("minimum").asLong()
                    && (proof.path("maximum").isNull()
                            || listeners <= proof.path("maximum").asLong());
        return policy.has("audience_min_listeners")
                && listeners >= policy.path("audience_min_listeners").asLong()
                && listeners <= policy.path("audience_max_listeners").asLong(Long.MAX_VALUE);
    }
}
