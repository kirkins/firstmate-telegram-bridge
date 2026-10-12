"""Exact, bridge-owned binding for a main Codex pane lacking a Herdr reference.

Run this module from a tool shell in the primary Codex conversation to bind its
native CODEX_THREAD_ID. A managed app-server tool shell also requires the primary
owner's explicit --primary-session reference; it is not a pane descendant.
No transport, token lookup, or terminal input is involved.
The binding is revalidated on reads; other same-cwd session activity invalidates
it rather than selecting a conversation by age, cwd, or title.
"""

import glob
import json
import os
import re
import sys
import tomllib
from typing import NamedTuple

import firstmate
import tg

BINDING = "codex_main.json"
SESSIONS = os.path.join(os.path.expanduser("~"), ".codex", "sessions")
PROC = "/proc"
MANAGED_RELEASES = os.path.join(os.path.expanduser("~"), ".codex", "packages",
                               "app-server-daemon", "releases")
MISE_CODEX_INSTALLS = os.path.join(os.path.expanduser("~"), ".local", "share", "mise",
                                  "installs", "codex")
SESSION_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
MISE_CODEX_VERSION = re.compile(r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)*")


class BindingError(ValueError):
    def __init__(self, message: str, reason: str = "source_unavailable"):
        super().__init__(message)
        self.reason = reason


class Resolution(NamedTuple):
    path: str = ""
    reason: str = ""
    lifetime: tuple = ()

    @property
    def state(self) -> str:
        if self.path:
            return "ok"
        return "retrying" if self.reason == "source_unavailable" else "handoff_required"


RECOVERY = ("The current manager must register its verified session using the "
            "owner-registration workflow (codex_binding.py in its own native tool shell; "
            "managed Codex also needs --primary-session). Replies resume automatically "
            "after registration; no bridge restart is needed.")


def _process(pid: int) -> tuple[int, str]:
    """Parent PID and Linux process start ticks (protect against PID reuse)."""
    with open(os.path.join(PROC, str(pid), "stat"), encoding="utf-8") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    return int(fields[1]), fields[19]


def _foreground(pane_id: str) -> list[int]:
    result, err = tg.herdr_json("pane", "process-info", "--pane", pane_id, timeout=10)
    if err:
        raise BindingError("pane process-info unavailable")
    info = result.get("process_info", {})
    if info.get("pane_id") != pane_id:
        raise BindingError("process-info pane mismatch", "primary_replaced")
    return [p["pid"] for p in info.get("foreground_processes", [])
            if p.get("name") == "codex" and type(p.get("pid")) is int]


def _ancestor() -> tuple[int, str]:
    """Stop at the nearest Codex, so a worker cannot borrow a main ancestor."""
    seen = set()
    pid = os.getpid()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        parent, ticks = _process(pid)
        with open(os.path.join(PROC, str(pid), "comm"), encoding="utf-8") as f:
            if f.read().strip() == "codex":
                return pid, ticks
        pid = parent
    raise BindingError("run binding inside a tool shell of the primary Codex conversation")


def _managed_executable(executable: str) -> bool:
    """Accept only direct release binaries from the two supported install roots."""
    roots = ((os.path.realpath(MANAGED_RELEASES), None),
             (os.path.realpath(MISE_CODEX_INSTALLS), MISE_CODEX_VERSION))
    for root, version_pattern in roots:
        try:
            parts = os.path.relpath(executable, root).split(os.sep)
        except ValueError:
            continue
        if (len(parts) == 3 and parts[0] not in ("", ".", "..")
                and parts[1:] == ["bin", "codex"]
                and (version_pattern is None or version_pattern.fullmatch(parts[0]))):
            return True
    return False


def _managed(pid: int, ticks: str) -> None:
    """Validate an installed managed Codex executable, mode, owner and lifetime."""
    directory = os.path.join(PROC, str(pid))
    executable = os.readlink(os.path.join(directory, "exe"))
    if (not _managed_executable(executable)
            or os.path.realpath(executable) != executable
            or not os.path.isfile(executable) or not os.access(executable, os.X_OK)
            or not os.path.samestat(os.stat(os.path.join(directory, "exe")),
                                   os.stat(executable))
            or os.stat(directory).st_uid != os.getuid()):
        raise BindingError("caller is not the installed managed Codex app-server", "primary_replaced")
    with open(os.path.join(directory, "cmdline"), "rb") as f:
        args = f.read(65536).split(b"\0")
    if len(args) < 2 or args[1] != b"app-server":
        raise BindingError("managed Codex caller is not in app-server mode", "primary_replaced")
    if _process(pid)[1] != ticks:
        raise BindingError("managed Codex process lifetime changed", "process_lifetime_changed")


