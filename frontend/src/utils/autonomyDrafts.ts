import type { AiAutonomyDraft } from '@/types/ai'

function draftTime(value?: string): number {
  const parsed = value ? Date.parse(value) : Number.NaN
  return Number.isFinite(parsed) ? parsed : 0
}

function draftIdentity(draft: AiAutonomyDraft): string {
  return String(draft.id || draft.run_id || '')
}

/** Keep live and history-restored autonomy cards in one deterministic order. */
export function compareAutonomyDrafts(
  left: AiAutonomyDraft,
  right: AiAutonomyDraft,
): number {
  const timeDelta = draftTime(left.created_at) - draftTime(right.created_at)
  if (timeDelta !== 0) return timeDelta
  return draftIdentity(left).localeCompare(draftIdentity(right))
}

export function sortAutonomyDrafts(
  items: readonly AiAutonomyDraft[],
): AiAutonomyDraft[] {
  return [...items].sort(compareAutonomyDrafts)
}
