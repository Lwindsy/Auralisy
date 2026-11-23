import type { ProductCard, ProductState } from "../api/types.ts";

/** The hour of the morning the wallpaper turns to the next record. */
export const TURN_HOUR = 8;

/** Where the wallpaper keeps today's pick in its own storage (this storage version). */
export const PICK_KEY = "auralisy.wallpaper.pick.v5";
/** Earlier storage versions, read once, settled against the set and then removed. */
const LEGACY_KEYS = [
  "auralisy.wallpaper.pick.v4",
  "auralisy.wallpaper.pick.v3",
  "auralisy.wallpaper.pick",
];

/**
 * One card place of the session, in the server's order. The server keeps a place for a
 * record: 听过了 fills the same place with another record (or leaves it supplying for a
 * while), and an undo puts the record back there. So the place, not the order the page
 * met the records in, is what the wallpaper remembers.
 */
export interface Place {
  /** The place's number, from its slot id (`slot-0003` is 3). */
  number: number;
  slotId: string;
  /** The record in it, or null while the place is being filled or left empty. */
  card: ProductCard | null;
  /** True while the server is still filling the place (SUPPLYING), false once empty. */
  filling: boolean;
}

/**
 * Kept on this device only (the wallpaper's own storage): the record on show, since which
 * wallpaper day, and its place. Three fields only: an opaque candidate id, a date and a
 * position; never the person's input or the session credential. The position `place` is
 * `<session fingerprint>:<place number>`, negative once the person has stepped beyond the
 * last record. Place numbers start again at 1 in every session, so a place is only
 * followed in the session it was written for; the fingerprint is a short one-way hash of
 * the session id, enough to tell sessions apart and nothing more.
 */
export interface PickMemory {
  candidate: string;
  day: string;
  place?: string;
}

/** A marker from an earlier storage version, settled on its first use. */
interface LegacyMemory extends PickMemory {
  legacy: "beyond" | "due" | "after" | "plain";
}

export interface Pick {
  /** Index into the places, or -1 when there is nothing to show. */
  at: number;
  memory: PickMemory | null;
  finished: boolean;
  /**
   * False while the remembered place is still being filled: the page shows something in
   * the meantime but keeps the stored marker as it is.
   */
  settled: boolean;
}

const pad = (value: number) => String(value).padStart(2, "0");

/** The wallpaper's calendar day: it changes at TURN_HOUR in the morning, not at midnight. */
export function wallpaperDay(now: Date, turnHour = TURN_HOUR): string {
  const shifted = new Date(now);
  shifted.setHours(shifted.getHours() - turnHour);
  return `${shifted.getFullYear()}-${pad(shifted.getMonth() + 1)}-${pad(shifted.getDate())}`;
}

/** A short one-way fingerprint of the session id (FNV-1a, 32 bits). */
export function fingerprint(session: string | null): string {
  let hash = 0x811c9dc5;
  for (const char of session ?? "")
    hash = Math.imul(hash ^ char.charCodeAt(0), 0x01000193) >>> 0;
  return hash.toString(16).padStart(8, "0");
}

function readPlace(place: string | undefined) {
  const match = /^([0-9a-f]{8}):(-?\d+)$/.exec(place ?? "");
  if (!match) return null;
  const value = Number(match[2]);
  return { session: match[1]!, number: Math.abs(value), beyond: value < 0 };
}

/**
 * Card places in the server's order. A place being filled, left empty because nothing
 * was left to supply, or whose filling failed keeps its spot (card null): an undo can
 * still put its record back.
 */
export function placesOf(state: ProductState | null): Place[] {
  if (!state) return [];
  return state.slots.flatMap((slot): Place[] => {
    const number = Number(/(\d+)$/.exec(slot.slot_id)?.[1] ?? NaN);
    if (!Number.isFinite(number)) return [];
    if (slot.status === "READY" && slot.card)
      return [
        { number, slotId: slot.slot_id, card: slot.card, filling: false },
      ];
    // A place left empty, or whose filling failed, keeps its spot like one being filled:
    // an undo can still put its record back there.
    return slot.status === "SUPPLYING" ||
      slot.status === "EMPTY" ||
      slot.status === "FAILED"
      ? [
          {
            number,
            slotId: slot.slot_id,
            card: null,
            filling: slot.status === "SUPPLYING",
          },
        ]
      : [];
  });
}

