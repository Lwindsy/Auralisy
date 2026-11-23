import { useEffect, useRef, useState } from "react";
import type { Api } from "../api/client.ts";
import { ApiError } from "../api/client.ts";
import { message } from "../api/messages.ts";
import type { DiscoveryInput, ProductState } from "../api/types.ts";
import { Entry } from "../components/entry/Entry.tsx";
import { LangSwitch } from "../components/LangSwitch.tsx";
import {
  Contours,
  ripple,
  setFlow,
  setTerrain,
} from "../components/motion/Contours.tsx";
import { Drum } from "../components/motion/Drum.tsx";
import { Waiting } from "../components/motion/Waiting.tsx";
import { useCopy, withLanguage } from "../i18n/index.ts";
import { paintPage } from "../lib/motion.ts";
import type { DiscoveryController } from "../state/controller.ts";
import { readyCards } from "../state/deck.ts";
import { useStore } from "../state/hooks.ts";
import { hasInput, supplying } from "../state/input.ts";
import type { LongReviewController } from "../state/review.ts";
import { Gallery } from "./Gallery.tsx";
import { writeMemory } from "./pick.ts";

type Stage = "connecting" | "entry" | "leaving" | "waiting" | "gallery";

const NIGHT = { room: "#0d1220", ink: "#efe9df" };
const LEAVE_MS = 720;
const MIN_WAIT_MS = 1600;
/** How often the wallpaper tries to reach the local service again when it is down. */
const RECONNECT_MS = 10000;

const working = (state: ProductState) =>
  ["RUNNING", "WAITING"].includes(state.status);
const answered = (state: ProductState) =>
  readyCards(state).length > 0 || (!working(state) && !supplying(state));

/**
 * The wallpaper has no keyboard (Wallpaper Engine passes only the mouse), so it asks
 * only for genres: albums and free text are left empty on every request it sends.
 */
const genresOnly = (input: DiscoveryInput): DiscoveryInput => ({
  ...structuredClone(input),
  liked_albums: [],
  request_text: "",
});

/**
 * Auralisy as a desktop wallpaper: pick genres with the mouse, wait while the first
 * records are chosen, then one record a day. The same Java product API as the website,
 * from the same origin, so identity, retention and budgets stay where they are.
 */
