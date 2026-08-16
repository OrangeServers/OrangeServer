// Minimal M1/S3 frontend contract tests for the shared SSE frame parser.
// parseEventBlock is consumed by both the chat POST stream and the
// autonomous-run GET stream, so its framing rules are a locked contract.
import { describe, expect, it } from 'vitest'

import { parseEventBlock } from '@/utils/aiStream'

describe('parseEventBlock', () => {
  it('parses a typed JSON frame with id and event fields', () => {
    const block = [
      'id: 7',
      'event: step.completed',
      'data: {"outcome": "resolved"}',
    ].join('\n')

    expect(parseEventBlock(block)).toEqual({
      type: 'step.completed',
      data: { outcome: 'resolved' },
      id: '7',
    })
  })

  it('falls back to the payload type when no event field exists', () => {
    const block = 'data: {"type": "message", "content": "hi"}'

    expect(parseEventBlock(block)).toEqual({
      type: 'message',
      data: { type: 'message', content: 'hi' },
      id: undefined,
    })
  })

  it('maps the [DONE] sentinel to run.completed', () => {
    expect(parseEventBlock('data: [DONE]')).toEqual({
      type: 'run.completed',
      data: {},
      id: undefined,
    })
  })

  it('wraps non-JSON payloads into content instead of dropping them', () => {
    expect(parseEventBlock('data: plain output')).toEqual({
      type: 'message',
      data: { content: 'plain output' },
      id: undefined,
    })
  })

  it('joins multi-line data with newlines before parsing', () => {
    const block = 'data: {"summary":\ndata: "one\\ntwo"}'

    expect(parseEventBlock(block)).toEqual({
      type: 'message',
      data: { summary: 'one\ntwo' },
      id: undefined,
    })
  })

  it('ignores comment-only and empty blocks', () => {
    expect(parseEventBlock(': keep-alive')).toBeNull()
    expect(parseEventBlock('')).toBeNull()
  })
})
