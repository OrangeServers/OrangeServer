import { describe, expect, it } from 'vitest'

import {
  compareAutonomyDrafts,
  sortAutonomyDrafts,
} from '@/utils/autonomyDrafts'
import type { AiAutonomyDraft } from '@/types/ai'

function draft(
  id: string,
  created_at: string,
): AiAutonomyDraft {
  return { id, run_id: `run-${id}`, created_at, goal: id }
}

describe('autonomy draft ordering', () => {
  it('orders restored cards by creation time instead of payload order', () => {
    const newest = draft('newest', '2026-08-14T10:00:02.000Z')
    const oldest = draft('oldest', '2026-08-14T10:00:01.000Z')

    expect(sortAutonomyDrafts([newest, oldest])).toEqual([oldest, newest])
  })

  it('uses the stable card id when timestamps are equal', () => {
    const right = draft('draft-b', '2026-08-14T10:00:01.000Z')
    const left = draft('draft-a', '2026-08-14T10:00:01.000Z')

    expect(compareAutonomyDrafts(right, left)).toBeGreaterThan(0)
    expect(sortAutonomyDrafts([right, left])).toEqual([left, right])
  })
})
