# Changelog

Notable user-visible changes are recorded here. This project follows the
principles of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- The AI Ops landing page now leads with pending alerts, active runs, recent
  conclusions, Worker capacity, and knowledge-index status. An optional
  Bearer-authenticated Alertmanager webhook creates idempotent `ask` runs and
  attaches bounded, server-owned Prometheus observations to their timeline.
- An administrator-reviewed operations knowledge base indexes Markdown
  runbooks and independently verified runs with a bundled Chinese BGE ONNX
  model or a separate OpenAI-compatible embedding endpoint. Chat and the
  Autonomy Planner return bounded, versioned citations without granting
  permissions or replacing live evidence.

### Changed

- AI operations now uses a Codex-style workbench with grouped task and alert
  views, a collapsible recent-task rail, an in-place evidence and raw-output
  inspector, on-demand run context, compact mobile actions, legacy-route
  redirects, and a separate first-level knowledge destination. Task rows now
  lead with a readable target and next action; alert groups show Alertmanager's
  latest firing/resolved state separately from Run outcome. Terminal failures
  now lead with the blocking step instead of a pending-conclusion message;
  task, knowledge, and evidence read failures remain visible instead of being
  presented as empty or zero-result states.
  Authorized users can manage their own Runs and search scoped knowledge;
  knowledge mutations remain administrator-only.
- Chat now handles query, explanation, diagnosis, and Autonomy Run drafts;
  every remote write is executed through the existing approval-gated Run path.
  Its composer now offers four real operations starters and a user-owned
  `ask`/`ai_review`/`auto`/`custom` permission selector. The server stores and
  enforces that conversation profile for future drafts, so the model cannot
  choose or widen execution permissions.
- The Autonomy Worker keeps Celery prefork and defaults to two configurable
  execution slots. DeepSeek reasoning is preserved across tool-call turns.
- The standard Compose deployment now serves the bundled SPA from the app
  image and uses four product containers with one Redis 8 service.

### Removed

- The unused in-tree Ansible Runner and its `ansible-core` dependency.

### Fixed

- Worker health checks no longer leak long-lived Celery inspect processes;
  the bounded probe now verifies checkpoint storage and Worker registration
  in one directly managed process.
- Fresh-setup administrators now receive the existing all-access binding,
  authorized host-scoped Run knowledge is available to chat, and ask-mode
  approval plus final Run conclusions expose the complete operator-facing
  action and evidence-backed conclusion fields.
- Existing and renamed administrator accounts now retain direct permission
  bindings used by asset and automation authorization, and the upgrade
  migration backfills custom admin names into the existing all-access rule.
- The Run composer now loads owner-scoped credential options for normal users
  instead of calling an administrator-only credential-management endpoint.
  Conversation profile changes and Agent turns share the existing run lock so
  neither can overwrite the other's Redis conversation state.

## [1.1.1] - 2026-08-19

### Added

- `bootstrap-compose.sh --autonomy-redis-image` registry override; the China
  entry point mirrors the autonomy Redis Stack image by default.
- UPGRADE.md documents the `OGS_AI_AUTONOMY_*` environment keys required
  when upgrading an existing bundled installation.

### Changed

- Standard bundled Compose now starts dedicated autonomy Redis Stack and a
  Celery Worker using the same backend image. Autonomous runs are on by
  default; `OGS_AI_AUTONOMY_ENABLED=false` remains an emergency process
  kill switch.

### Fixed

- Run detail cancel button: the v1.1.0 dist was built from sources missing
  the `doCancel` handler, so the button rendered but did nothing.
- Cancelling a run whose worker lease has expired now releases the host
  lock immediately; terminal-state run failure handling is idempotent.

## [1.1.0] - 2026-08-19

### Added

- AI autonomy M1/S1 safety and approval baseline (disabled by default):
  Run/Step/event/artifact domain model with a strict server-side state
  machine, an administrator-managed asset environment column
  (`t_host.ai_environment`, rev53), structured probe actions with budget,
  policy, redaction, and approval-digest validation, and optimistic-revision
  step decisions that re-check asset and credential authorization atomically.
  Every autonomy endpoint stays rejected until `OGS_AI_AUTONOMY_ENABLED` is
  explicitly set, and this stage performs no remote execution.