def _metadata(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        row = json.loads(f.readline(65536))
    if (not isinstance(row, dict) or row.get("type") != "session_meta"
            or not isinstance(row.get("payload"), dict)):
        raise BindingError("rollout has no native session metadata")
    return row["payload"]


def _exact(session_id: str, cwd: str) -> str:
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise BindingError("invalid native session id", "missing_binding")
    hits = glob.glob(os.path.join(SESSIONS, "*", "*", "*", f"rollout-*-{session_id}.jsonl"))
    if not hits:
        raise BindingError("exact rollout missing")
    if len(hits) != 1:
        raise BindingError("exact rollout ambiguous", "conversation_ambiguous")
    try:
        meta = _metadata(hits[0])
    except (OSError, ValueError) as e:
        raise BindingError(f"bound rollout unreadable: {hits[0]}: {e}") from None
    if meta.get("id") != session_id or meta.get("cwd") != cwd:
        raise BindingError("rollout session id/cwd mismatch", "conversation_ambiguous")
    return hits[0]


def _others(path: str, cwd: str) -> tuple[dict, dict]:
    """Metadata only: fingerprint competing cwd sessions, never read their text.

    A rollout whose metadata cannot be read (crash at creation, truncation,
    permissions) is neither a proven competitor nor safely ignorable: it is
    captured as an opaque stat fingerprint, so a stable one never blocks the
    binding while a new or changed one still invalidates it.
    """
    others, opaque = {}, {}
    for candidate in glob.glob(os.path.join(SESSIONS, "*", "*", "*", "rollout-*.jsonl")):
        if candidate == path:
            continue
        try:
            stat = os.stat(candidate)
        except OSError:
            opaque[candidate] = ["unreadable"]
            continue
        try:
            meta = _metadata(candidate)
        except (OSError, ValueError):
            opaque[candidate] = [stat.st_mtime_ns, stat.st_size]
            continue
        if meta.get("cwd") == cwd:
            others[candidate] = [stat.st_mtime_ns, stat.st_size]
    return others, opaque


def _changed(current: tuple[dict, dict], snapshot: tuple) -> str:
    """The rollout paths whose fingerprint moved, for the refusal diagnostics."""
    changed = []
    for kind, cur, old in (("same-cwd", current[0], snapshot[0]),
                           ("opaque", current[1], snapshot[1])):
        for candidate in sorted(set(cur) | set(old or {})):
            if cur.get(candidate) != (old or {}).get(candidate):
                changed.append(f"{kind} {candidate}")
    return "; ".join(changed[:3])


def _main(cfg: dict, pane_id: str, info: dict) -> str:
    cwd = firstmate.main_cwd(cfg)
    if (not firstmate.enabled(cfg) or info.get("agent") != "codex" or info.get("cwd") != cwd
            or (info.get("foreground_cwd") or cwd) != cwd):
        raise BindingError("pane is not the primary Codex pane", "primary_replaced")
    main = firstmate.main_pane_id(cfg)
    if pane_id != main:
        _, err = tg.herdr_json("agent", "list", timeout=10)
        if err:
            raise BindingError("primary source unavailable")
        raise BindingError("pane is not the primary Codex pane",
                           "primary_replaced" if main else "conversation_ambiguous")
    return cwd


def binding_result(cfg: dict, pane_id: str, info: dict) -> Resolution:
    """Classify a read without changing the binding or choosing a replacement."""
    lifetime = ()
    try:
        cwd = _main(cfg, pane_id, info)
        foreground = _foreground(pane_id)
        lifetime = tuple((pid, _process(pid)[1]) for pid in sorted(foreground))
        binding = tg.read_state(BINDING, {})
        if (not isinstance(binding, dict) or binding.get("version") != 1
                or binding.get("cwd") != cwd):
            raise BindingError("main session binding missing or mismatched; bind from its native tool shell",
                               "missing_binding")
        if binding.get("pane_id") != pane_id:
            raise BindingError("bound primary pane replaced", "primary_replaced")
        pid = binding.get("pid")
        if type(pid) is not int or pid not in foreground:
            raise BindingError("bound Codex process replaced; rebind the current session", "primary_replaced")
        if _process(pid)[1] != binding.get("start_ticks"):
            raise BindingError("bound Codex process lifetime changed; rebind the current session",
                               "process_lifetime_changed")
        if "managed_pid" in binding:
            managed_pid = binding["managed_pid"]
            if type(managed_pid) is not int or managed_pid <= 1:
                raise BindingError("invalid managed Codex process binding", "missing_binding")
            lifetime += ((managed_pid, _process(managed_pid)[1]),)
            _managed(managed_pid, binding.get("managed_start_ticks"))
        path = _exact(binding.get("session_id"), cwd)
        current = _others(path, cwd)
        snapshot = (binding.get("other_rollouts"), binding.get("opaque_rollouts"))
        if current != snapshot:
            raise BindingError(f"competing same-cwd rollout changed ({_changed(current, snapshot)});"
                               " rebind the current session", "conversation_ambiguous")
        return Resolution(path, lifetime=lifetime)
    except FileNotFoundError:
        return Resolution(reason="primary_replaced", lifetime=lifetime)
    except BindingError as e:
        return Resolution(reason=e.reason, lifetime=lifetime)
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        return Resolution(reason="source_unavailable", lifetime=lifetime)


def _diagnostic(cfg: dict, pane_id: str, result: Resolution) -> None:
    # Share the watcher's standing-condition memo with status/event reads too.
    import watch

    key = f"codexhealth:{tg.STATE_DIR}:{firstmate.main_cwd(cfg)}:{pane_id}"
    if result.path:
        watch._NOTES.pop(key, None)
    else:
        watch._note(key, repr((result.reason, result.lifetime)),
                    f"codex binding: pane={pane_id} {result.state} reason={result.reason}"
                    + (f"; {RECOVERY}" if result.state == "handoff_required" else "; retrying next poll"))


def resolve(cfg: dict, pane_id: str, info: dict) -> str:
    """Validated exact rollout, or empty with a bounded, credential-free reason."""
    result = binding_result(cfg, pane_id, info)
    _diagnostic(cfg, pane_id, result)
    return result.path


def source_result(cfg: dict, pane_id: str, info: dict, session: dict, cwd: str) -> Resolution:
    """An explicit exact reference takes priority, including when unavailable."""
    if not session.get("value"):
        return binding_result(cfg, pane_id, info)
    lifetime = ()
    try:
        lifetime = tuple((pid, _process(pid)[1]) for pid in sorted(_foreground(pane_id)))
    except (BindingError, OSError, ValueError, IndexError, KeyError, TypeError):
        pass  # An explicit reference retains its existing process-independent priority.
    try:
        if session.get("kind") == "path":
            path = session["value"] if os.path.isfile(session["value"]) else ""
        elif session.get("kind") == "id":
            path = _exact(session["value"], cwd)
        else:
            path = ""
        if path:
            with open(path, "rb") as stream:
                stream.read(1)
        return Resolution(path, "" if path else "source_unavailable", lifetime)
    except BindingError as e:
        reason = "conversation_ambiguous" if e.reason == "missing_binding" else e.reason
        return Resolution(reason=reason, lifetime=lifetime)
    except (OSError, ValueError, TypeError):
        return Resolution(reason="source_unavailable", lifetime=lifetime)


def session_path(cfg: dict, pane_id: str, info: dict, session: dict, cwd: str) -> str:
    """Explicit refs win; an omitted ref uses the validated binding."""
    result = source_result(cfg, pane_id, info, session, cwd)
    _diagnostic(cfg, pane_id, result)
    return result.path


def read_reply(cfg: dict, pane_id: str, info: dict, session: dict, cwd: str) -> tuple[str, str]:
    """Use the existing reader; explicit refs win, stale paths never select siblings."""
    import notify

    path = session_path(cfg, pane_id, info, session, cwd)
    return notify.last_assistant_jsonl(path) if path else ("", "")


class CompletedUpdate(NamedTuple):
    identity: str
    text: str
    session_id: str
    path: str


def _public_message(item: dict) -> bool:
    return (item.get("phase") in (None, "commentary", "partial_answer", "final_answer")
            and item.get("recipient") in (None, "all")
            and item.get("channel") in (None, "commentary", "partial_answer", "final", "final_answer")
            and item.get("visibility") in (None, "public")
            and item.get("status") in (None, "completed"))


def _complete_text(content, kind: str) -> str:
    # Refuse mixed/private/unsupported content rather than relaying a fragment.
    if (not isinstance(content, list) or not content
            or any(not isinstance(part, dict) or part.get("type") != kind
                   or not isinstance(part.get("text"), str) for part in content)):
        return ""
    return "\n".join(part["text"] for part in content).strip()


def completed_updates(cfg: dict, pane_id: str, info: dict) -> list[CompletedUpdate] | None:
    """Public native messages with an explicit matching item-completed record.

    The response-item timestamp preserves the identity used by existing status
    deliveries. Started items, deltas, reasoning and private agent_message
    response items cannot qualify. None means the source is unavailable/legacy;
    an empty list means a validated native source has no complete pairs yet.
    """
    import notify
    import watch

    note = f"codexread:{tg.STATE_DIR}:{firstmate.main_cwd(cfg)}:{pane_id}"

    try:
        cwd = _main(cfg, pane_id, info)
        reference = info.get("agent_session") or {}
        # An id is exact authority, not a glob pattern or a newest-file choice.
        path = session_path(cfg, pane_id, info, reference, cwd)
        if not path:
            return None
        meta = _metadata(path)
        sid = meta.get("id")
        if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid) or meta.get("cwd") != cwd:
            return None
        with open(path, "rb") as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - 2_000_000)
            stream.seek(start)
            data = stream.read(2_000_000)
        # Only sealed JSONL records; a split first/last line cannot be evidence.
        lines = data.split(b"\n")[1 if start else 0:-1]
        records = []
        for line in lines:
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(row, dict) and isinstance(row.get("payload"), dict):
                records.append(row)
        completed, ambiguous = {}, set()
        for row in records:
            payload = row["payload"]
            item = payload.get("item")
            if (row.get("type") != "event_msg" or payload.get("type") != "item_completed"
                    or payload.get("thread_id") != sid or not isinstance(payload.get("turn_id"), str)
                    or not payload["turn_id"] or not isinstance(item, dict)
                    or item.get("type") != "AgentMessage" or not _public_message(item)):
                continue
            mid = item.get("id")
            text = _complete_text(item.get("content"), "Text")
            if not isinstance(mid, str) or not mid or not text:
                continue
            value = (item.get("phase"), text, payload["turn_id"])
            if mid in completed and completed[mid] != value:
                ambiguous.add(mid)
            completed[mid] = value
        updates, seen = {}, {}
        for row in records:
            item = row["payload"]
            mid = item.get("id")
            if (row.get("type") != "response_item" or item.get("type") != "message"
                    or item.get("role") != "assistant" or not isinstance(mid, str)
                    or mid not in completed or mid in ambiguous or not _public_message(item)):
                continue
            text = _complete_text(item.get("content"), "output_text")
            phase, complete_text, turn = completed[mid]
            metadata = item.get("internal_chat_message_metadata_passthrough") or {}
            if (not text or (item.get("phase"), text) != (phase, complete_text)
                    or not isinstance(metadata, dict) or metadata.get("turn_id", turn) != turn
                    or not isinstance(row.get("timestamp"), str) or not row["timestamp"]):
                continue
            candidate = CompletedUpdate(notify.message_identity(item, row, text), text, sid, path)
            if mid in seen and seen[mid] != candidate:
                ambiguous.add(mid)
            seen[mid] = candidate
            updates[mid] = candidate
        watch._NOTES.pop(note, None)
        return [candidate for mid, candidate in updates.items() if mid not in ambiguous]
    except (BindingError, OSError, ValueError, IndexError, KeyError, TypeError):
        watch._note(note, "unavailable", f"codex completed update: pane={pane_id}: source unavailable; retrying")
        return None


