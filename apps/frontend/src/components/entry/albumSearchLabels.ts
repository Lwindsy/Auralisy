import type { CatalogAlbum } from "../../api/types.ts";
import type { Locale } from "../../i18n/index.ts";

const secondaryTypesZh = new Map([
  ["Live", "现场"],
  ["Compilation", "合辑"],
  ["Soundtrack", "原声"],
  ["Remix", "混音"],
  ["DJ-mix", "DJ 混音"],
  ["Mixtape/Street", "混音带"],
  ["Demo", "小样"],
  ["Interview", "访谈"],
  ["Spokenword", "口述"],
  ["Audiobook", "有声书"],
  ["Audio drama", "广播剧"],
  ["Field recording", "环境录音"],
]);

export function albumSearchLabels(
  album: CatalogAlbum,
  locale: Locale,
  yearUnknown: string,
) {
  const { title, artist_credit, first_release_year, release_type } =
    album.identity;
  const secondaryTypes = (album.secondary_types ?? []).map((type) =>
    locale === "zh-CN" ? (secondaryTypesZh.get(type) ?? type) : type,
  );
  const details = [
    first_release_year ?? yearUnknown,
    release_type,
    ...secondaryTypes,
    album.disambiguation,
  ]
    .filter((part) => part != null && part !== "")
    .join(" · ");
  return { details, name: `${title} — ${artist_credit} · ${details}` };
}
