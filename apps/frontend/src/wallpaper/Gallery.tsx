import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import type { ProductCard, ProductState } from "../api/types.ts";
import { Cover } from "../components/Cover.tsx";
import { reviewProse } from "../components/deck/ReviewText.tsx";
import { useRoom } from "../components/deck/useRoom.ts";
import { genreDisplayName } from "../content/genreNotes.ts";
import { useCopy } from "../i18n/index.ts";
import { albumSeed } from "../lib/cover.ts";
import { paintPage } from "../lib/motion.ts";
import type { DiscoveryController } from "../state/controller.ts";
import { useStore } from "../state/hooks.ts";
import { supplying } from "../state/input.ts";
import type { LongReviewController } from "../state/review.ts";
import { LongSheet } from "./LongSheet.tsx";
import {
  TURN_HOUR,
  fingerprint,
  pickFor,
  placesOf,
  readMemory,
  rememberedNumber,
  stepped,
  wallpaperDay,
  writeMemory,
  type PickMemory,
} from "./pick.ts";

const sameMemory = (a: PickMemory | null, b: PickMemory | null) =>
  a?.candidate === b?.candidate && a?.day === b?.day && a?.place === b?.place;
const NIGHT = { room: "#0d1220", ink: "#efe9df", spark: "#efe9df" };

/** Steps the title down until it fits in `lines` lines of its full size. */
function useFittedTitle(text: string, lines: number) {
  const ref = useRef<HTMLHeadingElement>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const fit = () => {
      element.style.fontSize = "";
      const full = parseFloat(getComputedStyle(element).fontSize);
      const leading = parseFloat(getComputedStyle(element).lineHeight) || full;
      let size = full;
      while (
        element.scrollHeight > lines * leading * (size / full) + 2 &&
        size > full * 0.42
      ) {
        size *= 0.93;
        element.style.fontSize = `${size.toFixed(1)}px`;
      }
    };
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(element.parentElement ?? element);
    return () => observer.disconnect();
  }, [text, lines]);
  return ref;
}

/** A record marked 听过了 here, kept on show (dimmed) until its place is filled again. */
interface Heard {
  slotId: string;
  card: ProductCard;
}

/**
 * The wallpaper itself: one record a day, hung like a print. The whole desktop takes the
 * sleeve's colour; the record changes each morning at TURN_HOUR, and ‹ › step through the
 * set by hand. The record is remembered by its place in the server's set, so 听过了, its
 * replacement and an undo all land where the server puts them. Nothing here asks for new
 * picks on its own: 再来一组 and 听过了 are the person's clicks, and only those spend.
 */