def current_update(cfg: dict, pane_id: str, info: dict, update: CompletedUpdate) -> bool:
    return update in (completed_updates(cfg, pane_id, info) or [])


def delivery_denied(pane_id: str, status: str, identity: str, reply: str,
                    content_id: str = "") -> bool:
    if status == "done" and not reply:
        tg.log(f"skip pane={pane_id}: Codex completion has no displayable reply; awaiting transcript/binding")
        return True
    if content_id and content_id != identity:
        tg.log(f"skip pane={pane_id}: Codex content changed during delivery; retry current message")
        return True
    return False


def changed_during_settle(cfg: dict, pane_id: str, identity: str, reply: str) -> bool:
    import notify

    info = notify.agent_info(pane_id)
    fresh = read_reply(cfg, pane_id, info, info.get("agent_session") or {},
                       notify.transcript_cwd(info, "codex")) if info.get("agent") == "codex" else ("", "")
    if fresh != (identity, reply):
        tg.log(f"skip pane={pane_id}: Codex session/reply changed during settle; retry current message")
        return True
    return False


def prepare_binding(cfg: dict, pane_id: str, info: dict,
                    primary_session: str | None = None) -> dict:
    """Validate a binding candidate without changing persisted state."""
    cwd = _main(cfg, pane_id, info)
    session_id = os.environ.get("CODEX_THREAD_ID", "")
    if primary_session is not None and (not SESSION_ID.fullmatch(primary_session)
                                       or session_id != primary_session):
        raise BindingError("native caller thread does not match the primary owner's exact session reference")
    caller, caller_ticks = _ancestor()
    foreground = _foreground(pane_id)
    managed = caller not in foreground
    if managed:
        if primary_session is None:
            raise BindingError("managed tool shell requires the primary owner's --primary-session reference")
        _managed(caller, caller_ticks)
        if len(foreground) != 1:
            raise BindingError("primary foreground Codex process missing or ambiguous")
        pid = foreground[0]
    else:
        pid = caller
    ticks = _process(pid)[1]
    path = _exact(session_id, cwd)
    others, opaque = _others(path, cwd)
    # Recheck after the metadata scan; a replacement must not mint a stale binding.
    if pid not in _foreground(pane_id) or _process(pid)[1] != ticks:
        raise BindingError("primary foreground Codex process changed during binding")
    if managed:
        _managed(caller, caller_ticks)
    elif _process(caller)[1] != caller_ticks:
        raise BindingError("primary caller process changed during binding")
    binding = {"version": 1, "pane_id": pane_id, "cwd": cwd, "pid": pid,
               "start_ticks": ticks, "session_id": session_id,
               "other_rollouts": others, "opaque_rollouts": opaque}
    if managed:
        binding.update(managed_pid=caller, managed_start_ticks=caller_ticks)
    return binding


