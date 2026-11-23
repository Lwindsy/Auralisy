import { message } from "../api/messages.ts";
import type { ProductCard } from "../api/types.ts";
import { reviewProse } from "../components/deck/ReviewText.tsx";
import { useCopy } from "../i18n/index.ts";
import { useStore } from "../state/hooks.ts";
import { ACTIVE_REVIEW, type LongReviewController } from "../state/review.ts";

const SECTION_ORDER = [
  "production",
  "instrumentation",
  "vocals",
  "mood",
] as const;

/**
 * The long review on the wallpaper: a sheet beside the sleeve, read with the mouse wheel.
 * Opening it is the person's click (it raises the review's priority); a failed review is
 * written again only when they ask.
 */
export function LongSheet({
  card,
  review,
}: {
  card: ProductCard;
  review: LongReviewController;
}) {
  useStore(review);
  const words = useCopy().review;
  const status = review.view?.status ?? card.long_review_state;
  const sections = review.view?.review?.sections;
  const paragraphs =
    status === "READY" && sections
      ? SECTION_ORDER.map((key) => sections[key])
          .flatMap((part) => reviewProse(part ?? "").split(/\n{2,}/))
          .map((part) => part.trim())
          .filter(Boolean)
      : [];
  const writing =
    !paragraphs.length &&
    (status === "READY" || ACTIVE_REVIEW.includes(status));
  const reason = review.view?.message_key;

  return (
    <aside className="sheet" aria-label={words.open}>
      <p className="sheet__head mono">{card.identity.title}</p>
      {review.error ? (
        // A lost write keeps its key and is sent again; a failed read is read again.
        <div className="sheet__status" role="alert">
          <p>{message(review.error.messageKey ?? review.error.code)}</p>
          <button
            type="button"
            className="wall__button"
            disabled={review.busy}
            onClick={() =>
              review.error?.retry && review.pending
                ? void review.write(review.pending.operation)
                : void review.read()
            }
          >
            {words.again}
          </button>
        </div>
      ) : paragraphs.length ? (
        <div className="sheet__body">
          {paragraphs.map((text, index) => (
            <p key={index}>{text}</p>
          ))}
        </div>
      ) : writing ? (
        <p className="sheet__status" role="status">
          {words.writing}
        </p>
      ) : (
        <div className="sheet__status" role="status">
          <p>
            {words.failures[status] ?? words.unavailable}
            {reason ? words.detail(message(reason)) : ""}
          </p>
          <button
            type="button"
            className="wall__button"
            disabled={review.busy}
            onClick={() => void review.write("regenerate")}
          >
            {words.regenerate}
          </button>
        </div>
      )}
    </aside>
  );
}
