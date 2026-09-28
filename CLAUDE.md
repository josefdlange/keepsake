# keepsake

Python daemon that two-way syncs one Google Keep checklist with one Apple Reminders list. Runs
unattended under systemd on a Raspberry Pi (ARM64). No Mac is involved.

## WARNING: both service APIs are unofficial

- Google Keep uses `gkeepapi`, a reverse-engineered client for Keep's internal sync API.
- Apple Reminders uses `pyicloud`'s CloudKit-backed `RemindersService`.

Neither library has a stable, documented contract. **Never trust memory, docs, or the task
description for their APIs.** Before writing or changing backend code, read the installed source
under `.venv/lib/python3.*/site-packages/{gkeepapi,pyicloud}/` and confirm method names,
signatures, model attributes, and exception types. When upgrading either library, re-read the
source and re-run the spikes in `spikes/`.

## Commands

- `uv sync`: install
- `uv run pytest`: tests (no test may touch the network)
- `uv run ruff check . && uv run ruff format .`: lint and format
- `uv run pyright`: type check (strict)

## Layout and conventions

- `src/keepsake/`: package (`src/` layout); CLI entry point `keepsake` -> `keepsake.cli:app`.
- Layers stay separate:
  - `model.py`: `Item` and text normalization
  - `backend.py`: `Backend` Protocol and in-memory `FakeBackend`
  - `state.py`: SQLite state store (migrations via `PRAGMA user_version`)
  - `planner.py`: pure `plan()` with no I/O
  - `executor.py`: applies operations and updates base state
  - `engine.py`: one sync pass with the safety guards
  - `keep_backend.py`, `reminders_backend.py`: real backends (verified facts in their docstrings)
  - `icloud.py`, `auth.py`: iCloud session reuse and the interactive auth flows
  - `config.py`, `paths.py`, `app.py`, `cli.py`: config, XDG paths, wiring, CLI
- The planner must stay pure and deterministic, with no network, clock, or randomness. All
  behavior is covered by table-driven tests in `tests/`.
- Change detection compares exact text; pairing compares normalized text (trim, collapse
  whitespace, casefold). Always write the original text, never the normalized form.
- Safety first: an empty or truncated fetch must never cascade into mass deletion. Auth failures
  stop all writes.
- Secrets live under `~/.local/share/keepsake/` (dir 0700, files 0600). Never put them in the repo,
  config, argv, env vars, or logs.
- Never let unattended code retry an Apple ID password: a few failed logins lock the account
  (error -20209). The daemon only reuses the saved session (one keyring attempt at most).
- Never call pyicloud's `lists()` outside `keepsake auth icloud`: it walks the whole zone (~60s).
- Personal tool: keep code straightforward, no frameworks, no async.
