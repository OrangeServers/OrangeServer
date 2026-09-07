# AI operations

OrangeServer's AI operations workbench keeps conversations, monitoring
investigations, alerts, and controlled Autonomy Runs inside the same permission
boundary as every human user. Chat is the read-only analysis entry point;
remote writes continue through the existing approval-gated Run workflow.

![AI operations](/screens/ai-agent.png)
![AI operations on a narrow screen](/screens/ai-agent-narrow.png)

## Start at the workbench

The primary routes are:

- `/ai-ops` — one workbench for the current conversation and recent task rail;
- `/ai-ops/tasks` — tasks grouped by attention, running, and completed state;
- `/ai-ops/alerts` — Runs entered through Alertmanager;
- `/ai-knowledge` — reviewed Runbooks and verified-run retrieval.

The four starter cards in an empty conversation fill a real prompt but do not
send it automatically. Add the asset, service, or symptom, then choose the
Autonomy profile before sending.

## Monitoring analysis

An administrator configures read-only Prometheus, Grafana, Loki, and Zabbix
sources under the monitoring-source settings and confirms their asset mappings.
The Agent discovers the available metrics, log streams, panels, and monitoring
items, then chooses bounded queries from the user's prompt. It can compare time
windows and sources; it is not limited to a fixed CPU or availability check.

The server controls destinations, credentials, labels, query shape, time range,
sample and response limits. Monitoring analysis in chat is read-only. When a
change, restart, or continued investigation is needed, the user explicitly
creates an Autonomy Run.

## Controlled Autonomy Runs

Administrators and ordinary users can create and manage their own single-host
Runs when the asset and system-account combination is authorized. A Run records
the plan, approvals, actions, evidence, independent verification, and final
conclusion. The server fixes the target, account, profile, budget, and action
allowlist; the model never receives an unrestricted shell.

The four-container bundled install uses one Redis 8 service split into DB0
checkpoint/vector data, DB1 Celery broker, and DB2 sessions/cache, plus a
prefork Worker. `ask`, `ai_review`, and `custom` remain bounded by server-side
approval rules; `auto` is restricted to `lab` assets.

## Knowledge

`/ai-knowledge` is a first-level entry. Users can search authorized global and
host-scoped metadata and citations; only administrators can add or change
Runbooks, approve sources, configure embeddings, or rebuild the index. The
index accepts reviewed Markdown Runbooks and verified resolved Runs, not chat,
credentials, raw SSH output, or unsanitized logs.

## What it cannot do

- It cannot run SQL or open an unrestricted shell.
- Chat monitoring analysis is read-only. Run writes are governed by the selected
  profile and server-side approval policy; `auto` is limited to assets marked
  `lab` and still cannot elevate a `deny`.
- It cannot invent asset IDs, database fields, or execution results — tool
  results are the only source of truth.
- Tool output, history summaries, and diagnostic evidence are treated as
  untrusted low-privilege data: instructions embedded in them are never
  followed.

## Evidence and audit

Every tool call, approval, monitoring observation, and execution is recorded.
Diagnostic findings and monitoring observations are citable; Run conclusions
reference Evidence from the current investigation and independent verification.

## Learn more

- [AI user guide](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/USER_GUIDE.md)
- [Providers and context modes](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/PROVIDER_AND_CONTEXT.md)
- [Read-only diagnostics](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/DIAGNOSTICS.md)
- [API reference](https://github.com/OrangeServers/OrangeServer/blob/main/docs/ai/API.md)