export function Gallery({
  controller,
  review,
  state,
  onRethink,
  onMore,
  onRestart,
  refusals,
  freshSets,
}: {
  controller: DiscoveryController;
  review: LongReviewController;
  state: ProductState;
  onRethink: () => void;
  /** Asks for another set; resolves true once the server has accepted it. */
  onMore: () => Promise<boolean>;
  /** Offered only when the server wants a new explicit intent: a fresh set, own budget. */
  onRestart: (() => void) | null;
  /** Counts refused requests for another set (also after a retry from the toast). */
  refusals: number;
  /** Counts new sets the server has accepted; each starts at its first record. */
  freshSets: number;
}) {
  useStore(controller);
  useStore(review);
  const words = useCopy();
  const copy = words.wallpaper;

  const [today, setToday] = useState(() => wallpaperDay(new Date()));
  useEffect(() => {
    const timer = setInterval(() => setToday(wallpaperDay(new Date())), 60000);
    return () => clearInterval(timer);
  }, []);

  const places = useMemo(() => placesOf(state), [state]);
  const session = fingerprint(state.session_id);
  const [memory, setMemory] = useState<PickMemory | null>(() => readMemory());
  // Place numbers on show when 再来一组 asked to continue; the first new one then goes up.
  const [awaiting, setAwaiting] = useState<ReadonlySet<number> | null>(null);
  const [heard, setHeard] = useState<Heard | null>(null);

  const [seenFresh, setSeenFresh] = useState(freshSets);
  if (seenFresh !== freshSets) {
    setSeenFresh(freshSets);
    setMemory(null);
    setAwaiting(null);
  }
  const [seenRefusals, setSeenRefusals] = useState(refusals);
  if (seenRefusals !== refusals) {
    // The set stays as it was: stop waiting for a continuation.
    setSeenRefusals(refusals);
    setAwaiting(null);
  }
  const [seen, setSeen] = useState(state);
  if (seen !== state) {
    setSeen(state);
    const fresh = awaiting
      ? places.find((place) => place.card && !awaiting.has(place.number))
      : undefined;
    if (fresh?.card) {
      setAwaiting(null);
      setMemory({
        candidate: fresh.card.candidate_id,
        day: today,
        place: `${session}:${fresh.number}`,
      });
    }
  }

  const pick = pickFor(places, memory, today, session);
  // Only a settled pick is remembered; while a place is being filled the marker waits.
  if (pick.settled && !sameMemory(pick.memory, memory)) setMemory(pick.memory);
  // Only a complete marker of this version is written (and earlier ones removed); one
  // still being settled from an earlier version stays where it is until then.
  const memoryText = memory?.place ? JSON.stringify(memory) : null;
  useEffect(() => {
    if (memoryText) writeMemory(JSON.parse(memoryText) as PickMemory);
  }, [memoryText]);

  const number = rememberedNumber(memory, session);
  const remembered = places.find((place) => place.number === number);
  const shownPlace = pick.at >= 0 ? places[pick.at] : undefined;
  // A record marked 听过了 stays up, dimmed, while its place is being filled.
  const lingering =
    !!heard && !!remembered?.filling && remembered.slotId === heard.slotId;
  const card = lingering ? heard.card : (shownPlace?.card ?? null);
  const place = lingering ? remembered : shownPlace;
  const dimmed =
    !!card &&
    heard?.card.candidate_id === card.candidate_id &&
    (controller.locked ||
      controller.undoable?.candidateId === card.candidate_id);

  const [liked, setLiked] = useState<ReadonlySet<string>>(() => new Set());
  const room = useRoom(card) ?? NIGHT;
  useEffect(() => {
    paintPage(room.room, room.ink);
  }, [room.room, room.ink]);

  useEffect(() => {
    review.reconcile(
      state,
      new Set(lingering && heard ? [heard.card.candidate_id] : []),
    );
  });

  // The last single-card action of this page, which the server can still undo.
  const undoable =
    controller.undoable?.action === "HEARD" &&
    controller.undoable.sessionId === state.session_id;

  const title = useFittedTitle(card?.identity.title ?? "", 2);
  if (!card || !place)
    return (
      <section className="wall wall--empty">
        <p className="wall__notice">
          {supplying(state) ? words.deck.supplying : copy.noCards}
        </p>
        <div className="wall__finish">
          {undoable ? (
            <button
              type="button"
              className="wall__button"
              disabled={controller.locked}
              onClick={() => void controller.undo()}
            >
              {copy.undo}
            </button>
          ) : null}
          <button type="button" className="wall__button" onClick={onRethink}>
            {copy.rethink}
          </button>
        </div>
      </section>
    );

  const identity = card.identity;
  const [year, month, date] = today.split("-").map(Number) as [
    number,
    number,
    number,
  ];
  const weekday = new Date(year, month - 1, date).getDay();
  const genres = identity.primary_genres
    .slice(0, 3)
    .map((genre) => genreDisplayName(genre.genre_id, genre.name));
  // Places count while they are being filled, so the position stays put meanwhile.
  const position =
    places.findIndex((entry) => entry.slotId === place.slotId) + 1;
  const markable = !!place.card && !dimmed && !lingering;
  // ‹ › step from the place actually on show (a dimmed record waiting for its
  // replacement included), not from the record shown meanwhile.
  const onShow = { ...pick, at: places.indexOf(place) };
  const reading =
    review.isOpen && review.card?.candidate_id === card.candidate_id;
  const working = supplying(state) || controller.busy;

  const markHeard = async () => {
    if (!markable || controller.locked) return;
    setHeard({ slotId: place.slotId, card });
    const ok = await controller.action("card", place.slotId, "HEARD");
    if (!ok && !controller.pending) setHeard(null);
  };
  const askMore = () => {
    setAwaiting(new Set(places.map((entry) => entry.number)));
    void onMore().then((accepted) => {
      // Refused: the set stays. An unconfirmed answer waits for the retry's outcome.
      if (!accepted && !controller.pending) setAwaiting(null);
    });
  };
  const remember = (next: PickMemory | null) => {
    if (next) setMemory(next);
  };

  return (
    <section
      className="wall"
      data-heard={dimmed || undefined}
      data-reading={reading || undefined}
      style={{ "--spark": room.spark } as CSSProperties}
      aria-label={copy.title}
    >
      <div className="wall__sleeve" key={card.candidate_id}>
        <div className="wall__vinyl" aria-hidden="true">
          <i />
        </div>
        <Cover
          seed={albumSeed(identity.title, identity.artist_credit)}
          title={identity.title}
          url={card.cover_url}
          className="wall__cover"
        />
        <span className="wall__stamp" aria-hidden="true">
          {copy.heard}
        </span>
      </div>

      <div className="wall__text" key={`text:${card.candidate_id}`}>
        <p className="wall__eyebrow mono">
          {copy.today} · {copy.day(month, date, weekday)}
        </p>
        <h1 className="wall__title" ref={title}>
          {identity.title}
        </h1>
        <p className="wall__artist">
          {identity.artist_credit}
          {liked.has(card.candidate_id) ? (
            <span className="wall__love" aria-label={copy.like}>
              {" "}
              ♥
            </span>
          ) : null}
        </p>
        <p className="wall__meta mono">
          {[identity.first_release_year, ...genres]
            .filter((part) => part !== null && part !== undefined)
            .join(" · ")}
        </p>
        <p className="wall__review">{reviewProse(card.short_review.text)}</p>
      </div>

      <p className="wall__foot mono">
        {copy.position(Math.max(1, position), places.length)}
        {pick.finished
          ? ` · ${copy.finished}`
          : ` · ${copy.nextDay(TURN_HOUR)}`}
        {working ? ` · ${words.deck.supplying}` : ""}
      </p>

      <div className="wall__controls" role="group" aria-label={copy.controls}>
        <button
          type="button"
          aria-label={words.deck.prev}
          onClick={() => remember(stepped(places, onShow, -1, today, session))}
        >
          ‹
        </button>
        <button
          type="button"
          aria-label={words.deck.forward}
          onClick={() => remember(stepped(places, onShow, 1, today, session))}
        >
          ›
        </button>
        <button
          type="button"
          aria-pressed={dimmed}
          disabled={!markable || controller.locked}
          onClick={() => void markHeard()}
        >
          {copy.heard}
        </button>
        {undoable ? (
          <button
            type="button"
            disabled={controller.locked}
            onClick={() => void controller.undo()}
          >
            {copy.undo}
          </button>
        ) : null}
        <button
          type="button"
          aria-pressed={liked.has(card.candidate_id)}
          onClick={() =>
            setLiked((set) => {
              const next = new Set(set);
              if (next.has(card.candidate_id)) next.delete(card.candidate_id);
              else next.add(card.candidate_id);
              return next;
            })
          }
        >
          {copy.like}
        </button>
        <button
          type="button"
          aria-pressed={reading}
          onClick={() => (reading ? review.close() : void review.open(card))}
        >
          {reading ? words.review.close : words.review.open}
        </button>
      </div>

      {pick.finished || onRestart ? (
        <div className="wall__finish">
          <p className="wall__notice">{copy.finished}</p>
          {onRestart ? (
            <button
              type="button"
              className="wall__button"
              disabled={controller.locked}
              title={copy.restartNote}
              onClick={onRestart}
            >
              {copy.restart}
            </button>
          ) : null}
          <button
            type="button"
            className="wall__button"
            disabled={controller.locked || working}
            title={copy.paid}
            onClick={askMore}
          >
            {copy.more}
          </button>
          <button type="button" className="wall__button" onClick={onRethink}>
            {copy.rethink}
          </button>
        </div>
      ) : null}

      {reading ? <LongSheet card={card} review={review} /> : null}
    </section>
  );
}
