# Contributing

Keep PostRadar focused on user-configured feed subscriptions. Domain-specific
business rules belong in private user configuration, not application defaults.

## Local setup

Use Python 3.12 or 3.13 and install the locked development dependencies:

```powershell
uv sync --locked --group dev
uv run pytest -q
uv run ruff check .
uv run mypy src
```

Read [the architecture](docs/ARCHITECTURE.md) before changing worker or persistence
behavior. Changes to externally observable state transitions need meaningful tests.
Use fake credentials, HTTP transports and SMTP connections. Automated tests must
not call paid APIs, contact real feeds or send email.

## Changes and review

- Keep configuration, source adapters, evaluation, storage and delivery separate.
- Keep timestamps in UTC and persist queued work before dispatching it.
- Preserve lease fencing and explicit handling of uncertain SMTP delivery.
- Treat remote text as data; validate all structured model output.
- Preserve TLS verification and fail closed when a secure credential store is unavailable.
- Use only invented examples and reserved example domains in fixtures and documentation.
- Update user-facing documentation when behavior or commands change.

When updating dependencies, regenerate `uv.lock` and review the changed dependency
set. GitHub Actions are pinned to full commit hashes. Verify replacement commits
against the action's official repository rather than copying an unverified hash.

## Before publication

```powershell
uv build
uv run python scripts/check_release.py --source --artifacts dist
```

The source check reads Git-tracked filenames, so review the staged diff and file list
as well. It must not be used to justify adding an entire working directory blindly.
Inspect the built wheel and source archive as separate release artifacts. A clean
scan does not replace review for personal information, real content, provenance or
license obligations.

Do not attach production logs, local configurations, runtime state, databases or
private audit reports to a pull request. Report security issues through the process
in [SECURITY.md](SECURITY.md).