- AI autonomy M1/S3 planning, evidence and product loop (disabled by
  default): a server-side tool-calling planner whose immutable plan steps are
  bound to a one-time plan-level authorization digest, an optional Guardian
  that can only tighten `ask` decisions, redacted untrusted Evidence, an
  independent verification step and tri-state run outcomes, read-only REST
  and resumable SSE contracts for runs, an administrator workbench
  (`/ai-runs`), and chat draft reference cards that can never start, approve
  or cancel runs. The feature remains gated behind
  `OGS_AI_AUTONOMY_ENABLED`; the standard release deployment keeps it off and
  does not start the dedicated Redis 8/Worker pair.

### Fixed

- Chat autonomy draft reference cards now use creation time plus a stable ID
  when restoring history, so refreshing a conversation does not change their
  display order.

- The disabled-by-default autonomy SSH runner now remains compatible with older Linux
  `setsid` implementations that do not support `--wait`, while the development
  Worker inherits the configured host-key policy from the backend.

- The disabled-by-default autonomy Planner now gives OpenAI-compatible reasoning models
  such as DeepSeek one bounded, server-selected tool-contract repair after a
  phase or parameter mismatch; rejected first proposals remain side-effect
  free and all repaired plans still pass the server action fences. Finish
  citations are constrained to server-issued same-run Evidence IDs; a second
  invalid citation after an independent verification fails closed to an
  inconclusive outcome without replaying writes.

### Changed

- The disabled-by-default M1 autonomy workflow now uses the common
  `allow` / `ask` / `deny` harness contract and four permission profiles:
  ask every time, AI review, automatic, and custom. New runs use a versioned
  LangGraph route where server-owned read-only probes continue without a
  per-step prompt, while `ask` still pauses at the existing approval interrupt
  and `deny` can never be elevated by a model. Persisted v1 runs retain their
  original graph and legacy mode semantics for recovery.

- The disabled-by-default autonomy workbench now leads with a server-authoritative
  conclusion and presents each step as a readable action, command result, and
  linked bounded Artifact. Raw Evidence summaries and action digests remain
  available under audit disclosures; execution, authorization, and Evidence
  trust semantics are unchanged.

- AI operations pages now make the primary path easier to scan: the Run list has
  search and an explicit details action, creation modes show their guidance
  together, advanced limits are opt-in, and detail-page evidence, artifacts, and
  audit events are collapsed until needed. Conversation technical identifiers
  and backend error details are likewise disclosed on demand; the shared header
  also stays legible on narrow screens.

- The disabled-by-default M1/S2 worker path now fences every claim, checkpoint write,
  and final write with a one-time lease token, continuously rescans
  recoverable runs, and fails closed when its Redis checkpoint store or a
  safe MySQL recovery cursor is unavailable. The feature remains disabled by
  default and is not part of a release deployment.

- Structured file patches now retain a complete managed backup reference and
  a redacted, encrypted unified diff in the same transaction as the successful
  step outcome. Missing rollback evidence fails closed instead of reporting a
  safely recoverable change.

## [1.0.4] - 2026-07-30

### Fixed

- Dashboard asset, user, and group totals now exclude soft-deleted records, so
  the overview cards and resource distribution chart stay consistent with the
  corresponding management lists after records are deleted.

## [1.0.3] - 2026-07-30

### Added

- China mainland one-line Compose deployment through a fixed Gitee release tag,
  the project backend on Tencent Cloud TCR, and digest-pinned DaoCloud public
  mirrors for the official Nginx, Redis, and MySQL images. All public dependency
  image references remain operator-overridable.

- Full bilingual UI (Simplified Chinese / English): every page, menu,
  dialog, and Element Plus built-in string follows the interface language.
  Switch instantly under Settings → Appearance & Language; the choice is
  persisted in `t_settings.language` (rev51 migration) and applied on the
  login page before sign-in. The AI assistant answers in the configured
  language, and a `check-i18n` build gate keeps the two locales in key
  parity with no hard-coded UI strings.

- First-run web setup wizard (`/setup`): when required configuration (MySQL,
  secret key, Fernet keys) is missing, the backend boots into a minimal
  wizard app instead of failing. The wizard validates connectivity, creates
  the schema and an administrator account (replacing the seeded weak
  `admin/admin` row), writes configuration to `<data dir>/runtime.env`
  (0600), and restarts the worker automatically. Guarded by a one-time
  setup token, origin checks, and a completion sentinel; `OGS_SETUP_MODE`
  supports `off`/`force`. Broken configurations on an already-configured
  system now land in a read-only maintenance page instead of a crash loop.