const ready = (places: readonly Place[], at: number) => !!places[at]?.card;

function nextReady(places: readonly Place[], from: number) {
  for (let at = from + 1; at < places.length; at++)
    if (ready(places, at)) return at;
  return -1;
}

function previousReady(places: readonly Place[], from: number) {
  for (let at = from - 1; at >= 0; at--) if (ready(places, at)) return at;
  return -1;
}

const memoryAt = (
  places: readonly Place[],
  at: number,
  day: string,
  session: string,
  beyond = false,
): PickMemory => ({
  candidate: places[at]!.card!.candidate_id,
  day,
  place: `${session}:${beyond ? -places[at]!.number : places[at]!.number}`,
});

/**
 * The record on show today. It stays the same all day (a record put in its place by
 * 听过了 takes over); on a new day the wallpaper moves to the next place. Past the last
 * one the set is finished and the last record stays up. Without a marker, or with one
 * from another session, it starts at the first record. This never asks the server for
 * anything: a new set is always the person's own click, and the page forgets the marker
 * once the server has accepted one. `session` is the current session's fingerprint.
 */
export function pickFor(
  places: readonly Place[],
  stored: PickMemory | null,
  today: string,
  session: string,
): Pick {
  const shown = (at: number): Pick => ({
    at,
    memory: memoryAt(places, at, today, session),
    finished: false,
    settled: true,
  });
  const ended = (at: number): Pick => ({
    at,
    memory: memoryAt(places, at, today, session, true),
    finished: true,
    settled: true,
  });
  const waiting = (at: number, memory: PickMemory): Pick => {
    // The place is being filled: show the record before it (or after it) meanwhile.
    const before = previousReady(places, at);
    const meanwhile = before >= 0 ? before : nextReady(places, at);
    return { at: meanwhile, memory, finished: false, settled: false };
  };

  const first = places.findIndex((place) => place.card);
  if (first < 0)
    return { at: -1, memory: null, finished: false, settled: false };
  if (!stored) return shown(first);

  const legacy = (stored as LegacyMemory).legacy;
  const place = readPlace(stored.place);
  // A marker written for another session never steers this one, not even when the same
  // album turns up again in it: the new set starts at its first record.
  if (!legacy && place && place.session !== session) return shown(first);
  const byRecord = places.findIndex(
    (entry) => entry.card?.candidate_id === stored.candidate,
  );
  if (legacy === "plain")
    return pickFor(
      places,
      { candidate: stored.candidate, day: stored.day },
      today,
      session,
    );
  if (legacy) {
    // Earlier versions remembered the record only. Settle what can be settled; anything
    // unclear falls back to showing the remembered record, or the first one.
    if (byRecord < 0) return shown(first);
    const next = nextReady(places, byRecord);
    if (legacy === "beyond")
      return next < 0 ? ended(byRecord) : shown(byRecord);
    if (legacy === "after") {
      // "The record after this one": while a place after it is still being filled (the
      // same day), that place is the one meant, so wait for it instead of settling now.
      const end = next < 0 ? places.length : next;
      const filling = places.findIndex(
        (entry, at) => at > byRecord && at < end && entry.filling,
      );
      if (filling >= 0 && stored.day === today)
        return {
          at: byRecord,
          memory: stored,
          finished: false,
          settled: false,
        };
      return next < 0 ? ended(byRecord) : shown(next);
    }
    return shown(byRecord);
  }

  const samePlace =
    place?.session === session
      ? places.findIndex((entry) => entry.number === place.number)
      : -1;
  const at = byRecord >= 0 ? byRecord : samePlace;
  if (at < 0) return shown(first);
  if (!ready(places, at)) {
    // The same day the place may still be filled (or its record put back by an undo):
    // wait. On a later day the wallpaper moves on instead of waiting for it.
    if (stored.day === today) return waiting(at, stored);
    const later = nextReady(places, at);
    if (later >= 0) return shown(later);
    const before = previousReady(places, at);
    return before >= 0 ? ended(before) : waiting(at, stored);
  }

  const next = nextReady(places, at);
  if (stored.day === today)
    return place?.beyond && next < 0 ? ended(at) : shown(at);
  return next < 0 ? ended(at) : shown(next);
}

