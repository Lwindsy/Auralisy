import { expect, test } from "vitest";
import type { AlbumSearchResult, ResolvedAlbum } from "../../src/api/types.ts";
import { albumSearchLabels } from "../../src/components/entry/albumSearchLabels.ts";
import { shared } from "../fixtures.ts";

test("search labels translate known MusicBrainz types and preserve the original comment", () => {
  const album = shared<AlbumSearchResult>("search-with-disambiguation")
    .items[0]!;
  const zh = albumSearchLabels(album, "zh-CN", "年份未知");
  expect(zh.details).toBe("1988 · Album · 现场 · New type · Live at the venue");
  expect(zh.name).toBe(
    "Synthetic 外部专辑 — Synthetic Artist · 1988 · Album · 现场 · New type · Live at the venue",
  );
  expect(albumSearchLabels(album, "en", "Year unknown").details).toBe(
    "1988 · Album · Live · New type · Live at the venue",
  );
});

test("null, empty and missing disambiguation add no extra separators", () => {
  const row = shared<ResolvedAlbum>("outside-core-with-disambiguation").album;
  const legacy = shared<ResolvedAlbum>("outside-core-genre-mapping").album;
  for (const album of [row, { ...row, secondary_types: [] }, legacy]) {
    expect(albumSearchLabels(album, "en", "Year unknown").details).toBe(
      "2020 · Album",
    );
  }
});
