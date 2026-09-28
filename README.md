# keepsake

Two-way sync between **one Google Keep checklist** and **one Apple Reminders list**, built for a
shared household grocery list: one person uses Reminders on iOS, the other uses Keep on Android,
and both edit at the same time. It runs unattended as a systemd service on a Raspberry Pi. No Mac
is needed.

> **Both service APIs are unofficial.** Keep goes through [`gkeepapi`](https://github.com/kiwiz/gkeepapi)
> and Reminders through [`pyicloud`](https://github.com/timlaing/pyicloud)'s CloudKit service.
> Either can break when Google or Apple change something. `keepsake doctor` prints the exact
> library versions to include in a bug report.

## How it works

- **Three-way diff.** For every mapped pair, keepsake stores the last-synced text and checked
  state (the *base*). Each pass compares both sides against the base:
  - A change on one side is copied to the other.
  - The same change on both sides only updates the base.
  - A text edit on both sides is resolved by the newer timestamp. **Keep has no per-item
    timestamps** (it sends 1970 placeholders), so in practice the `tie_breaker` setting decides
    (default: Keep wins).
- **Matching.** New items are paired with an unmapped item on the other side if their
  normalized text matches (case, spacing), so simultaneous adds don't duplicate. Otherwise the
  item is created on the other side.
- **Checked items.** Unmapped items that are already checked are ignored. Your Reminders history
  of completed items is never copied into Keep. Once an item is mapped, its checked state syncs
  normally.
- **Deletes.**
  - A delete on one side is copied to the other, unless the other side changed the item: then
    the item is recreated.
  - Deleted on one side and only checked on the other both mean "done": the pair is unlinked and
    nothing is recreated.
  - Reminders deletes are soft and land in *Recently Deleted*.
- **Cheap polling.** Reminders is polled through CloudKit's change feed, about 1.3 s per pass.
  A full re-read runs at startup and hourly, about 3 s. Keep's sync is incremental and takes
  well under a second.

### Safety guards

- **Suspicious empty fetch.** If a side returns zero items while unchecked items are mapped, the
  pass is skipped, never turned into mass deletion. Clearing a fully checked list is fine.
- **Deletion threshold.** A pass that would delete more than `max_deletes` unchecked items, or
  more than `max_delete_fraction` of mapped items, is not executed. It's logged, you're
  notified, and it needs `keepsake sync --once --force`. Deleting checked items after shopping
  never counts.
- **Auth failures.** On any auth failure, all writes stop. keepsake logs loudly, notifies you,
  and keeps retrying with backoff, reusing the saved credentials only. It **never retries your
  Apple ID password**: a few failed logins lock the account (Apple error -20209).
- **No overwriting concurrent edits.** If a reminder changed since keepsake read it, iCloud
  rejects the write (CONFLICT). The write is retried with fresh data next pass instead of
  overwriting the edit.
- **Dry run.** `--dry-run` prints the exact operation plan and writes nothing.

## Setup on a Raspberry Pi

These steps are for Raspberry Pi OS 64-bit (Bookworm or later, Python 3.11+).

1. **Install uv and clone the repo:**
   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh
   git clone <your remote> ~/keepsake
   cd ~/keepsake
   uv sync
   ```

2. **Create the config** (no secrets go in it):
   ```sh
   mkdir -p ~/.config/keepsake
   cp deploy/config.example.toml ~/.config/keepsake/config.toml
   nano ~/.config/keepsake/config.toml
   ```

3. **Authenticate Google Keep:**
   ```sh
   uv run keepsake auth keep
   ```
   It walks you through getting an `oauth_token` cookie:
   - Open `https://accounts.google.com/EmbeddedSetup` in a desktop browser, sign in, and click
     *I agree*.
   - In dev tools, copy the `oauth_token` cookie.
   - Paste it promptly; it expires within minutes.

   keepsake exchanges it once for a long-lived master token and verifies it by fetching the
   checklist.

4. **Authenticate iCloud:**
   ```sh
   uv run keepsake auth icloud
   ```
   - It asks for your Apple ID password **once** per run, then a 2FA code (approve the prompt on
     your iPhone or Mac). A security key plugged into the Pi also works.
   - Only the trusted session is saved. The password is not stored unless you set
     `use_keyring = true`, which a headless Pi usually can't support anyway.
   - Looking up the list by name takes about a minute, because pyicloud reads the whole zone.
     If you already know the list id (e.g. from a previous run), pass `--list-id List/...` to
     skip the lookup.
   - **If login fails, don't keep retrying.** First sign in at icloud.com to check the
     password.

5. **Preview the first sync, then run it:**
   ```sh
   uv run keepsake sync --once --dry-run
   uv run keepsake sync --once
   ```
   The first sync merges both lists by text and never deletes anything.

6. **Check that iOS accepts keepsake's writes** before enabling the service. pyicloud rewrites
   the whole title document on every update, even a checked-only one. It also sends fresh
   conflict-resolution metadata. Whether iOS merges that cleanly hasn't been verified yet. After
   the first sync:
   - On Android, check an item that was added on the iPhone, then run `keepsake sync --once`.
     The iPhone should show it checked, title unchanged.
     - It should still be checked after the phone syncs again.
     - A second `sync --once` should plan 0 operations.
   - On the iPhone, rename an item, then run `sync --once`. Keep should show exactly the new
     text.
   - On Android, add an item, then run `sync --once`. It should appear on the iPhone.

   **Don't enable the service if you see any of these:**
   - a check that never shows up, or reverts later
   - a doubled title (e.g. "MilkMilk")
   - later passes planning updates nobody made

7. **Enable the service:**
   ```sh
   sudo cp deploy/keepsake@.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now keepsake@$USER
   journalctl -u keepsake@$USER -f
   ```

### iCloud requirements

- **Advanced Data Protection.** If ADP is on, enable *Settings → [your name] → iCloud → Access
  iCloud Data on the Web* on your iPhone. Otherwise pyicloud's CloudKit access fails.
- **The list must be in your own account.** Lists shared *with* you live in someone else's
  CloudKit zone and are not visible to pyicloud.
- **First access can be slow.** The first full read of a list may fail with `TRY_AGAIN_LATER`
  while CloudKit builds an index. keepsake waits and retries automatically.

## Everyday commands

| Command | What it does |
| --- | --- |
| `keepsake status` | Auth state, item counts, last sync, last error, cursor age (no network) |
| `keepsake doctor` | Live connectivity and auth checks for both sides, permissions, library versions |
| `keepsake sync --once [--dry-run] [--force]` | One full pass; `--force` overrides the guards |
| `keepsake run` | The daemon (what the service runs) |
| `keepsake -v ...` | Debug logging, including the duration of every backend call |

`keepsake sync --once` and the daemon share a lock, so stop the service before a manual forced
sync:
```sh
sudo systemctl stop keepsake@$USER
uv run keepsake sync --once --force
sudo systemctl start keepsake@$USER
```

## Notifications

Set `[notify] webhook_url` to get a plain-text POST when auth fails or a guard trips, at most
once per issue per `cooldown_hours`. [ntfy.sh](https://ntfy.sh) with a private topic works well.
That's your cue to SSH in and re-run `keepsake auth ...` or review the blocked plan.

## Files

| Path | Contents |
| --- | --- |
| `~/.config/keepsake/config.toml` | Settings (no secrets) |
| `~/.local/share/keepsake/` (mode 700) | `keep_master_token`, `keep_android_id`, `icloud/` session, `state.db` |

All files under the data directory are mode 600. Secrets never appear in argv, environment
variables, the unit file, or logs.

## Development

```sh
uv run pytest        # no test touches the network
uv run ruff check . && uv run ruff format .
uv run pyright
```

- **Spikes.** `spikes/` holds the scripts used to verify library behavior against real accounts.
  Re-run them after upgrading `gkeepapi` or `pyicloud`.
- **Library docs.** See `CLAUDE.md` for project conventions. The docstrings of
  `keep_backend.py` and `reminders_backend.py` record what was verified about each library.

## License

[MIT](LICENSE). keepsake is not affiliated with or endorsed by Google or Apple, and it relies on
unofficial, reverse-engineered APIs that may break or change at any time. Use at your own risk.