- Dashboard "AI operations executions" panel: a 7-day stacked success/failure
  bar chart backed by the new `GET /ai/stats` endpoint, which aggregates
  existing `t_command_log` rows of type `AI 批量命令` (no schema change).

- Web AI operations assistant with permission-filtered platform tools,
  server-side result sets, visible tool events, conversation history, and
  approval-gated batch commands.
- OpenAI-compatible Provider presets, encrypted API Key storage, model
  discovery, Tool Calling verification, and explicit enable/default controls.
- 256K standard context mode and an opt-in 1M deep-diagnostic context mode for
  Providers whose capability is explicitly declared by an administrator.
- Server-owned read-only Linux and Docker diagnostic profiles, encrypted
  evidence, deterministic cited findings, Runbook guidance, per-asset progress,
  and owner-scoped diagnostic APIs.
- Public documentation center, AI user/configuration/API guides, unified
  upgrade procedure, trust-boundary documentation, and community health files.

### Fixed

- The versioned installer now normalizes only the packaged frontend static
  assets to Nginx-readable permissions, preventing `/setup` from returning 500
  after a restrictive fixed-tag checkout while keeping generated secrets 0600.

- Every documented deployment path now actually works, verified end-to-end
  (deployment audit): `orange.sql` is loadable for the first time — seed
  INSERTs use explicit column lists, FK target columns got the required
  indexes, and FK column charsets are aligned (verified against a real
  `mysql:8.0` initdb, now guarded by a CI job). Physical-machine preflight no
  longer fails on its own bugs (literal `'***'` passwords, nonexistent
  `Config` class); install scripts fix a long-standing bash syntax error,
  actually create the `app_user` database account, and no longer write the
  root password into the backend env. Compose host mode gets a working
  `make docker-up-host`; `make docker-up` is re-entrant; the physical nginx
  config now serves the frontend SPA; systemd/supervisor instructions and
  path layouts are consistent; `CHANGE_ME` placeholders are rejected at
  startup; MySQL 8 `caching_sha2_password` verified working out of the box.

### Changed

- AI operations page redesign: model and context-mode selectors moved into the
  composer toolbar, assistant replies render Markdown (tables, lists, code),
  approval cards use a horizontal status strip, the context sidebar only shows
  sections that have data, and switching model/context asks before starting a
  new conversation.
- Batch operation canvas colors now map to the global theme tokens so the
  batch command/script pages render correctly in the dark theme.
- Page containers are full-width (the previous 1600px cap is removed); the
  dashboard AI card reuses the sidebar `Cpu` icon for consistency.
- Batch command and script pages now use a three-stage operation canvas with
  real asset and credential data, an explicit local configuration check,
  authoritative per-asset results, and retry for failed assets.
- Batch execution remains synchronous and no longer presents simulated
  per-host progress before the server returns a final response.
- Batch scripts accept UTF-8 `.sh` and `.py` files up to 1 MB and use fixed
  `bash` or `python3` interpreters; legacy response fields remain available
  alongside structured per-asset `items[]`.
- The legacy `put_type=send` mode remains upload-only and compatible; the new
  batch script page uses the separately validated `put_type=sh` path.
- Tool running and completed states render as one timeline record.
- Batch execution results remain available in the conversation and distinguish
  success, partial failure, failure, rejection, cancellation, and expiry.
- Follow-up messages use the latest authoritative action state instead of the
  stale pending snapshot from action creation.
- Dashboard AI status uses the same numeric card structure as other summary
  metrics.
- Public project licensing is consistently documented as Apache-2.0.

### Security

- Batch commands and scripts revalidate asset access, credential use,
  asset-credential authorization, soft-deletion state, duplicate and empty
  targets, a 50-host limit, and dangerous input before any remote operation.
- Provider API Keys are encrypted server-side and never returned by the API.
- Provider destinations reject private, loopback, and link-local addresses
  unless the administrator explicitly enables a controlled private gateway.
- AI execution revalidates owner, conversation, expiry, asset authorization,
  system-user authorization, target limits, and dangerous-command rules.

### Upgrade notes

- Existing installations enabling AI must apply `rev48_ai_provider.sql`,
  `rev49_ai_context_window.sql`, and `rev50_ai_diagnostics.sql` in order.
- Follow [the unified upgrade procedure](docs/operations/UPGRADE.md); do not
  execute isolated migration snippets from older documentation.
