import { describe, expect, it } from 'vitest'

import {
  autonomyArtifactLabelKey,
  countAutonomySteps,
  parseAutonomyStepExecutionNote,
  presentAutonomyStep,
  summarizeAutonomyGoal,
} from '@/utils/autonomyPresentation'
import type { AutonomyStep } from '@/types/autonomy'

function step(
  seq: number,
  kind: AutonomyStep['kind'],
  status: AutonomyStep['status'],
  summary: string,
): AutonomyStep {
  return {
    id: `step-${seq}`,
    run_id: 'run-1',
    kind,
    status,
    seq,
    summary,
    action_digest: '',
    note: '',
    created_at: null,
  }
}

describe('autonomy detail presentation', () => {
  it('turns known probe summaries into readable actions while retaining raw text', () => {
    const result = presentAutonomyStep(
      step(1, 'action', 'succeeded', 'probe probe_id=system.disk_usage'),
    )

    expect(result.labelKey).toBe('aiRuns.detail.stepPresentation.diskUsage')
    expect(result.command).toBe('probe system.disk_usage')
    expect(result.rawSummary).toBe('probe probe_id=system.disk_usage')
  })

  it('keeps target paths visible for file and service actions', () => {
    expect(presentAutonomyStep(
      step(1, 'action', 'succeeded', 'file_patch content=ok path=/tmp/marker.txt'),
    ).command).toBe('file_patch · path=/tmp/marker.txt')

    expect(presentAutonomyStep(
      step(2, 'action', 'succeeded', 'systemd operation=restart unit=crond'),
    )).toMatchObject({
      labelKey: 'aiRuns.detail.stepPresentation.systemdRestart',
      command: 'systemd restart · unit=crond',
    })
  })

  it('parses executor result metadata into a bounded display model', () => {
    expect(parseAutonomyStepExecutionNote(
      'exit_code=0; stdout_artifact=stdout-1; stderr_artifact=none; output_truncated=false',
    )).toEqual({
      exitCode: 0,
      stdoutArtifact: 'stdout-1',
      stderrArtifact: null,
      outputTruncated: false,
    })
  })

  it('counts verification success separately from action success', () => {
    const counts = countAutonomySteps([
      step(1, 'action', 'succeeded', 'systemd operation=restart unit=crond'),
      step(2, 'verification', 'succeeded', 'probe probe_id=service.status unit=crond'),
      step(3, 'verification', 'failed', 'probe probe_id=file.read_bounded'),
    ])

    expect(counts).toEqual({
      total: 3,
      succeeded: 2,
      failed: 1,
      verificationTotal: 2,
      verificationSucceeded: 1,
      verificationFailed: 1,
    })
  })

  it('maps artifact kinds to user-facing result labels', () => {
    expect(autonomyArtifactLabelKey('step_stdout')).toBe('stdout')
    expect(autonomyArtifactLabelKey('patch_diff')).toBe('patchDiff')
    expect(autonomyArtifactLabelKey('unknown')).toBe('generic')
  })

  it('keeps the first operational clause as a scannable goal summary', () => {
    expect(summarizeAutonomyGoal(
      '在唯一目标资产上完成一次有界自治闭环：先调查磁盘、内存和负载；然后重启 crond；最后独立验证。', // i18n-ignore test fixture
    )).toBe('在唯一目标资产上完成一次有界自治闭环：先调查磁盘、内存和负载') // i18n-ignore test fixture
  })

  it('bounds very long goals without changing the full goal source', () => {
    expect(summarizeAutonomyGoal('a'.repeat(20), 10)).toBe('aaaaaaaaa…')
  })
})
