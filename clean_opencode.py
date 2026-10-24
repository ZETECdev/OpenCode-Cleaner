#!/usr/bin/env python3
"""
clean_opencode.py - Assisted cleanup tool for the OpenCode data folder.

Run it WITHOUT arguments (or with --interactive) and it asks step by step
what you want to clean:

    STEP 1   what to clean (history, sessions, tool outputs, snapshots, ...)
    STEP 2   optional: extra CLOSED conversations to keep
             (open tabs are ALWAYS kept automatically)
    STEP 3   minimum size of the closed conversations to delete
    STEP 4   also delete the closed session rows themselves
    STEP 5   optional backup copy of opencode.db
    then     a summary and a final confirmation

The conversations currently open as tabs in the OpenCode desktop app (read
from drafts.sqlite) are ALWAYS kept, plus their ancestors, their fork
sources, and any extra session id you type.

NOTE: on big databases (10+ GB) the first scan takes a while. The script
prints dots while working, so "....." means it is busy, NOT frozen.

SAFETY
    - It never touches the real git repositories of your projects.
    - It shows the whole plan and asks before deleting anything.
    - It refuses to run while OpenCode (or its Bun server) is running,
      unless --force is used.

NON-INTERACTIVE MODE (optional)
    python clean_opencode.py                              # dry run, deletes nothing
    python clean_opencode.py --apply                      # everything, asks YES
    python clean_opencode.py --apply --yes                # everything, no questions
    python clean_opencode.py --apply --targets snapshots,log
    python clean_opencode.py --apply --min-mb 20
    python clean_opencode.py --apply --purge-sessions
    python clean_opencode.py --apply --keep ses_xxx,ses_yyy
    python clean_opencode.py --apply --backup "D:\\Backups\\opencode.db"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

try:  # make sure Windows consoles print accents/symbols fine
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOME = os.path.expanduser("~")
DEFAULT_DATA = os.path.join(HOME, ".local", "share", "opencode")
DEFAULT_DRAFTS = os.path.join(
    os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming")),
    "ai.opencode.desktop",
    "drafts.sqlite",
)

TARGET_INFO = [
    ("history", "Message history of closed conversations"),
    ("sessions", "Closed session records (includes their history)"),
    ("tool-output", "Tool outputs not used by the kept conversations"),
    ("snapshots", "Unused snapshot (git) objects"),
    ("vacuum", "Compact opencode.db (reclaims disk space)"),
    ("log", "Log files"),
]
WIZARD_KEYS = [key for key, _ in TARGET_INFO]
DEFAULT_TARGETS = ["log", "tool-output", "history", "snapshots", "vacuum"]
ALL_TARGETS = WIZARD_KEYS


# --------------------------------------------------------------------------- #
# console helpers
# --------------------------------------------------------------------------- #
def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0 or unit == "TB":
            return "%d B" % n if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


def log(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    log()
    log("=" * 92)
    log(title)
    log("=" * 92)


def ask(prompt: str, default: str | None = None) -> str:
    suffix = " [%s]" % default if default is not None else ""
    try:
        answer = input("%s%s: " % (prompt, suffix)).strip()
    except EOFError:
        answer = ""  # EOF never picks an option by itself
    if not answer and default is not None:
        return default
    return answer


def ask_bool(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    prompt = "%s (%s)" % (prompt, hint)
    try:
        answer = input("%s: " % prompt).strip().lower()
    except EOFError:
        return False  # EOF = cancel, never "yes" by accident
    if not answer and default:
        return default
    return answer[:1] in ("y",)


def _stdin_is_console() -> bool:
    """True only when stdin is an interactive Windows console (a human can press keys).

    Note: with stdin from NUL/DEVNULL, isatty() lies (returns True), so we ask
    the OS whether the handle really is a console (GetConsoleMode).
    """
    if os.name != "nt":
        try:
            return sys.stdin.isatty()
        except Exception:
            return False
    try:
        import msvcrt
        import ctypes
        from ctypes import wintypes

        fd = sys.stdin.fileno()
        mode = wintypes.DWORD()
        return bool(ctypes.windll.kernel32.GetConsoleMode(msvcrt.get_osfhandle(fd),
                                                         ctypes.byref(mode)))
    except Exception:
        return False


def wait_key(text: str) -> bool:
    """Show text, wait for a single key press. True = continue, False = quit.

    Only a real Windows console can deliver a keypress; when stdin is not one
    (pipes, NUL, schedulers, tests) report quit instead of hanging forever.
    """
    print("\n%s" % text, flush=True)
    if _stdin_is_console():
        import msvcrt

        key = msvcrt.getwch()
        if key in ("\x00", "\xe0"):  # arrow / function keys send two parts
            msvcrt.getwch()
        if key in ("\x03", "\x1b"):  # Ctrl+C or Esc = quit
            raise KeyboardInterrupt
        return True
    return False


_TICK = {"last": 0.0, "line": 0}


def _tick(min_gap: float = 0.5) -> None:
    """Print at most one dot every `min_gap` seconds (live feedback, never frozen-looking)."""
    now = time.time()
    if now - _TICK["last"] >= min_gap:
        _TICK["last"] = now
        print(".", end="", flush=True)
        _TICK["line"] += 1
        if _TICK["line"] >= 40:  # wrap the dot line after 40 dots
            _TICK["line"] = 0
            print()


def attach_progress(con: sqlite3.Connection) -> None:
    """Make slow SQLite queries print live dots instead of looking frozen."""

    def _cb() -> int:
        _tick()
        return 0

    try:
        con.set_progress_handler(_cb, 20000)
    except Exception:
        pass


def end_dots() -> None:
    """Close the dot line (only when dots were printed) and reset the ticker."""
    if _TICK["line"]:
        print()
    _TICK["line"] = 0
    _TICK["last"] = 0.0


def open_ro(path: str) -> sqlite3.Connection:
    """Read-only SQLite connection that waits politely on locks."""
    con = sqlite3.connect(
        "file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=30
    )
    attach_progress(con)
    return con


# --------------------------------------------------------------------------- #
# pre-flight checks
# --------------------------------------------------------------------------- #
def opencode_processes() -> list[str]:
    """Names of the OpenCode / Bun processes currently running."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=90,
        ).stdout
    except Exception:
        return []
    names = set()
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        name = line.split('","')[0].strip('"')
        low = name.lower()
        if low in ("bun.exe", "bun") or "opencode" in low:
            names.add(name)
    return sorted(names)


