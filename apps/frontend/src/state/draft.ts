import type { DiscoveryInput } from "../api/types.ts";

export const REACH_DEFAULT = 10;

/** Nominal genre-relative reach range; deployed bands may expand for candidate supply. */
export function reachBounds(reach: number): { min: number; max: number } {
  return { min: Math.max(0, reach - 10), max: Math.min(100, reach + 10) };
}

/** Unsubmitted input on the entry page, with 传播度 kept as the slider's percentage. */
export interface EntryDraft {
  input: DiscoveryInput;
  reach: number;
}

/**
 * Keeps what the person typed or picked while they look at results and come back. Only in
 * this page's memory: natural language never goes to browser storage. Submitting, a reload
 * or a different session drops it.
 */
export class DraftStore {
  private entry: { owner: string; draft: EntryDraft } | null = null;

  get(owner: string): EntryDraft | null {
    return this.entry?.owner === owner
      ? structuredClone(this.entry.draft)
      : null;
  }

  keep(owner: string, draft: EntryDraft) {
    this.entry = { owner, draft: structuredClone(draft) };
  }

  discard() {
    this.entry = null;
  }
}

export const draftFrom = (input: DiscoveryInput): EntryDraft => ({
  input: structuredClone(input),
  reach: input.audience_reach_percent ?? REACH_DEFAULT,
});
