"""Read-only, foreground-owned Claude discovery for an omitted Herdr reference.

Only the Linux interactive CLI registry observed in Claude Code 2.1.296 is
supported. No cwd/mtime guess or bridge/private-state registration is made.
"""

import glob
import json
import os
import re
from typing import NamedTuple

import firstmate
import tg

SESSIONS = os.path.expanduser("~/.claude/sessions")
PROJECTS = os.path.expanduser("~/.claude/projects")
PROC = "/proc"
SESSION_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


class Source(NamedTuple):
    pane_id: str
    cwd: str
    pid: int
    start_ticks: str
    session_id: str
    started_at: int
    path: str
    file_identity: tuple[int, int]


def missing(session: dict | None) -> bool:
    """An unusable explicit reference is still explicit; never override it."""
    return session is None or (isinstance(session, dict)
                               and not (session.get("kind") or session.get("value")))


def _process(pid: int, cwd: str) -> str:
    directory = os.path.join(PROC, str(pid))
    if os.stat(directory).st_uid != os.getuid():
        raise ValueError("foreign process")
    with open(os.path.join(directory, "comm"), encoding="utf-8") as f:
        if f.read().strip() != "claude":
            raise ValueError("foreground process is not Claude")
    if os.readlink(os.path.join(directory, "cwd")) != cwd:
        raise ValueError("process cwd mismatch")
    with open(os.path.join(directory, "stat"), encoding="utf-8") as f:
        ticks = f.read().rsplit(")", 1)[1].split()[19]
    if not ticks.isdigit() or int(ticks) <= 0:
        raise ValueError("invalid process lifetime")
    return ticks


def _transcript(session_id: str, cwd: str) -> tuple[str, tuple[int, int]]:
    # UUID validation precedes the glob: registry values cannot introduce a
    # path or a wildcard. Duplicate exact UUIDs are ambiguous, regardless of age.
    hits = glob.glob(os.path.join(PROJECTS, "*", session_id + ".jsonl"))
    if len(hits) != 1:
        raise ValueError("exact transcript unavailable or ambiguous")
    path = hits[0]
    verified = False
    with open(path, "rb") as f:
        stat = os.fstat(f.fileno())
        lines = f.read(600_000).splitlines(keepends=True)
    for line in lines:
        if not line.endswith(b"\n"):
            continue  # bounded or still-being-written record
        row = json.loads(line)
        if row.get("type") not in ("user", "assistant") or row.get("isSidechain"):
            continue
        if row.get("sessionId") != session_id or row.get("cwd") != cwd:
            raise ValueError("transcript session/cwd mismatch")
        verified = True
    if not verified:
        raise ValueError("native transcript identity unavailable")
    return path, (stat.st_dev, stat.st_ino)


def _snapshot(cfg: dict, pane_id: str, info: dict) -> Source:
    cwd = firstmate.main_cwd(cfg)
    if (not firstmate.enabled(cfg) or not pane_id
            or info.get("pane_id") != pane_id or info.get("agent") != "claude"
            or not missing(info.get("agent_session"))
            or info.get("cwd") != cwd or info.get("foreground_cwd") != cwd
            or not os.path.isabs(cwd) or firstmate.claims_pane(cfg, pane_id)
            or firstmate.main_pane_id(cfg) != pane_id):
        raise ValueError("not the verified main Claude pane")
    result, err = tg.herdr_json("pane", "process-info", "--pane", pane_id, timeout=10)
    process_info = result.get("process_info", {})
    foreground = process_info.get("foreground_processes", [])
    if err or process_info.get("pane_id") != pane_id or len(foreground) != 1:
        raise ValueError("foreground ownership unavailable or ambiguous")
    proc = foreground[0]
    pid = proc.get("pid")
    if type(pid) is not int or pid <= 1 or proc.get("name") != "claude" or proc.get("cwd") != cwd:
        raise ValueError("foreground Claude identity mismatch")
    ticks = _process(pid, cwd)
    with open(os.path.join(SESSIONS, str(pid) + ".json"), encoding="utf-8") as f:
        native = json.load(f)
    session_id = native.get("sessionId")
    proc_start = native.get("procStart")
    started_at = native.get("startedAt")
    if (type(native.get("pid")) is not int or native["pid"] != pid
            or native.get("cwd") != cwd or native.get("kind") != "interactive"
            or native.get("entrypoint") != "cli"
            or not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id)
            or type(proc_start) not in (int, str) or str(proc_start) != ticks
            or type(started_at) is not int or started_at <= 0):
        raise ValueError("native registry identity/lifetime mismatch")
    path, file_identity = _transcript(session_id, cwd)
    if _process(pid, cwd) != ticks:
        raise ValueError("process lifetime changed")
    return Source(pane_id, cwd, pid, ticks, session_id, started_at, path, file_identity)


def resolve(cfg: dict | None, pane_id: str, info: dict) -> Source | None:
    """Require two consistent ownership observations around native discovery."""
    try:
        source = _snapshot(cfg or {}, pane_id, info)
        return source if current(cfg, source) else None
    except (OSError, ValueError, TypeError, IndexError, KeyError, AttributeError):
        return None


def current(cfg: dict | None, source: Source) -> bool:
    """Recheck live pane, process lifetime, registry generation and exact file."""
    try:
        result, err = tg.herdr_json("agent", "get", source.pane_id, timeout=10)
        return not err and _snapshot(cfg or {}, source.pane_id, result.get("agent", {})) == source
    except (OSError, ValueError, TypeError, IndexError, KeyError, AttributeError):
        return False
