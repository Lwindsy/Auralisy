import { describe, expect, it } from "vitest";
import type { ProductCard, ProductState } from "../../src/api/types.ts";
import {
  fingerprint,
  pickFor,
  placesOf,
  rememberedNumber,
  stepped,
  wallpaperDay,
  type Place,
} from "../../src/wallpaper/pick.ts";

const TODAY = "2026-11-23";
const YESTERDAY = "2026-11-22";
const S = fingerprint("session-1");
const OTHER = fingerprint("session-2");
const pl = (number: number, session = S) => `${session}:${number}`;

const card = (id: string) => ({ candidate_id: id }) as ProductCard;
/** Places 1..n holding the given records; null is a place being filled. */
const at = (...ids: (string | null)[]): Place[] =>
  ids.map((id, i) => ({
    number: i + 1,
    slotId: `slot-${String(i + 1).padStart(4, "0")}`,
    card: id ? card(id) : null,
    filling: !id,
  }));

describe("wallpaperDay", () => {
  it("turns at eight in the morning, not at midnight", () => {
    expect(wallpaperDay(new Date(2026, 10, 23, 7, 59))).toBe(YESTERDAY);
    expect(wallpaperDay(new Date(2026, 10, 23, 8, 0))).toBe(TODAY);
    expect(wallpaperDay(new Date(2026, 0, 1, 3, 0))).toBe("2025-12-31");
  });
});

describe("fingerprint", () => {
  it("tells sessions apart with eight hex digits", () => {
    expect(S).toMatch(/^[0-9a-f]{8}$/);
    expect(S).not.toBe(OTHER);
    expect(fingerprint("session-1")).toBe(S);
  });
});

describe("placesOf", () => {
  it("keeps the server's order, with places being filled, left empty or failed", () => {
    const state = {
      slots: [
        { slot_id: "slot-0001", status: "READY", card: card("a") },
        { slot_id: "slot-0002", status: "SUPPLYING", card: null },
        { slot_id: "slot-0003", status: "EMPTY", card: null },
        { slot_id: "slot-0004", status: "READY", card: card("d") },
        { slot_id: "slot-0005", status: "FAILED", card: null },
      ],
    } as unknown as ProductState;
    expect(
      placesOf(state).map((place) => [place.number, place.card?.candidate_id]),
    ).toEqual([
      [1, "a"],
      [2, undefined],
      [3, undefined],
      [4, "d"],
      [5, undefined],
    ]);
  });
});

