# Contributing to OrangeServer

Thank you for helping improve OrangeServer. Contributions are accepted under
the [Apache License 2.0](LICENSE).

## Before opening an issue

- Use the documentation index and search existing issues first.
- Use a public issue for reproducible bugs and focused feature proposals.
- Follow [SECURITY.md](SECURITY.md) for vulnerabilities; do not disclose them
  in a public issue.
- Remove credentials, private addresses, hostnames, audit data, deployment
  paths, command output, and user information from logs and screenshots.

## Development setup

```bash
git clone https://github.com/OrangeServers/OrangeServer.git
cd OrangeServer
cp .env.example .env
cp backend/.env.example backend/.env
make install
make dev
```

Use test-only credentials and infrastructure. Never copy production or private
test-machine configuration into the repository.

## Working on a change

1. Keep changes focused and avoid unrelated formatting or generated-file churn.
2. Preserve existing authorization, CSRF, audit, secret-handling, and SSH safety
   boundaries.
3. Add or update tests for behavior changes.
4. Update public documentation for user-visible configuration, API, migration,
   security, or operational changes.
5. Add an entry under `Unreleased` in [CHANGELOG.md](CHANGELOG.md).
6. Do not hand-maintain volatile test counts in documentation.

AI and automation changes require special care:

- the model must never be an authorization source;
- structured tool input must be validated by the server;
- execution must revalidate ownership and permissions;
- remote output and model output must be treated as untrusted;
- secrets must not enter logs, API responses, prompts, events, or fixtures;
- planned APIs must not be documented as released.

## Repository lineage and pre-disclosure work

The public `origin/main` branch is the only canonical product baseline. Start
a normal change from its latest revision:

```bash
git fetch origin main
git switch -c <type>/<short-name> origin/main
git merge-base --is-ancestor origin/main HEAD
```

An explicitly approved private staging remote may store an unpublished feature
branch, but it is not a second product line and its default branch must never be
merged into public work. When disclosure is approved, review and test the same
branch, then push that branch to the public repository and open a pull request.
Do not convert changes between unrelated public and private histories.

A large milestone may use a short-lived public `integration/<milestone>` branch
created from `origin/main`. Its Issue must name that branch as the PR target;
work branches then start from the current integration branch. Integration
branches accept only that milestone, are never tagged or deployed as releases,
and are deleted after a final reviewed PR merges them into `main`. Do not keep a
permanent `develop` branch or turn an integration branch into a second product
line.

Before the first public push:

- inspect every commit and the full diff from `origin/main`;
- verify that the branch still descends from public `origin/main`;
- run the checks required by the change;
- remove private infrastructure data and use reserved example addresses;
- scan the complete branch range for secrets and sensitive history.

If the ancestry check fails, create a clean branch from `origin/main` and port
only reviewed changes. Do not solve it by merging unrelated histories.

## Local checks

Run checks proportional to the change. For application code, the normal
baseline is:

```bash
cd backend
python -m pytest
flake8 app tests

cd ../frontend
npm run type-check
npm run build
```

For documentation-only changes, run the repository documentation check:

```bash
pwsh -File ops/check-docs.ps1
```

If a check cannot run locally, state exactly what was not run and why in the
pull request.

## Pull requests

- Explain the user-visible outcome and the security/compatibility impact.
- Link the issue when one exists.
- List database migrations and rollback steps.
- Include desktop and narrow-screen screenshots for visual changes, with all
  environment-specific data removed.
- Report the exact tests run and their result.
- Keep public documentation in sync with the implementation.

Reviewers may ask for changes when a patch expands authorization, execution,
network access, secret exposure, or data retention without explicit tests and
documentation.

## Documentation style

- Follow the ownership and release-state rules in the
  [documentation writing guide](docs/WRITING.md).
- English `README.md` is the concise international landing page.
- `README.zh-CN.md` and `docs/` contain the detailed Chinese documentation.
- [The upgrade guide](docs/operations/UPGRADE.md) is the only source for
  database migration commands.
- Internal development notes are not public product contracts.
- Use neutral example domains, users, addresses, and paths.

## Conduct

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
