# AI operations

OrangeServer's AI assistant works inside the same permission boundary as every
human user — it queries authorized platform data, runs fixed read-only
diagnostics, and prepares batch actions that always require human approval.

![AI operations](/screens/ai-agent.png)

## What it can do

- **Query platform data** through permission-filtered structured tools: assets,
  groups, execution logs, audit records. Results come back as server-side
  result sets with authoritative IDs.
- **Run read-only diagnostics** on managed hosts using fixed server-owned
  Linux/Docker profiles. Evidence is sanitized, size-capped, encrypted at
  rest, and every finding must cite evidence from the current run.
- **Prepare batch commands** across authorized assets. The plan is shown as an
  approval card; nothing executes until a human approves it.
- **Answer in your language** — replies follow the interface language setting.

## M1 controlled autonomy

M1 adds an administrator-only workbench at `/ai-runs` for a single-host,
recoverable Run: investigate, perform bounded changes, restart a service when
approved, verify independently, and produce a cited conclusion. The server
fixes the target, system account, permission profile, budget, and action
allowlist; the model never receives an unrestricted shell. `auto` is restricted
to assets explicitly marked `lab`.

The standard bundled install starts dedicated Redis and a Worker; autonomous
runs are available by default. See the
[repository AI guide](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/USER_GUIDE.md).

## What it cannot do

- It cannot run SQL or open an unrestricted shell.
- Chat diagnostics and batch commands never execute until a human approves
  them. M1 server-owned `allow` probes may continue without a per-step prompt;
  `auto` is limited to assets marked `lab` and still cannot elevate a `deny`.
- It cannot invent asset IDs, database fields, or execution results — tool
  results are the only source of truth.
- Tool output, history summaries, and diagnostic evidence are treated as
  untrusted low-privilege data: instructions embedded in them are never
  followed.

## Evidence and audit

Every tool call, approval, and execution is recorded. Diagnostic findings are
deterministic and citable — each one references evidence IDs from the current
diagnostic run, so conclusions can always be traced back to raw data.

## Learn more

- [AI user guide](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/USER_GUIDE.md)
- [Providers and context modes](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/PROVIDER_AND_CONTEXT.md)
- [Read-only diagnostics](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/DIAGNOSTICS.md)
- [API reference](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/API.md)