describe("pickFor", () => {
  it("shows nothing while no record is ready", () => {
    expect(pickFor(at(null), null, TODAY, S)).toMatchObject({
      at: -1,
      settled: false,
    });
  });

  it("starts at the first record without a marker", () => {
    expect(pickFor(at("a", "b"), null, TODAY, S)).toEqual({
      at: 0,
      memory: { candidate: "a", day: TODAY, place: pl(1) },
      finished: false,
      settled: true,
    });
  });

  it("keeps the same record all day", () => {
    const memory = { candidate: "b", day: TODAY, place: pl(2) };
    expect(pickFor(at("a", "b", "c"), memory, TODAY, S)).toMatchObject({
      at: 1,
      memory,
      finished: false,
    });
  });

  it("moves to the next place on a new day", () => {
    const pick = pickFor(
      at("a", "b", "c"),
      { candidate: "a", day: YESTERDAY, place: pl(1) },
      TODAY,
      S,
    );
    expect(pick.memory).toEqual({ candidate: "b", day: TODAY, place: pl(2) });
  });

  it("finishes the set on the last place and keeps it up", () => {
    const pick = pickFor(
      at("a", "b", "c"),
      { candidate: "c", day: YESTERDAY, place: pl(3) },
      TODAY,
      S,
    );
    expect(pick).toEqual({
      at: 2,
      memory: { candidate: "c", day: TODAY, place: pl(-3) },
      finished: true,
      settled: true,
    });
    expect(pickFor(at("a", "b", "c"), pick.memory, TODAY, S).finished).toBe(
      true,
    );
  });

  it("is no longer finished once the set gains a record, and keeps the one on show", () => {
    const pick = pickFor(
      at("a", "b", "c", "d"),
      { candidate: "c", day: TODAY, place: pl(-3) },
      TODAY,
      S,
    );
    expect(pick).toMatchObject({ at: 2, finished: false });
  });

  it("shows the record put in the same place after 听过了 (or an undo)", () => {
    const memory = { candidate: "b", day: TODAY, place: pl(2) };
    expect(pickFor(at("a", "j", "c"), memory, TODAY, S)).toMatchObject({
      at: 1,
      memory: { candidate: "j", day: TODAY, place: pl(2) },
      settled: true,
    });
  });

  it("follows a place only in the session it was written for", () => {
    // A new session numbers its places from 1 again: start at its first record.
    const memory = { candidate: "c", day: TODAY, place: pl(-3) };
    expect(pickFor(at("10", "11", "12"), memory, TODAY, OTHER).at).toBe(0);
  });

  it("waits for a place being filled without rewriting the marker", () => {
    const memory = { candidate: "c", day: TODAY, place: pl(3) };
    const waiting = pickFor(at("a", "b", null), memory, TODAY, S);
    expect(waiting).toMatchObject({ at: 1, memory, settled: false });
    // The replacement arrives later: it goes up, in its place.
    expect(pickFor(at("a", "b", "j"), memory, TODAY, S)).toMatchObject({
      at: 2,
      memory: { candidate: "j", day: TODAY, place: pl(3) },
      settled: true,
    });
  });

  it("follows repeated replacements in one place", () => {
    // 2 was heard, then its replacement 10 too: the place now holds 11.
    const memory = { candidate: "10", day: TODAY, place: pl(2) };
    expect(pickFor(at("1", "11", "3"), memory, TODAY, S)).toMatchObject({
      at: 1,
      memory: { candidate: "11", day: TODAY, place: pl(2) },
      finished: false,
    });
  });

  it("starts at the first record when the marker names no place of this set", () => {
    const memory = { candidate: "c", day: TODAY, place: pl(9) };
    expect(pickFor(at("x", "y"), memory, TODAY, S).at).toBe(0);
  });

  it("settles earlier versions' markers against the set", () => {
    const set = at("a", "b", "c");
    const legacy = (kind: string, candidate: string) =>
      ({ candidate, day: YESTERDAY, legacy: kind }) as never;
    expect(pickFor(set, legacy("beyond", "c"), TODAY, S)).toMatchObject({
      at: 2,
      finished: true,
    });
    expect(pickFor(set, legacy("due", "b"), TODAY, S).at).toBe(1);
    expect(pickFor(set, legacy("after", "a"), TODAY, S).at).toBe(1);
    expect(pickFor(set, legacy("after", "c"), TODAY, S).finished).toBe(true);
    // A plain position is an ordinary marker: a new day moves on.
    expect(pickFor(set, legacy("plain", "a"), TODAY, S).at).toBe(1);
    expect(pickFor(set, legacy("due", "gone"), TODAY, S).at).toBe(0);
  });

  it("keeps an earlier 'after' marker waiting while the place after it is filled", () => {
    const legacy = { candidate: "b", day: TODAY, legacy: "after" } as never;
    const waiting = pickFor(at("a", "b", null), legacy, TODAY, S);
    expect(waiting).toMatchObject({ at: 1, settled: false, finished: false });
    expect(pickFor(at("a", "b", "j"), legacy, TODAY, S)).toMatchObject({
      at: 2,
      memory: { candidate: "j", day: TODAY, place: pl(3) },
      settled: true,
    });
  });
});