def bind(cfg: dict, pane_id: str, info: dict, primary_session: str | None = None) -> dict:
    """Persist a validated pane binding or managed caller's owner-verified candidate."""
    binding = prepare_binding(cfg, pane_id, info, primary_session)
    tg.write_state(BINDING, binding)
    return binding


def main() -> None:
    try:
        # Read configuration without resolving any bot/STT credentials.
        with open(os.path.join(tg.CONFIG_DIR, "config.toml"), "rb") as f:
            cfg = tomllib.load(f)
        pane_id = firstmate.main_pane_id(cfg)
        result, err = tg.herdr_json("agent", "get", pane_id, timeout=10)
        checking = sys.argv[1:] == ["--check"]
        if err and not checking:
            raise BindingError("primary pane unavailable")
        if checking:
            info = result.get("agent", {})
            if err or not pane_id:
                agents, unavailable = tg.herdr_json("agent", "list", timeout=10)
                candidates = [p for p in agents.get("agents", [])
                              if p.get("cwd") == firstmate.main_cwd(cfg)]
                health = Resolution(reason="conversation_ambiguous" if not unavailable
                                    and not pane_id and len(candidates) > 1 else "source_unavailable")
            else:
                health = source_result(cfg, pane_id, info, info.get("agent_session") or {},
                                       info.get("cwd", ""))
            if not health.path:
                print(f"Codex reply health: {health.state}; reason={health.reason}")
                print(RECOVERY if health.state == "handoff_required" else
                      "The bridge will retry this source automatically; no registration is needed.")
                raise SystemExit(1)
        elif not sys.argv[1:]:
            bind(cfg, pane_id, result.get("agent", {}))
        elif len(sys.argv) == 3 and sys.argv[1] == "--primary-session":
            bind(cfg, pane_id, result.get("agent", {}), sys.argv[2])
        else:
            raise BindingError("usage: codex_binding.py [--check | --primary-session UUID]")
        print(f"Codex main binding validated: pane={pane_id}")
    except (BindingError, OSError, ValueError, IndexError, KeyError, TypeError) as e:
        print(f"Codex binding refused: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
