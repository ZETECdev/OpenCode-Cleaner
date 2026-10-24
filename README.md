<h1 align="center">🧹 OpenCode Cleaner</h1>

<p align="center">
  <b>Safely reclaim gigabytes from your OpenCode data folder</b><br>
  (opencode.db messages, tool outputs, snapshot objects, logs)
</p>

---

OpenCode (desktop + CLI) accumulates a LOT over time: the message history of every
conversation, full tool outputs nobody reads anymore, git snapshot objects and logs.
On a busy setup `opencode.db` alone can reach **10+ GB**.

**OpenCode Cleaner** is a small interactive script that cleans all of that up,
**without touching anything you still use**.

## What it cleans

| Step | Target | Notes |
|------|--------|-------|
| `history` | Messages of **closed** conversations | Cannot be undone |
| `sessions` | The closed session records themselves | Cleaner, also cannot be undone |
| `tool-output` | Tool output files not used by the kept conversations | |
| `snapshots` | Unused git objects inside the internal snapshot repos | Your real project repos are never touched |
| `vacuum` | Compacts `opencode.db` | Without it, deleted space does not return to disk |
| `log` | Log files | |

## What is always kept

- 📌 Conversations currently **open as tabs** in the OpenCode desktop app
  (they are read from the app's `drafts.sqlite`)
- ⛓️ Their **ancestors** and **fork sources** (the family tree is walked automatically)
- ➕ Any extra session id you type during the wizard (or `--keep` on the CLI)

## ⚠️ Before running

OpenCode must not be writing while the cleaner works, so:

1. **Close OpenCode completely** — the desktop app *and* any `bun` processes still
   running it. Check the system tray and Task Manager (look for `OpenCode` and `bun`).
2. If something is still open, the script tells you exactly what it found and
   **waits — press any key to retry** once you have closed it (Ctrl+C to quit).
3. Run it:

```
Start OpenCode Cleaner.cmd        (double-click, Windows)
python clean_opencode.py          (or directly)
```

## The wizard

1. **STEP 1** – pick what to clean (numbers, `all`, `none`, or just **Enter** for the defaults).
2. **Open tabs are always kept automatically.** Optionally type extra
   **closed** conversation ids you want to keep (Enter = keep none).
   The 15 heaviest closed conversations are listed to help you decide.
3. **STEP 3** – minimum size to delete: `0` (default) = delete *all* closed
   conversations; `5` = only those of 5 MB or more.
4. **STEP 4** – also delete the closed session records (recommended, Enter = yes).
5. **STEP 5** – optional backup copy of `opencode.db` (recommended, Enter = yes).
6. Review the **summary** → `Run now` → done.

> 💡 **Big databases:** the first scan reads the whole database. On a 10 GB
> database this can take a couple of minutes — the script prints dots (`.....`)
> every half second while working, so dots = busy, **not frozen**. The backup
> copy and `vacuum` also print dots instead of going silent.
>
> 💡 After finishing (or cancelling), **press any key to run again** — handy if you
> want to repeat with different options. Ctrl+C quits.

## Non-interactive mode

```bash
python clean_opencode.py                                # dry run: shows the plan, deletes NOTHING
python clean_opencode.py --apply                        # everything, asks YES first
python clean_opencode.py --apply --yes                  # everything, no questions
python clean_opencode.py --apply --targets snapshots,log
python clean_opencode.py --apply --min-mb 20            # only closed conversations >= 20 MB
python clean_opencode.py --apply --purge-sessions       # also delete the closed session records
python clean_opencode.py --apply --keep ses_xxx,ses_yyy # extra sessions to protect
python clean_opencode.py --apply --backup "D:\Backups\opencode.db"
python clean_opencode.py --force                        # ignore the OpenCode/bun running check (own risk)
```

## Safety notes

- Deleting history **cannot be undone** — the wizard offers a backup copy by default
  (`backup-opencode-<date>.db`, next to the database).
- `vacuum` needs free disk space roughly equal to the size of `opencode.db`;
  it is skipped automatically if there is not enough.
- Deleting history without `vacuum` frees space *inside* the database file but not on disk.
- `--force` while OpenCode is running can corrupt the database — don't.

## Requirements

- Windows 10/11
- Python 3.9+ (`python` on PATH)
- `git` on PATH only for the `snapshots` step

## How it works (short version)

- Open-tab protection: reads the `state` table of the desktop app's `drafts.sqlite`
  (`tabs` + `tabs.info` of every window).
- History: deletes rows in `session_v2` / `session_message` / `session_inbox` /
  `session_pending` of every unprotected closed conversation.
- Snapshots: for each repo under `snapshot/`, computes the git objects reachable
  from the snapshots of the kept conversations (`git cat-file` + `git rev-list`)
  and deletes every loose object that is not reachable.
- Everything else: plain file deletion in `tool-output/` and `log/`.

---

*Runs at your own risk — it deletes data. Make the backup when the wizard offers it.* 🧹