def preflight_blockers(data_dir: str, force: bool) -> list[str]:
    db = os.path.join(data_dir, "opencode.db")
    blockers: list[str] = []
    if not os.path.isdir(data_dir):
        blockers.append("the OpenCode data folder does not exist: %s" % data_dir)
    elif not os.path.isfile(db):
        blockers.append("the OpenCode database does not exist: %s" % db)
    procs = opencode_processes()
    if procs and not force:
        blockers.append("OpenCode is still running (%s)" % ", ".join(procs))
    elif procs:
        log("   WARNING: OpenCode processes detected (%s) but --force is active."
            % ", ".join(procs))
    if not blockers:
        wal = db + "-wal"
        if os.path.isfile(wal) and os.path.getsize(wal) > 0:
            log("   note: %s not merged into opencode.db yet; it will be integrated while compacting."
                % human(os.path.getsize(wal)))
    return blockers


def ensure_preflight(data_dir: str, force: bool, interactive: bool) -> bool:
    """Print every blocker. Interactive users can close the app and RETRY."""
    while True:
        blockers = preflight_blockers(data_dir, force)
        if not blockers:
            return True
        running = any("still running" in b for b in blockers)
        log()
        for b in blockers:
            log("  ! %s" % b)
        if running:
            log()
            log("  Close OpenCode COMPLETELY (check the system tray too) and also any")
            log("  'bun' processes still running it (Task Manager > OpenCode / Bun).")
            log("  Then press a key to retry.")
        if not interactive:
            return False
        if not wait_key("Press any key to retry (Ctrl+C to quit)..."):
            return False


def read_open_sessions(drafts_path: str) -> tuple[list[str], dict]:
    """Ids of the desktop tabs that are open + their titles/directories."""
    if not os.path.isfile(drafts_path):
        return [], {}
    con = sqlite3.connect(
        "file:%s?mode=ro" % drafts_path.replace("\\", "/"), uri=True, timeout=30
    )
    try:
        rows: list = []
        info: dict = {}
        for name, key, value in con.execute("select name,key,value from state"):
            if key == "tabs" and ".window." in name:
                try:
                    data = json.loads(value)
                except ValueError:
                    continue
                if isinstance(data, list):
                    rows.extend(data)
            elif key == "tabs.info":
                try:
                    data = json.loads(value)
                except ValueError:
                    continue
                if isinstance(data, dict):
                    info.update(data)
    finally:
        con.close()
    sessions = [t["sessionId"] for t in rows if t.get("type") == "session"]
    titles = {}
    for key, value in info.items():
        if "/session/" in key:
            titles[key.split("/session/")[-1]] = value
    return sessions, titles


def dir_size(path: str) -> tuple[int, int]:
    total = files = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                pass
    return total, files


def force_remove(path: str) -> None:
    """Windows: loose git objects are read-only."""
    os.chmod(path, 0o666)
    os.remove(path)


# --------------------------------------------------------------------------- #
# protected conversations
# --------------------------------------------------------------------------- #
def protect_set(cur: sqlite3.Cursor, keep: list[str]) -> set[str]:
    """Kept ids + ancestors + fork sources."""
    protected: set[str] = set()
    pending = list(keep)
    while pending:
        sid = pending.pop()
        if sid in protected:
            continue
        row = cur.execute(
            "select parent_id, fork_session_id from session_v2 where id=?", (sid,)
        ).fetchone()
        if not row:
            continue
        protected.add(sid)
        for ref in row:
            if ref:
                pending.append(ref)
    if keep:
        marks = ",".join("?" * len(keep))
        for (src,) in cur.execute(
            "select distinct fork_session_id from session_v2 "
            "where fork_session_id is not null and id in (%s)" % marks,
            keep,
        ):
            if src and src not in protected:
                protected.add(src)
    return protected


def lookup_sessions(cur: sqlite3.Cursor, ids: list[str]) -> list[tuple[str, str, str]]:
    out = []
    for sid in ids:
        row = cur.execute(
            "select coalesce(title,''), directory from session_v2 where id=?", (sid,)
        ).fetchone()
        if row:
            out.append((sid, row[0], row[1]))
    return out