export function WallpaperApp({
  api,
  controller,
  review,
}: {
  api: Api;
  controller: DiscoveryController;
  review: LongReviewController;
}) {
  useStore(controller);
  const words = useCopy();
  const state = controller.state;
  const [requested, setRequested] = useState<Stage | null>(null);
  const [entryKey, setEntryKey] = useState(0);
  const [minDone, setMinDone] = useState(true);
  /** The server asked for a new explicit intent: offer a fresh set as a second click. */
  const [offerRestart, setOfferRestart] = useState(false);
  const [refusals, setRefusals] = useState(0);
  const [freshSets, setFreshSets] = useState(0);
  /** The server accepted a new set: forget today's pick, the new set starts at its first. */
  const freshSet = () => {
    writeMemory(null);
    setFreshSets((count) => count + 1);
  };
  const minTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(minTimer.current), []);

  const session = state?.session_id ?? null;
  const cards = state ? readyCards(state).length : 0;
  let stage: Stage;
  if (!state) stage = "connecting";
  else if (requested === "leaving") stage = "leaving";
  else if (!session || requested === "entry") stage = "entry";
  else if (requested === "waiting") stage = "waiting";
  else stage = working(state) && !cards ? "waiting" : "gallery";
  if (stage === "waiting" && requested !== "waiting") setRequested("waiting");

  const settling =
    stage === "waiting" &&
    !!state &&
    (minDone || cards > 0) &&
    !controller.pending &&
    !controller.busy &&
    answered(state);

  // The local service may start after the wallpaper: keep knocking quietly.
  useEffect(() => {
    if (state || controller.busy) return;
    const timer = setTimeout(() => void controller.connect(), RECONNECT_MS);
    return () => clearTimeout(timer);
  }, [state, controller, controller.busy]);

  useEffect(() => {
    if (stage !== "gallery") paintPage(NIGHT.room, NIGHT.ink);
  }, [stage]);
  useEffect(() => {
    setFlow(stage === "waiting" ? 2.4 : stage === "gallery" ? 0.3 : 0.7);
  }, [stage]);
  const submittedMode = state?.editable_input?.mode ?? "explore";
  useEffect(() => {
    if (stage !== "entry") setTerrain(submittedMode);
  }, [stage, submittedMode]);

  const startWait = () => {
    setMinDone(false);
    clearTimeout(minTimer.current);
    minTimer.current = setTimeout(() => setMinDone(true), MIN_WAIT_MS);
    setRequested("waiting");
    ripple(1.4);
  };

  const submit = async (input: DiscoveryInput) => {
    setOfferRestart(false);
    setRequested("leaving");
    const leave = new Promise((resolve) => setTimeout(resolve, LEAVE_MS));
    const ok = await controller.submit(withLanguage(genresOnly(input)));
    if (ok) freshSet();
    await leave;
    if (ok || controller.pending) startWait();
    else setRequested("entry");
  };

  /**
   * 再来一组 asks the server for more in the same session (a continuation of the current
   * pool when there is one). A refusal is never turned into a new session on its own:
   * the server's retry instruction decides, and a fresh set (its own budget) is only
   * offered as a second, explicit click. The same handling settles a retry from the toast.
   */
  const more = async (): Promise<boolean> => {
    const current = controller.state;
    if (!current) return false;
    setOfferRestart(false);
    const tail = current.continuation_tail_id;
    const ok = tail
      ? await controller.continueAfter(tail)
      : await controller.action("next-batch");
    return settleBatch(ok, !!tail);
  };

  const settleBatch = async (ok: boolean, continuing: boolean) => {
    if (ok) {
      // A new set replaces this one (its records are new, so the gallery starts at the
      // first). A continuation only appends, and the gallery moves to the first new one.
      if (!continuing) {
        freshSet();
        startWait();
      }
      return true;
    }
    if (controller.pending) return false; // unconfirmed: the toast retries the same key
    // Refused: stop waiting for a new set before any fresh state comes in, so a record
    // the set gains on its own is not taken for the answer.
    setRefusals((count) => count + 1);
    const failure = controller.error;
    const disposition =
      failure instanceof ApiError ? failure.details?.retry_disposition : null;
    if (disposition === "POLL_STATE") {
      controller.clearError();
      await controller.refresh();
    } else if (disposition === "NEW_EXPLICIT_INTENT") {
      // Only when there are genres to start from; otherwise 换风格 is the way on.
      const input = controller.state?.editable_input;
      setOfferRestart(!!input && hasInput(genresOnly(input)));
    }
    return false;
  };

  /** The toast's retry: the same key again, then the same settling as the first try. */
  const retryPending = async () => {
    const operation = controller.pending;
    if (!operation) return;
    const batch = operation.path.endsWith("/next-batch");
    const continuing = batch && "tail_candidate_id" in operation.body;
    const ok = await controller.retry();
    if (batch) await settleBatch(ok, continuing);
    else if (ok && operation.path === "/v1/discovery-sessions") freshSet();
  };

  const restart = async () => {
    const input = controller.state?.editable_input;
    if (!input) return;
    setOfferRestart(false);
    controller.clearError();
    if (await controller.submit(withLanguage(genresOnly(input)))) {
      freshSet();
      startWait();
    }
  };

  const error = controller.error;
  const errorText =
    error && error.message !== "EXPIRED"
      ? message(
          error instanceof ApiError ? error.details?.message_key : null,
          message(error.message),
        )
      : null;

  return (
    <>
      <Contours />
      {stage === "entry" || stage === "leaving" ? (
        <div className="corner">
          <LangSwitch />
        </div>
      ) : null}
      <main className="stage" data-stage={stage} data-surface="wallpaper">
        {stage === "connecting" ? (
          <section className="connecting">
            {controller.busy ? (
              <>
                <Drum settling={false} />
                <p role="status">{words.app.restoring}</p>
              </>
            ) : (
              <p role="status">{words.wallpaper.offline}</p>
            )}
          </section>
        ) : stage === "entry" || stage === "leaving" ? (
          <Entry
            key={`${session ?? "blank"}:${entryKey}`}
            api={api}
            initial={
              state!.editable_input ? genresOnly(state!.editable_input) : null
            }
            saved={null}
            onDraft={() => {}}
            locked={controller.locked}
            leaving={stage === "leaving"}
            notice={<p className="entry__notice">{words.wallpaper.hint}</p>}
            onResume={session && cards > 0 ? () => setRequested(null) : null}
            onSubmit={(input) => void submit(input)}
            doors={["genres"]}
            genreSearch={false}
          />
        ) : stage === "waiting" ? (
          <Waiting
            state={state}
            settling={settling}
            onSettled={() => {
              setRequested(null);
              ripple(1.6);
            }}
          />
        ) : (
          <Gallery
            controller={controller}
            review={review}
            state={state!}
            onRethink={() => {
              review.close();
              setEntryKey((value) => value + 1);
              setRequested("entry");
            }}
            onMore={more}
            onRestart={offerRestart ? () => void restart() : null}
            refusals={refusals}
            freshSets={freshSets}
          />
        )}
      </main>
      {errorText ? (
        <div className="wall-toast" role="alert">
          <p>{errorText}</p>
          {state ? (
            <button
              type="button"
              disabled={controller.busy}
              onClick={() => void controller.refresh()}
            >
              {words.app.refresh}
            </button>
          ) : null}
          {controller.pending ? (
            <button
              type="button"
              disabled={controller.busy}
              onClick={() => void retryPending()}
            >
              {words.app.retry}
            </button>
          ) : null}
        </div>
      ) : null}
    </>
  );
}
