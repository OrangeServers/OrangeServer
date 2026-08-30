import { describe, expect, it } from 'vitest'

import router from '@/router'

describe('AI Ops routes', () => {
  it('keeps one workbench route family and redirects legacy bookmarks', () => {
    const routes = router.getRoutes()
    const byPath = (path: string) => routes.find(route => route.path === path && (route.name || route.redirect))

    expect(byPath('/ai-ops')?.name).toBe('AiOpsWorkbench')
    expect(byPath('/ai-ops/tasks')?.name).toBe('AiOpsTasks')
    expect(byPath('/ai-ops/tasks/:runId')?.name).toBe('AiOpsRunDetail')
    expect(byPath('/ai-ops/alerts')?.name).toBe('AiOpsAlerts')
    expect(byPath('/ai-knowledge')?.name).toBe('AiKnowledge')
    expect(byPath('/ai-agent')?.redirect).toEqual({ name: 'AiOpsWorkbench' })
    expect(byPath('/ai-runs')?.redirect).toEqual({ name: 'AiOpsTasks' })
    expect(byPath('/ai-runs/:runId')?.redirect).toBeTypeOf('function')
  })
})
