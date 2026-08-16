import type { AutonomyArtifact, AutonomyStep } from '@/types/autonomy'

export interface AutonomyStepPresentation {
  /** i18n key for the human-readable action title. */
  labelKey: string
  /** Short, safe action descriptor shown above the raw audit text. */
  command: string
  /** Original server-owned step summary, kept for the audit disclosure. */
  rawSummary: string
}

export interface AutonomyStepExecutionNote {
  exitCode: number | null
  stdoutArtifact: string | null
  stderrArtifact: string | null
  outputTruncated: boolean
}

export interface AutonomyStepCounts {
  total: number
  succeeded: number
  failed: number
  verificationTotal: number
  verificationSucceeded: number
  verificationFailed: number
}

const PROBE_LABEL_KEYS: Record<string, string> = {
  'system.disk_usage': 'diskUsage',
  'system.memory': 'memory',
  'system.load': 'load',
  'file.read_bounded': 'fileRead',
  'service.status': 'serviceStatus',
}

function field(summary: string, name: string): string | null {
  const match = summary.match(new RegExp(`(?:^|\\s)${name}=([^\\s]+)`))
  return match?.[1] || null
}

function pathField(summary: string): string | null {
  const match = summary.match(/(?:^|\s)path=(.+?)(?:\s+probe_id=|$)/)
  return match?.[1] || null
}

/**
 * Turn the server's bounded action summary into a readable label while
 * retaining the exact summary for the optional audit disclosure.
 */
export function presentAutonomyStep(
  step: Pick<AutonomyStep, 'kind' | 'summary'>,
): AutonomyStepPresentation {
  const rawSummary = String(step.summary || '').trim()
  const probeId = field(rawSummary, 'probe_id')

  if (probeId) {
    const probeLabel = PROBE_LABEL_KEYS[probeId]
    const path = pathField(rawSummary)
    return {
      labelKey: `aiRuns.detail.stepPresentation.${probeLabel || 'genericProbe'}`,
      command: [
        `probe ${probeId}`,
        path ? `path=${path}` : '',
      ].filter(Boolean).join(' · '),
      rawSummary,
    }
  }

  const patch = rawSummary.match(/^file_patch(?:\s+content=(.*?)\s+)?path=(.+)$/)
  if (patch) {
    return {
      labelKey: 'aiRuns.detail.stepPresentation.filePatch',
      command: `file_patch · path=${patch[2]}`,
      rawSummary,
    }
  }

  const systemd = rawSummary.match(/^systemd\s+operation=([^\s]+)\s+unit=(.+)$/)
  if (systemd) {
    const operation = systemd[1]
    return {
      labelKey: operation === 'restart'
        ? 'aiRuns.detail.stepPresentation.systemdRestart'
        : 'aiRuns.detail.stepPresentation.systemd',
      command: `systemd ${operation} · unit=${systemd[2]}`,
      rawSummary,
    }
  }

  return {
    labelKey: step.kind === 'plan'
      ? 'aiRuns.detail.stepPresentation.plan'
      : 'aiRuns.detail.stepPresentation.genericAction',
    command: rawSummary || '—',
    rawSummary,
  }
}

function noteField(note: string, name: string): string | null {
  const match = note.match(new RegExp(`(?:^|;)\\s*${name}=([^;]+)`))
  const value = match?.[1]?.trim()
  return value && value !== 'none' ? value : null
}

/** Parse the bounded executor note without exposing raw metadata as the UI's main result. */
export function parseAutonomyStepExecutionNote(note: string): AutonomyStepExecutionNote {
  const exitCodeMatch = note.match(/(?:^|;)\s*exit_code=(-?\d+)/)
  return {
    exitCode: exitCodeMatch ? Number(exitCodeMatch[1]) : null,
    stdoutArtifact: noteField(note, 'stdout_artifact'),
    stderrArtifact: noteField(note, 'stderr_artifact'),
    outputTruncated: /(?:^|;)\s*output_truncated=true(?:;|$)/.test(note),
  }
}

export function autonomyArtifactLabelKey(kind: string): string {
  switch (kind) {
    case 'step_stdout': return 'stdout'
    case 'step_stderr': return 'stderr'
    case 'patch_diff': return 'patchDiff'
    case 'backup_ref': return 'backup'
    default: return 'generic'
  }
}

/**
 * Keep the list view scannable without throwing away the full server goal.
 * Prefer the first operational clause, which normally names the first thing
 * the operator asked the Run to do.
 */
export function summarizeAutonomyGoal(goal: string, maxLength = 88): string {
  const normalized = String(goal || '').replace(/\s+/g, ' ').trim()
  if (!normalized) return '—'

  const sentence = normalized.split(/[。！？]/, 1)[0] || normalized
  const semicolon = sentence.indexOf('；')
  const candidate = semicolon > 0 ? sentence.slice(0, semicolon) : sentence
  if (candidate.length <= maxLength) return candidate
  return `${candidate.slice(0, Math.max(1, maxLength - 1)).trimEnd()}…`
}

export function countAutonomySteps(steps: readonly AutonomyStep[]): AutonomyStepCounts {
  const succeeded = steps.filter((step) => step.status === 'succeeded').length
  const failed = steps.filter((step) => step.status === 'failed').length
  const verification = steps.filter((step) => step.kind === 'verification')
  return {
    total: steps.length,
    succeeded,
    failed,
    verificationTotal: verification.length,
    verificationSucceeded: verification.filter((step) => step.status === 'succeeded').length,
    verificationFailed: verification.filter((step) => step.status === 'failed').length,
  }
}

export function artifactsForAutonomyStep(
  artifacts: readonly AutonomyArtifact[],
  stepId: string,
): AutonomyArtifact[] {
  return artifacts.filter((artifact) => artifact.step_id === stepId)
}