/**
 * A step the person asks for with ‹ ›. Stepping past the last record shows the end of
 * the set instead of wrapping round; stepping back from there returns to the last one.
 */
export function stepped(
  places: readonly Place[],
  pick: Pick,
  delta: 1 | -1,
  today: string,
  session: string,
): PickMemory | null {
  if (pick.at < 0) return null;
  if (delta === -1 && pick.finished && ready(places, pick.at))
    return memoryAt(places, pick.at, today, session);
  // From the place on show (also one still being filled), to the nearest record.
  const at =
    delta === 1 ? nextReady(places, pick.at) : previousReady(places, pick.at);
  if (at >= 0) return memoryAt(places, at, today, session);
  return delta === 1 && ready(places, pick.at)
    ? memoryAt(places, pick.at, today, session, true)
    : null;
}

function parse(raw: string | null) {
  if (!raw) return null;
  const value = JSON.parse(raw) as {
    candidate?: unknown;
    day?: unknown;
    place?: unknown;
    index?: unknown;
    finished?: unknown;
  };
  return typeof value.candidate === "string" && typeof value.day === "string"
    ? {
        candidate: value.candidate,
        day: value.day,
        place: typeof value.place === "string" ? value.place : undefined,
        index:
          typeof value.index === "number" && Number.isInteger(value.index)
            ? value.index
            : undefined,
        finished: value.finished === true,
      }
    : null;
}

export function readMemory(): PickMemory | null {
  try {
    const current = parse(localStorage.getItem(PICK_KEY));
    if (current)
      return {
        candidate: current.candidate,
        day: current.day,
        ...(readPlace(current.place) ? { place: current.place } : {}),
      };
    // Earlier versions: v4 kept a place number without the session (negative: the end
    // of the set); v3 kept -1 due, -2 the end of the set, -3 "the record after this
    // one"; v1/v2 kept a finished flag, or a position that cannot be told apart from
    // "one past the record". Settled against the set on first use and written under the
    // new key; an unclear position is treated as an ordinary one.
    for (const [version, key] of LEGACY_KEYS.entries()) {
      const legacy = parse(localStorage.getItem(key));
      if (!legacy) continue;
      const index = legacy.index;
      const kind: LegacyMemory["legacy"] =
        version === 0
          ? index !== undefined && index < 0
            ? "beyond"
            : "plain"
          : legacy.finished || index === -2
            ? "beyond"
            : index === -1
              ? "due"
              : index === -3
                ? "after"
                : "plain";
      return {
        candidate: legacy.candidate,
        day: legacy.day,
        legacy: kind,
      } as LegacyMemory;
    }
    return null;
  } catch {
    return null;
  }
}

/** Writes the marker; earlier versions are removed once the new one is in place. */
export function writeMemory(memory: PickMemory | null) {
  try {
    if (memory)
      localStorage.setItem(
        PICK_KEY,
        JSON.stringify({
          candidate: memory.candidate,
          day: memory.day,
          ...(memory.place === undefined ? {} : { place: memory.place }),
        }),
      );
    else localStorage.removeItem(PICK_KEY);
    for (const key of LEGACY_KEYS) localStorage.removeItem(key);
  } catch {
    // Storage refused (private profile): the wallpaper still shows today's record.
  }
}

/** The place number a marker names in this session, or null (none, or another session). */
export function rememberedNumber(memory: PickMemory | null, session: string) {
  const place = readPlace(memory?.place);
  return place && place.session === session ? place.number : null;
}
