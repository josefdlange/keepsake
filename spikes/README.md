# Spikes

Throwaway scripts that exercise the real libraries against real accounts, to confirm behavior
before writing the backends. Secrets and sessions go to `~/.local/share/keepsake/spike/`.

    uv run python spikes/keep_spike.py --email you@gmail.com --title "Groceries" [--wait-for-edit]
    uv run python spikes/reminders_spike.py --apple-id you@icloud.com --list "Groceries"