# --------------------------------------------------------------------------- #
# single message scan (history sizes + live tool-output references)
# --------------------------------------------------------------------------- #
TOOL_ID_RE = re.compile(r"tool_[A-Za-z0-9]{8,40}")


def scan_messages(cur: sqlite3.Cursor):
    """Sizes of every stored conversation, computed by SQLite in one aggregation.

    Returns counts as {session_id: [n_messages, bytes]} for ALL sessions.
    SQLite computes length() from the record headers, so it does not even need
    to read the full bodies of the messages.
    """
    counts: dict[str, list] = {}
    rows = cur.execute(
        "select session_id, count(*), total(length(data)) "
        "from session_message group by session_id"
    ).fetchall()
    for sid, n, size in rows:
        counts[sid] = [int(n), int(size or 0)]
    return counts


def protected_tool_refs(cur: sqlite3.Cursor, protected: set[str], known: set[str]) -> set[str]:
    """Tool outputs still mentioned by the messages of the KEPT conversations."""
    referenced: set[str] = set()
    if not known or not protected:
        return referenced
    target = len(known)
    for sid in protected:
        for (data,) in cur.execute(
            "select data from session_message where session_id=?", (sid,)
        ):
            if "tool_" not in data:
                continue
            for token in TOOL_ID_RE.findall(data):
                if token in known:
                    referenced.add(token)
                    if len(referenced) == target:
                        return referenced
    return referenced


def build_history_rows(cur: sqlite3.Cursor, counts: dict, protected: set[str]) -> list[tuple]:
    """(session_id, n_messages, bytes, title) of every unprotected conversation, biggest first."""
    meta = dict(cur.execute("select id, coalesce(title,'') from session_v2").fetchall())
    rows = [
        (sid, n, size, meta.get(sid, ""))
        for sid, (n, size) in counts.items()
        if sid not in protected
    ]
    rows.sort(key=lambda r: -r[2])
    return rows


# --------------------------------------------------------------------------- #
# history deletion
# --------------------------------------------------------------------------- #
def chunks(seq, size=400):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def apply_history(cur: sqlite3.Cursor, selected: list, purge_sessions: bool) -> None:
    ids = [s[0] for s in selected]
    actions = [("session_message", "session_id", "delete from %s where %s in (%s)")]
    if purge_sessions:
        actions.append(("session_inbox", "session_id", "delete from %s where %s in (%s)"))
        actions.append(("session_pending", "session_id", "delete from %s where %s in (%s)"))
        actions.append(("session_v2", "id", "delete from %s where %s in (%s)"))
    for batch in chunks(ids):
        marks = ",".join("?" * len(batch))
        for table, column, sql in actions:
            cur.execute(sql % (table, column, marks), batch)


# --------------------------------------------------------------------------- #
# tool outputs
# --------------------------------------------------------------------------- #
def tool_files(out_dir: str) -> list[str]:
    if not os.path.isdir(out_dir):
        return []
    return [
        f for f in os.listdir(out_dir) if os.path.isfile(os.path.join(out_dir, f))
    ]


def plan_tool_output(out_dir: str, referenced: set):
    files = tool_files(out_dir)
    orphans = [os.path.join(out_dir, f) for f in files if f not in referenced]
    return orphans, sum(os.path.getsize(p) for p in orphans), len(files)


# --------------------------------------------------------------------------- #
# snapshots
# --------------------------------------------------------------------------- #
def snapshot_refs(cur: sqlite3.Cursor, protected: set[str]) -> set[str]:
    found: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "snapshot" and isinstance(value, dict):
                    for slot in ("start", "end"):
                        val = value.get(slot)
                        if isinstance(val, str) and len(val) == 40:
                            found.add(val)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for sid in protected:
        for (data,) in cur.execute(
            "select data from session_message where session_id=?", (sid,)
        ):
            try:
                walk(json.loads(data))
            except ValueError:
                pass
        row = cur.execute("select revert from session_v2 where id=?", (sid,)).fetchone()
        if row and row[0]:
            try:
                walk(json.loads(row[0]))
            except ValueError:
                pass
    return found


