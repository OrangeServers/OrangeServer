# Agent working agreement

This file defines the repository rules for coding agents. Read it before
changing files. User instructions and security policy take precedence.

## 1. Orient before editing

Run these checks first:

```powershell
git status --short --branch
git remote -v
git branch -vv
```

Then read the files that own the affected behavior, the linked Issue or work
package, and the relevant documentation entry in `docs/README.md`.

Stop and report the mismatch instead of guessing when:

- the working tree contains unrelated changes that overlap the task;
- the branch is not based on the current public `origin/main`;
- the Issue, code, migration, API contract, and documentation disagree;
- required credentials, infrastructure, fixtures, or acceptance criteria are
  missing.

## 2. Repository lineage

- Public `origin/main` is the only canonical product baseline.
- A milestone integration branch is a short-lived exception used only when an
  Issue names it as the PR target. The integration branch itself starts from
  the latest public `origin/main`, is never a release source, and is deleted
  after its final PR is merged to `main`.
- A work branch starts from the PR target named by its Issue: normally the
  latest `origin/main`, or the current milestone integration branch. Never use
  another feature branch, an old integration branch, or a staging default
  branch as its base.
- A private staging remote, when explicitly approved, is only storage for an
  unpublished branch. It does not have an independent product `main`.
- Never merge, rebase, or copy an old staging `main` into a release branch.
- Never push an unpublished branch to the public repository. Public branches
  and pull requests are public disclosure.
- Before the first public push, inspect the full commit range and diff, run the
  relevant tests, and perform the privacy checks in this file.
- Merging, deployment, publishing, remote branch deletion, and other external
  writes require explicit user authorization.

Create a normal branch with the Issue's target branch as its base:

```powershell
git fetch origin <target-branch>
git switch -c <type>/<short-name> origin/<target-branch>
git merge-base --is-ancestor origin/<target-branch> HEAD
```

If the ancestry check fails, do not repair it by merging unrelated histories.
Create a clean branch from the correct target and port only reviewed changes.

## 3. Work from a bounded contract

For non-trivial work, the Issue or work package must identify:

- goal and user-visible outcome;
- non-goals and modules that must not change;
- dependencies and locked interfaces or states;
- security and privacy invariants;
- exact acceptance commands.

Do not add adjacent features, speculative abstractions, dependencies, public
plugin systems, compatibility layers, or generated scaffolding unless the task
requires them. Reuse the existing architecture and keep the diff focused.

For AI and remote-execution work, preserve these boundaries:

- the server, never the model, authorizes actions;
- permissions and ownership are revalidated before side effects;
- credentials are passed by reference and never enter prompts or events;
- remote and model output is untrusted, sanitized, redacted, and bounded;
- an uncertain write result is not automatically replayed or reported as
  success.

## 4. Implement and verify

1. Trace the existing flow and its callers before editing shared behavior.
2. Make the smallest coherent change that satisfies the task.
3. Add or update tests for changed behavior.
4. Update the canonical public documentation for user-visible changes.
5. Run checks proportional to the diff. Documentation-only changes use:

   ```powershell
   pwsh -File ops/check-docs.ps1
   ```

6. Review `git diff --check`, `git status`, and the final diff before handoff.

Do not claim completion from a green CI badge, an old test report, or a service
that merely started. Report exactly what ran, what passed, and what was not
verified.

## 5. Privacy and public-release gate

Never commit real credentials, API keys, tokens, cookies, private keys, private
hostnames, internal addresses, deployment paths, audit data, or user data.
This includes fixtures, screenshots, terminal output, comments, commit messages,
and deleted-file history in the branch being published.

Use reserved examples such as `example.com`, `192.0.2.0/24`,
`198.51.100.0/24`, and `203.0.113.0/24`, plus clearly fake credentials.

Before the first public push, at minimum review:

```powershell
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
git diff origin/main...HEAD
pwsh -File ops/check-docs.ps1
```

Also run the repository's secret/privacy scan when one is defined by the work
package. If sensitive data was ever committed, removing it from the latest tree
is not enough: stop, report it, rotate real credentials, and clean the published
history through an explicitly approved procedure.

## 6. Documentation ownership

Follow `docs/WRITING.md`. Do not describe planned behavior as released, copy
migration commands into multiple files, maintain volatile test counts, or turn
temporary implementation notes into public contracts.

## 7. Handoff

The final report must state:

- outcome and files changed;
- checks run and their results;
- checks not run and why;
- migrations, compatibility, security, or privacy impact;
- remaining risks or follow-up work.

Leave unrelated user changes untouched. Do not stage or commit them.