describe("places that stay empty", () => {
  it("waits the same day, then moves on", () => {
    const memory = { candidate: "b", day: TODAY, place: pl(2) };
    expect(pickFor(at("a", null, "c"), memory, TODAY, S)).toMatchObject({
      at: 0,
      settled: false,
    });
    const tomorrow = "2026-11-24";
    expect(pickFor(at("a", null, "c"), memory, tomorrow, S)).toMatchObject({
      at: 2,
      memory: { candidate: "c", day: tomorrow, place: pl(3) },
      settled: true,
    });
    expect(pickFor(at("a", null), memory, tomorrow, S)).toMatchObject({
      at: 0,
      finished: true,
    });
  });
});

describe("sixth review", () => {
  it("does not let another session's marker pick a shared album", () => {
    const memory = { candidate: "3", day: TODAY, place: pl(-3, OTHER) };
    expect(pickFor(at("10", "3", "12"), memory, TODAY, S).at).toBe(0);
  });

  it("does not wait for an empty place behind an earlier anchor", () => {
    const set: Place[] = [
      { number: 1, slotId: "slot-0001", card: card("a"), filling: false },
      { number: 2, slotId: "slot-0002", card: null, filling: false },
      { number: 3, slotId: "slot-0003", card: card("c"), filling: false },
    ];
    const legacy = { candidate: "a", day: TODAY, legacy: "after" } as never;
    expect(pickFor(set, legacy, TODAY, S)).toMatchObject({
      at: 2,
      settled: true,
    });
  });

  it("stops waiting for a filling place behind an anchor on a later day", () => {
    const legacy = { candidate: "b", day: YESTERDAY, legacy: "after" } as never;
    expect(pickFor(at("a", "b", null), legacy, TODAY, S)).toMatchObject({
      at: 1,
      finished: true,
      settled: true,
    });
  });

  it("steps from a place being filled to its neighbours", () => {
    const set = at("a", "b", null);
    const onShow = { at: 2, memory: null, finished: false, settled: false };
    expect(stepped(set, onShow, -1, TODAY, S)?.candidate).toBe("b");
    expect(stepped(set, onShow, 1, TODAY, S)).toBeNull();
  });
});

describe("rememberedNumber", () => {
  it("reads the place only for its own session", () => {
    const memory = { candidate: "c", day: TODAY, place: pl(-3) };
    expect(rememberedNumber(memory, S)).toBe(3);
    expect(rememberedNumber(memory, OTHER)).toBeNull();
    expect(rememberedNumber(null, S)).toBeNull();
  });
});

describe("stepped", () => {
  const set = at("a", "b", "c");

  it("steps over places without wrapping and ends the set past the last", () => {
    const last = pickFor(
      set,
      { candidate: "c", day: TODAY, place: pl(3) },
      TODAY,
      S,
    );
    expect(stepped(set, last, 1, TODAY, S)).toEqual({
      candidate: "c",
      day: TODAY,
      place: pl(-3),
    });
    const first = pickFor(
      set,
      { candidate: "a", day: TODAY, place: pl(1) },
      TODAY,
      S,
    );
    expect(stepped(set, first, -1, TODAY, S)).toBeNull();
    expect(stepped(set, first, 1, TODAY, S)).toEqual({
      candidate: "b",
      day: TODAY,
      place: pl(2),
    });
  });

  it("returns from the end of the set to the last record", () => {
    const end = pickFor(
      set,
      { candidate: "c", day: TODAY, place: pl(-3) },
      TODAY,
      S,
    );
    expect(end.finished).toBe(true);
    expect(stepped(set, end, -1, TODAY, S)).toEqual({
      candidate: "c",
      day: TODAY,
      place: pl(3),
    });
  });

  it("skips places being filled", () => {
    const gappy = at("a", null, "c");
    const first = pickFor(gappy, null, TODAY, S);
    expect(stepped(gappy, first, 1, TODAY, S)?.candidate).toBe("c");
  });
});