def snapshot_repos(snap_dir: str):
    if not os.path.isdir(snap_dir):
        return []
    out = []
    for proj in sorted(os.listdir(snap_dir)):
        pdir = os.path.join(snap_dir, proj)
        if not os.path.isdir(pdir):
            continue
        for sub in sorted(os.listdir(pdir)):
            repo = os.path.join(pdir, sub)
            if not os.path.isfile(os.path.join(repo, "config")):
                continue
            worktree = ""
            try:
                with open(os.path.join(repo, "config"), encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if "worktree" in line and "=" in line:
                            worktree = line.split("=", 1)[1].strip()
            except OSError:
                pass
            out.append((repo, worktree))
    return out


def loose_objects(repo: str):
    base = os.path.join(repo, "objects")
    res = []
    if not os.path.isdir(base):
        return res
    for sub in os.listdir(base):
        if len(sub) != 2:
            continue
        d = os.path.join(base, sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            fp = os.path.join(d, name)
            if os.path.isfile(fp):
                res.append((sub + name, fp))
    return res


def git_available() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    except Exception:
        return False


def snapshot_plan(cur: sqlite3.Cursor, snap_dir: str, protected: set[str]):
    hashes = snapshot_refs(cur, protected)
    plan = []
    for repo, worktree in snapshot_repos(snap_dir):
        objs = loose_objects(repo)
        label = worktree or repo
        log("      analyzing %-52s objects=%d" % (label[:52], len(objs)))
        roots = []
        if hashes:
            r = subprocess.run(
                ["git", "--git-dir=" + repo, "cat-file", "--batch-check"],
                input="\n".join(sorted(hashes)) + "\n",
                capture_output=True,
                text=True,
            )
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    roots.append(parts[0])
        keep: set[str] = set()
        if roots:
            r = subprocess.run(
                ["git", "--git-dir=" + repo, "rev-list", "--objects",
                 "--no-object-names", *roots],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                log("      ! rev-list failed on %s: %s" % (repo, r.stderr.strip()[:160]))
            keep = {line for line in r.stdout.splitlines() if line}
        gone = [(n, fp) for n, fp in objs if n not in keep]
        plan.append({
            "repo": repo,
            "worktree": worktree or "?",
            "objects": len(objs),
            "roots": len(roots),
            "kept": len(objs) - len(gone),
            "gone": gone,
            "gone_bytes": sum(os.path.getsize(fp) for _, fp in gone),
            "index": os.path.join(repo, "index"),
        })
    return plan, len(hashes)


def apply_snapshot_plan(plan) -> tuple[int, int, int]:
    removed = freed = errors = 0
    for p in plan:
        here = 0
        for _name, fp in p["gone"]:
            try:
                freed += os.path.getsize(fp)
                force_remove(fp)
                removed += 1
                here += 1
                if removed % 25000 == 0:
                    log("      ... %s objects removed (%s)" % (f"{removed:,}", human(freed)))
            except OSError as exc:
                errors += 1
                if errors <= 5:
                    log("      ! could not delete %s: %s" % (fp, exc))
        if here and os.path.isfile(p["index"]):
            try:
                force_remove(p["index"])
            except OSError as exc:
                log("      ! could not delete %s: %s" % (p["index"], exc))
    return removed, freed, errors


# --------------------------------------------------------------------------- #
# vacuum / logs
# --------------------------------------------------------------------------- #
def _copy_with_dots(src: str, dst: str) -> int:
    """Copy src to dst printing live dots (a 10+ GB backup copy is silent otherwise)."""
    total = os.path.getsize(src)
    copied = 0
    try:
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            while True:
                chunk = fsrc.read(8 * 1024 * 1024)
                if not chunk:
                    break
                fdst.write(chunk)
                copied += len(chunk)
                _tick()
    except OSError:
        try:
            os.remove(dst)  # do not leave a partial backup behind
        except OSError:
            pass
        raise
    end_dots()
    return copied


def apply_vacuum(db: str) -> None:
    before = os.path.getsize(db)
    try:
        disk_free = shutil.disk_usage(os.path.dirname(db)).free
    except OSError:
        disk_free = 0
    if disk_free and disk_free < before:
        log("   !! VACUUM needs about %s free and only %s is available; step skipped."
            % (human(before), human(disk_free)))
        return
    log("   compacting %s ... (this may take several minutes; dots = working)"
        % human(before))
    t0 = time.time()
    result: dict = {}

    def _work() -> None:
        con = None
        try:
            con = sqlite3.connect(db, isolation_level=None)
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("VACUUM")
            con.execute("PRAGMA optimize")
            result["ok"] = True
        except Exception as exc:  # surfaced after joining the thread
            result["error"] = exc
        finally:
            if con is not None:
                con.close()

    thread = threading.Thread(target=_work, daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(timeout=2.0)
        if thread.is_alive():
            _tick(2.0)
    end_dots()
    if "error" in result:
        log("   !! vacuum failed: %s" % result["error"])
        return
    after = os.path.getsize(db)
    log("   done in %.1fs: %s -> %s (freed %s)"
        % (time.time() - t0, human(before), human(after), human(max(0, before - after))))


def plan_log(log_dir: str):
    files = []
    if os.path.isdir(log_dir):
        for name in os.listdir(log_dir):
            fp = os.path.join(log_dir, name)
            if os.path.isfile(fp):
                files.append(fp)
    return files, sum(os.path.getsize(f) for f in files)


# --------------------------------------------------------------------------- #
# wizard
# --------------------------------------------------------------------------- #
def wizard(data_dir: str, drafts_path: str) -> dict | None:
    db = os.path.join(data_dir, "opencode.db")
    snap_dir = os.path.join(data_dir, "snapshot")
    out_dir = os.path.join(data_dir, "tool-output")
    log_dir = os.path.join(data_dir, "log")

    con = open_ro(db)
    cur = con.cursor()
    total_sessions = cur.execute("select count(*) from session_v2").fetchone()[0]
    open_ids, info = read_open_sessions(drafts_path)

    rule("OPENCODE CLEANING WIZARD")
    log("Data folder : %s" % data_dir)
    log("Database    : %s   (%s conversations in total)"
        % (human(os.path.getsize(db)), f"{total_sessions:,}"))
    log()
    log("Checking folder sizes ...")
    for name, path in (("snapshot", snap_dir), ("tool-output", out_dir), ("log", log_dir)):
        size, files = dir_size(path) if os.path.isdir(path) else (0, 0)
        log("  %-18s %10s   (%d files)" % (name, human(size), files))
    log()

    if open_ids:
        log("Conversations currently OPEN (always kept): %d" % len(open_ids))
        for sid, title, _directory in lookup_sessions(cur, open_ids):
            log("    %s  %s" % (sid, (info.get(sid) or {}).get("title") or title))
    else:
        log("No open tabs were found in drafts.sqlite.")
        log("WARNING: only conversations you type by hand below will be kept!")

    # ---- 1) what to clean ------------------------------------------------- #
    rule("STEP 1  What do you want to clean?")
    titles = dict(TARGET_INFO)
    for key in ("history", "sessions"):
        titles[key] += "   <-- cannot be undone"
    chosen = ask_multi(WIZARD_KEYS, titles, [k for k in WIZARD_KEYS if k in DEFAULT_TARGETS])
    if not chosen:
        log("\nNothing selected; there is nothing to do.")
        cur.close()
        con.close()
        return None
    if "sessions" in chosen and "history" not in chosen:
        chosen.insert(0, "history")
    if "history" in chosen and "vacuum" not in chosen:
        log()
        log("   Note: deleting history without compacting does NOT free disk space.")
        if ask_bool("   Add the 'vacuum' step", True):
            chosen.append("vacuum")

    # ---- one single message scan (reused everywhere afterwards) ----------- #
    counts = None
    if "history" in chosen or "tool-output" in chosen:
        log()
        log("   Scanning all stored conversations ...")
        log("   (this reads the whole database; a 10+ GB one can take a couple of minutes)")
        log("   (one dot every half second = working, not frozen)")
        t0 = time.time()
        counts = scan_messages(cur)
        end_dots()
        log("   done in %.1fs: %s messages in %s conversations."
            % (time.time() - t0, f"{sum(n for n, _ in counts.values()):,}", f"{len(counts):,}"))

    keep: list[str] = []
    min_mb = 0.0
    selected: list = []
    if "history" in chosen:
        protected0 = protect_set(cur, open_ids)
        rows = build_history_rows(cur, counts, protected0)

        # ---- 2) extra conversations to keep ------------------------------- #
        rule("STEP 2  Closed conversations to KEEP (optional)")
        log("   %s closed conversations can still be deleted (%s of data)."
            % (f"{len(rows):,}", human(sum(r[2] for r in rows))))
        log("   The OPEN tabs listed above are ALWAYS kept automatically.")
        log()
        log("   The 15 heaviest closed conversations:")
        for sid, _n, size, title in rows[:15]:
            log("      %-34s %9s  %s" % (sid, human(size), (title or "")[:40]))
        log()
        extra = ask("   Extra session ids to KEEP, comma separated (Enter = keep none)", "")
        for piece in re.split(r"[,\s]+", extra):
            piece = piece.strip()
            if not piece or piece in open_ids or piece in keep:
                continue
            if cur.execute("select 1 from session_v2 where id=?", (piece,)).fetchone():
                keep.append(piece)
                log("      + will be kept: %s" % piece)
            else:
                log("      ! unknown conversation id: %s" % piece)
        if keep:
            newly = protect_set(cur, open_ids + keep) - protected0
            rows = [r for r in rows if r[0] not in newly]
            log("   (%d extra conversation(s) kept in total)" % len(keep))

        # ---- 3) minimum size ---------------------------------------------- #
        rule("STEP 3  Minimum size to delete")
        log("   0 = delete ALL closed conversations that are not kept.")
        log("   5 = only delete closed conversations of 5 MB or more.")
        for limit in (0, 1, 5, 20, 50):
            n = sum(1 for r in rows if r[2] / 1048576.0 >= limit)
            size = sum(r[2] for r in rows if r[2] / 1048576.0 >= limit)
            log("      >= %-4s MB : %6s conversations, %s" % (limit, f"{n:,}", human(size)))
        raw = ask("   Minimum size in MB (0 = all)", "0")
        try:
            min_mb = max(0.0, float(raw.replace(",", ".")))
        except ValueError:
            min_mb = 0.0
        selected = [r for r in rows if r[2] / 1048576.0 >= min_mb]

        # ---- 4) session rows ---------------------------------------------- #
        rule("STEP 4  Delete the closed session rows too?")
        log("   'history' deletes the messages; conversations disappear from the list,")
        log("   but the session records stay inside the database.")
        log("   'sessions' deletes the records completely (cleaner, also cannot be undone).")
        if ask_bool("   Delete the closed session rows completely", True):
            if "sessions" not in chosen:
                chosen.append("sessions")
        else:
            chosen = [c for c in chosen if c != "sessions"]

    # ---- 5) backup --------------------------------------------------------- #
    rule("STEP %d  Backup copy" % (5 if "history" in chosen else 2))
    backup = None
    if "history" in chosen:
        log("   Deleting history cannot be undone. You can back up opencode.db first")
        log("   (it currently takes %s)." % human(os.path.getsize(db)))
        if ask_bool("   Create a backup copy", True):
            default_backup = os.path.join(
                os.path.dirname(db),
                "backup-opencode-%s.db" % time.strftime("%Y%m%d-%H%M%S"),
            )
            raw = ask("   Backup path", default_backup)
            backup = raw.strip('"').strip()
            try:
                free = shutil.disk_usage(os.path.dirname(os.path.abspath(backup))).free
                if free < os.path.getsize(db):
                    log("   !! only %s free on that drive." % human(free))
                    if not ask_bool("   Continue anyway", False):
                        backup = None
            except OSError:
                pass
    else:
        log("   No history will be deleted, so no backup is required.")

    # ---- snapshot analysis (reused at execution) ---------------------------- #
    snap_plan = []
    snap_refs = 0
    if "snapshots" in chosen:
        log()
        log("   Analyzing snapshot repositories ...")
        if git_available():
            snap_plan, snap_refs = snapshot_plan(cur, snap_dir, protect_set(cur, open_ids + keep))
            end_dots()
        else:
            log("   ! git is not on PATH; this step will be skipped.")

    # ---- tool outputs of the kept conversations (summary) ------------------- #
    tool_refs = None
    if "tool-output" in chosen:
        tool_refs = protected_tool_refs(cur, protect_set(cur, open_ids + keep),
                                        set(tool_files(out_dir)))

    # ---- summary ------------------------------------------------------------ #
    rule("SUMMARY - WHAT WILL HAPPEN")
    if selected:
        log("   history     : %s messages from %s conversations  (%s)"
            % (f"{sum(r[1] for r in selected):,}", f"{len(selected):,}",
               human(sum(r[2] for r in selected))))
        log("                 session rows deleted too: %s"
            % ("yes" if "sessions" in chosen else "no"))
    if tool_refs is not None:
        orphans, size, total = plan_tool_output(out_dir, tool_refs)
        log("   tool-output : %d unused files of %d (%s)" % (len(orphans), total, human(size)))
    if "snapshots" in chosen:
        doomed = sum(len(p["gone"]) for p in snap_plan)
        bytes_doomed = sum(p["gone_bytes"] for p in snap_plan)
        log("   snapshots   : %s unused objects (%s)" % (f"{doomed:,}", human(bytes_doomed)))
        log("                 %d snapshots of the kept conversations stay untouched" % snap_refs)
    if "vacuum" in chosen:
        log("   vacuum      : compact opencode.db (%s)" % human(os.path.getsize(db)))
    if "log" in chosen:
        lfiles, lsize = plan_log(log_dir)
        log("   log         : %d files (%s)" % (len(lfiles), human(lsize)))
    if backup:
        log("   backup      : %s" % backup)
    log()
    log("   ALWAYS kept: %s open conversation(s)%s."
        % (f"{len(open_ids):,}", (" + %d you typed" % len(keep)) if keep else ""))

    cur.close()
    con.close()

    log()
    if not ask_bool("Run now", True):
        log("Cancelled. Nothing was touched.")
        return None

    return {
        "targets": chosen,
        "min_mb": min_mb,
        "purge_sessions": "sessions" in chosen,
        "keep": keep,
        "backup": backup,
        "selected": selected,
        "snap_plan": snap_plan,
        "snap_refs": snap_refs,
    }


def ask_multi(keys: list[str], titles: dict[str, str], preselected: list[str]) -> list[str]:
    log()
    log("What do you want to clean?")
    for i, key in enumerate(keys, 1):
        mark = "x" if key in preselected else " "
        log("    %d) [%s] %s" % (i, mark, titles[key]))
    log()
    log("    Type numbers separated by commas (e.g. 1,3,4)")
    log("    all = everything   none = nothing   Enter = the checked ones")
    answer = ask("Choice", "").strip().lower()
    if answer == "":
        return list(preselected)
    low = answer
    if low in ("all", "everything"):
        return list(keys)
    if low in ("none", "nothing"):
        return []
    chosen: list[str] = []
    for piece in re.split(r"[,\s]+", low):
        if piece.isdigit() and 1 <= int(piece) <= len(keys):
            key = keys[int(piece) - 1]
            if key not in chosen:
                chosen.append(key)
    return chosen


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def run(cfg: dict, data_dir: str, drafts_path: str) -> int:
    db = os.path.join(data_dir, "opencode.db")
    snap_dir = os.path.join(data_dir, "snapshot")
    out_dir = os.path.join(data_dir, "tool-output")
    log_dir = os.path.join(data_dir, "log")
    targets = cfg["targets"]

    con = open_ro(db)
    cur = con.cursor()
    open_ids, info = read_open_sessions(drafts_path)
    keep = list(cfg.get("keep") or [])
    known = lookup_sessions(cur, open_ids + keep)
    unknown = [s for s in keep if s not in {k[0] for k in known}]
    kept_ids = [k[0] for k in known]
    protected = protect_set(cur, kept_ids)

    rule("CONVERSATIONS THAT WILL BE KEPT (%d)" % len(kept_ids))
    for sid, title, directory in known:
        log("  %s  %-46s %s"
            % (sid, directory, ((info.get(sid) or {}).get("title") or title)[:38]))
    if unknown:
        log("  (unknown ids ignored: %s)" % ", ".join(unknown))
    if len(protected) != len(kept_ids):
        log("  + %d related (ancestors/forks) protected automatically"
            % (len(protected) - len(kept_ids)))

    if cfg.get("backup"):
        rule("BACKUP COPY")
        log("   copying %s -> %s" % (db, cfg["backup"]))
        log("   (dots = still copying; a 10+ GB backup takes a while)")
        os.makedirs(os.path.dirname(os.path.abspath(cfg["backup"])), exist_ok=True)
        _copy_with_dots(db, cfg["backup"])
        for suffix in ("-wal", "-shm"):
            if os.path.isfile(db + suffix):
                shutil.copy2(db + suffix, cfg["backup"] + suffix)
        log("   done: %s" % human(os.path.getsize(cfg["backup"])))

    cur.close()
    con.close()

    freed_total = 0
    writable = sqlite3.connect(db, timeout=30)
    attach_progress(writable)
    wcur = writable.cursor()

    if "history" in targets:
        rule("DELETING CLOSED-CONVERSATION HISTORY")
        selected = cfg.get("selected")
        if selected is None:
            log("   scanning conversations ... (dots = working)")
            t0 = time.time()
            counts = scan_messages(wcur)
            end_dots()
            log("   scan finished in %.1fs" % (time.time() - t0))
            rows = build_history_rows(wcur, counts, protected)
            selected = [r for r in rows if r[2] / 1048576.0 >= cfg["min_mb"]]
        log("   conversations affected : %s" % f"{len(selected):,}")
        log("   messages               : %s" % f"{sum(r[1] for r in selected):,}")
        log("   data                   : %s" % human(sum(r[2] for r in selected)))
        for sid, n, size, title in selected[:10]:
            log("      %-34s %9s  %6d msgs  %s" % (sid, human(size), n, (title or "")[:34]))
        if selected:
            apply_history(wcur, selected, cfg["purge_sessions"])
            writable.commit()
            freed_total += sum(r[2] for r in selected)
            log("   -> deleted%s"
                % (" (session rows too)" if cfg["purge_sessions"] else ""))
        else:
            log("   (nothing to delete)")

    if "tool-output" in targets:
        rule("DELETING UNUSED TOOL OUTPUTS")
        referenced = protected_tool_refs(wcur, protected, set(tool_files(out_dir)))
        orphans, size, total = plan_tool_output(out_dir, referenced)
        log("   total files      : %d" % total)
        log("   unused (orphans) : %d (%s)" % (len(orphans), human(size)))
        done = freed = 0
        for fp in orphans:
            try:
                s = os.path.getsize(fp)
                force_remove(fp)
                done += 1
                freed += s
            except OSError as exc:
                log("      ! %s: %s" % (fp, exc))
        freed_total += freed
        log("   -> deleted %d (%s)" % (done, human(freed)))

    if "snapshots" in targets:
        rule("PRUNING SNAPSHOT OBJECTS")
        if not git_available():
            log("   ! git is not on PATH; step skipped.")
        else:
            plan = cfg.get("snap_plan")
            nrefs = cfg.get("snap_refs", 0)
            if plan is None:
                log("   analyzing snapshot repositories ...")
                plan, nrefs = snapshot_plan(wcur, snap_dir, protected)
                end_dots()
            log("   snapshots referenced by the kept conversations: %d" % nrefs)
            for p in plan:
                if p["objects"]:
                    log("   %-38s objects=%-6d roots=%-4d keep=%-6d prune=%-6d %s"
                        % (p["worktree"], p["objects"], p["roots"], p["kept"],
                           len(p["gone"]), human(p["gone_bytes"])))
            removed, freed, errors = apply_snapshot_plan(plan)
            freed_total += freed
            log("   -> deleted %s objects (%s), %d errors"
                % (f"{removed:,}", human(freed), errors))

    wcur.close()
    writable.close()

    if "vacuum" in targets:
        rule("COMPACTING opencode.db")
        apply_vacuum(db)

    if "log" in targets:
        rule("DELETING LOGS")
        files, size = plan_log(log_dir)
        log("   files : %d (%s)" % (len(files), human(size)))
        done = freed = 0
        for fp in files:
            try:
                s = os.path.getsize(fp)
                force_remove(fp)
                done += 1
                freed += s
            except OSError as exc:
                log("      ! could not delete %s: %s" % (fp, exc))
        freed_total += freed
        log("   -> deleted %d (%s)" % (done, human(freed)))

    if "vacuum" not in targets and freed_total:
        rule("NOTICE")
        log("   opencode.db was NOT compacted; the space inside the database")
        log("   will not return to the disk until you run this again with 'vacuum'.")

    rule("SUMMARY")
    log("   disk space freed (approx.) : %s" % human(freed_total))
    if os.path.isfile(db):
        log("   current opencode.db size   : %s" % human(os.path.getsize(db)))
    for name in ("snapshot", "tool-output", "log"):
        path = os.path.join(data_dir, name)
        if os.path.isdir(path):
            size, files = dir_size(path)
            log("   %-22s %10s (%d files)" % (name, human(size), files))
    end_dots()
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Cleanup of the OpenCode data folder (interactive by default).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--interactive", "-i", action="store_true",
                    help="force the wizard even when other arguments are given")
    ap.add_argument("--data-dir", default=DEFAULT_DATA, help="OpenCode data folder")
    ap.add_argument("--drafts", default=DEFAULT_DRAFTS, help="drafts.sqlite of the desktop app")
    ap.add_argument("--apply", action="store_true", help="direct mode: apply without the wizard")
    ap.add_argument("--yes", action="store_true", help="direct mode: skip the final YES question")
    ap.add_argument("--force", action="store_true",
                    help="run even if OpenCode/Bun are still running (own risk)")
    ap.add_argument("--targets", default=None,
                    help="direct mode: comma separated list of steps (%s)" % ",".join(ALL_TARGETS))
    ap.add_argument("--min-mb", type=float, default=None,
                    help="direct mode: only delete history of closed conversations >= N MB")
    ap.add_argument("--purge-sessions", action="store_true",
                    help="direct mode: also delete the closed session rows")
    ap.add_argument("--keep", default="", help="extra session ids to keep, comma separated")
    ap.add_argument("--backup", default=None,
                    help="copy opencode.db to this path before touching anything")

    args = ap.parse_args()
    data_dir = os.path.abspath(args.data_dir)
    drafts_path = os.path.abspath(args.drafts)

    # interactive by default when nothing concrete is requested
    interactive = args.interactive or (
        not args.apply
        and args.targets is None
        and args.min_mb is None
        and not args.purge_sessions
        and not args.keep
        and not args.backup
        and not args.yes
    )
    if interactive and not _stdin_is_console() and not args.interactive:
        # no real console behind stdin (pipes, NUL, schedulers): behave like a dry run
        interactive = False

    if interactive:
        while True:  # the window stays open; press a key to run again, Ctrl+C to quit
            rule("OPENCODE CLEANER")
            log("data: %s" % data_dir)
            if not ensure_preflight(data_dir, args.force, interactive=True):
                return 1
            cfg = wizard(data_dir, drafts_path)
            if cfg is not None:
                run(cfg, data_dir, drafts_path)
            log()
            if not wait_key("Press any key to run again (Ctrl+C to quit)..."):
                break
        return 0

    # ---------------- non-interactive (direct) mode ---------------- #
    targets = ([t.strip() for t in args.targets.split(",") if t.strip()]
               if args.targets else list(DEFAULT_TARGETS))
    bad = [t for t in targets if t not in ALL_TARGETS]
    if bad:
        log("!! unknown steps: %s" % ", ".join(bad))
        log("   valid steps: %s" % ", ".join(ALL_TARGETS))
        return 2
    if "sessions" in targets and "history" not in targets:
        targets.insert(0, "history")
    if "history" in targets and "vacuum" not in targets:
        log("   note: history without vacuum; the space stays used until opencode.db is compacted.")

    rule("OPENCODE CLEANER")
    log("data: %s" % data_dir)
    log("mode: %s" % ("apply" if args.apply else "dry run"))
    if not ensure_preflight(data_dir, args.force, interactive=False):
        return 1

    cfg = {
        "targets": targets,
        "min_mb": args.min_mb or 0.0,
        "purge_sessions": args.purge_sessions or "sessions" in targets,
        "keep": [s.strip() for s in args.keep.split(",") if s.strip()],
        "backup": args.backup if args.apply else None,
    }

    if not args.apply:
        # dry run: report without touching anything
        con = open_ro(os.path.join(data_dir, "opencode.db"))
        cur = con.cursor()
        open_ids, info = read_open_sessions(drafts_path)
        kept_raw = open_ids + cfg["keep"]
        protected = protect_set(cur, kept_raw)
        rule("DRY RUN - NOTHING IS DELETED")
        for sid, title, directory in lookup_sessions(cur, kept_raw):
            log("  %s  %-46s %s" % (sid, directory,
                                    ((info.get(sid) or {}).get("title") or title)[:38]))
        log("  kept conversations (+ancestors/forks): %d" % len(protected))

        counts = None
        if "history" in targets or "tool-output" in targets:
            log("   scanning conversations ... (dots = working)")
            t0 = time.time()
            counts = scan_messages(cur)
            end_dots()
            log("   scan finished in %.1fs" % (time.time() - t0))
        if "history" in targets:
            rows = build_history_rows(cur, counts, protected)
            selected = [r for r in rows if r[2] / 1048576.0 >= cfg["min_mb"]]
            log("   history     : would delete %s messages from %s closed conversations (%s)"
                % (f"{sum(r[1] for r in selected):,}", f"{len(selected):,}",
                   human(sum(r[2] for r in selected))))
            if cfg["purge_sessions"]:
                log("                 session rows would be deleted too")
        if "tool-output" in targets:
            tool_refs = protected_tool_refs(cur, protected, set(tool_files(os.path.join(data_dir, "tool-output"))))
            orphans, size, total = plan_tool_output(os.path.join(data_dir, "tool-output"), tool_refs)
            log("   tool-output : %d unused files of %d (%s)" % (len(orphans), total, human(size)))
        if "snapshots" in targets:
            if git_available():
                plan, nrefs = snapshot_plan(cur, os.path.join(data_dir, "snapshot"), protected)
                end_dots()
                doomed = sum(len(p["gone"]) for p in plan)
                log("   snapshots   : %d referenced | would delete %s objects (%s)"
                    % (nrefs, f"{doomed:,}", human(sum(p["gone_bytes"] for p in plan))))
            else:
                log("   snapshots   : ! git not available; step skipped")
        if "log" in targets:
            files, size = plan_log(os.path.join(data_dir, "log"))
            log("   log         : would delete %d files (%s)" % (len(files), human(size)))
        cur.close()
        con.close()
        log()
        rule("DRY RUN DONE")
        log("   nothing was deleted. Run again with --apply to really execute it.")
        return 0

    if not args.yes:
        try:
            answer = input("\nThis deletes data permanently. Type YES to continue: ")
        except EOFError:
            answer = ""
        if answer.strip().upper() not in ("YES", "Y"):
            log("canceled by the user; nothing was touched.")
            return 0

    return run(cfg, data_dir, drafts_path)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\ninterrupted by the user")
        sys.exit(130)
