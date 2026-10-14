#!/usr/bin/env python3
"""herdr plugin hook: push a Telegram message on agent status changes.

Invoked by herdr with the event payload in HERDR_PLUGIN_EVENT_JSON.
Config lives in <plugin config dir>/config.toml (see config.example.toml).

Every notification is recorded against the agent it describes, so replying to it in
Telegram routes back to that pane — see bot.py, the poller this hook keeps alive.
The send path itself is deliver(): one shared function that this hook and the
poll-based watcher in watch.py both feed, so a transition pushes the same way
whichever side noticed it.
"""

import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime

import firstmate
import codex_binding
import claude_session
import tg
import tts


def agent_info(pane_id: str) -> dict:
    """Enrich via `herdr agent get`: cwd, terminal title, session ref."""
    result, err = tg.herdr_json("agent", "get", pane_id, timeout=10)
    if err:
        tg.log(f"agent get failed for {pane_id}: {err}")
        return {}
    return result.get("agent", {})


def transcript_cwd(info: dict, cli: str = "") -> str:
    """The cwd a pane's transcript resolves from. For pi, foreground_cwd wins:
    herdr reports no session ref for pi panes (0.8.2 here, 0.9.1 on the pod), so
    resolution keys on pi's cwd-keyed storage — but a worker pane's reported
    cwd is its spawn origin (the firstmate home), which would read the MAIN
    session's transcript as the worker's own, mis-attributing main's last
    message (in upstream mode even letting the content pass push it as the
    worker's completion; under firstmate mode workers never relay at all).
    foreground_cwd is the directory pi actually runs in. Other agents keep cwd
    first: their transcript refs do not key on cwd."""
    if cli == "pi":
        return info.get("foreground_cwd") or info.get("cwd") or ""
    return info.get("cwd") or info.get("foreground_cwd") or ""


PI_SESSIONS = os.path.join(os.path.expanduser("~"), ".pi", "agent", "sessions")


def pi_session_file(cwd: str) -> str:
    """pi's session storage, keyed by cwd — often the only way to find a pi
    transcript, because herdr reports no session ref at all for pi panes
    (observed on 0.9.1: the push fires as a bare header with no body). pi files
    each session under ~/.pi/agent/sessions/<cwd, path separators and colons
    turned into dashes, wrapped in ->-- (session-manager's own encoding), so
    the newest jsonl there is this pane's conversation whenever one
    conversation per directory is in play — the same newest-mtime guess the
    codex glob and the stale-ref rollover already make."""
    if not cwd:
        return ""
    body = cwd[1:] if cwd[:1] in ("/", "\\") else cwd
    encoded = "--" + re.sub(r"[/\\:]", "-", body) + "--"
    d = os.path.join(PI_SESSIONS, encoded)
    hits = glob.glob(os.path.join(d, "*.jsonl")) if os.path.isdir(d) else []
    return max(hits, key=os.path.getmtime) if hits else ""


def session_file(agent: str, session: dict, cwd: str = "") -> str:
    """Resolve the agent's transcript path from its herdr session ref."""
    kind, value = session.get("kind"), session.get("value", "")
    if kind == "path":
        if os.path.isfile(value):
            return value
        # stale ref (session rolled over): newest jsonl in the same dir
        d = os.path.dirname(value)
        hits = glob.glob(os.path.join(d, "*.jsonl")) if os.path.isdir(d) else []
        if hits:
            return max(hits, key=os.path.getmtime)
    if kind == "id" and value:
        home = os.path.expanduser("~")
        patterns = {
            "claude": f"{home}/.claude/projects/*/{value}.jsonl",
            "codex": f"{home}/.codex/sessions/*/*/*/rollout-*{value}.jsonl",
        }
        pat = patterns.get(agent)
        if pat:
            hits = glob.glob(pat)
            if hits:
                return max(hits, key=os.path.getmtime)
    # no usable ref: pi's own storage is keyed by cwd (herdr never reports one)
    return pi_session_file(cwd) if agent == "pi" else ""


TEXT_PARTS = ("text", "output_text", "input_text")  # codex writes user text as input_text


def _texts_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            c["text"] for c in content
            if isinstance(c, dict) and c.get("type") in TEXT_PARTS and c.get("text")
        )
    return ""


def _head_lines(path: str, max_bytes: int = 600_000) -> list:
    try:
        with open(path, "rb") as f:
            return f.read(max_bytes).decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _tail_lines(path: str, max_bytes: int = 2_000_000) -> list:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - max_bytes))
            return f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def message_identity(holder: dict, outer: dict, text: str) -> str:
    """Identity of one transcript message: the CLI's own message id when it wrote
    one, otherwise a hash of the text, with the timestamp appended when reported.
    Two reads of the same message must land on the same string, and two different
    messages must not collide — this is the key the sent-marker dedupes on."""
    mid = str(holder.get("id") or outer.get("id") or holder.get("uuid") or "").strip()
    ts = str(holder.get("timestamp") or outer.get("timestamp")
             or holder.get("ts") or outer.get("ts") or "").strip()
    if not mid:
        mid = "h" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{mid}@{ts}" if ts else mid


def assistant_messages_jsonl(path: str) -> list[tuple[str, str]]:
    """(identity, text) of EVERY assistant text message in a session jsonl
    (claude / pi / generic shapes), in transcript order; [] when there is none.
    One parseable line is one sealed text block — the message was finished when
    its record landed, so a operator-facing message mid-turn is exactly as
    complete as the last one. Thinking and tool-call records carry no text part
    and never qualify, and an unparseable (mid-write) trailing line is skipped —
    the same text-block rule the Codex path proves through native records."""
    out: list[tuple[str, str]] = []
    for ln in _tail_lines(path):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        # claude: type=assistant + message.role; pi: type=message + message.role;
        # codex: payload.role; generic: top-level role
        for holder in (o.get("message"), o.get("payload"), o):
            if isinstance(holder, dict) and holder.get("role") == "assistant":
                t = _texts_from_content(holder.get("content")).strip()
                if t:
                    out.append((message_identity(holder, o, t), t))
                    break
    return out


def last_assistant_jsonl(path: str) -> tuple[str, str]:
    """(identity, text) of the last assistant message in a session jsonl
    (claude / codex / pi / generic shapes); ("", "") when there is none."""
    messages = assistant_messages_jsonl(path)
    return messages[-1] if messages else ("", "")


def identity_time(identity: str):
    """The timestamp riding on a message identity ("id@ts"), as a UNIX time in
    seconds — epoch milliseconds (pi) or an ISO-8601 stamp (claude/codex) — or
    None when the identity carries nothing parseable. Ordering across a
    session rollover keys on this when the boundary message is no longer in
    the tail window."""
    stamp = identity.rsplit("@", 1)[1] if "@" in identity else ""
    stamp = stamp.strip()
    if not stamp:
        return None
    if stamp.isdigit():
        value = int(stamp)
        return value / 1000 if value > 10_000_000_000 else float(value)
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except ValueError:
        return None


def last_reply(path: str) -> str:
    """Last assistant text in a session jsonl (claude / codex / pi / generic shapes)."""
    return last_assistant_jsonl(path)[1]


GROK_SESSION = re.compile(r"(/[^\s]*/\.grok/sessions/[^/\s]+/[0-9a-fA-F][0-9a-fA-F-]{8,})/")


def grok_session_dir(pane_id: str, cwd: str) -> str:
    """Which grok session is this pane in?

    herdr reports no agent_session for grok, and grok files its sessions under a
    url-encoded *cwd* — so several conversations, and several panes, share one
    directory and the newest mtime is not necessarily this pane's. Worse, a brand new
    conversation has no chat_history.jsonl until its first reply lands, so picking by
    mtime hands back the previous conversation. The running grok process holds its own
    events.jsonl open, which identifies the session exactly.
    """
    if not pane_id:
        return ""
    info, err = tg.herdr_json("pane", "process-info", "--pane", pane_id, timeout=10)
    if err:
        tg.log(f"grok: process-info failed for {pane_id}: {err}")
        return ""
    for proc in info.get("process_info", {}).get("foreground_processes", []):
        pid = proc.get("pid")
        if not isinstance(pid, int):
            continue
        try:
            out = subprocess.run(["lsof", "-p", str(pid)], capture_output=True, timeout=10, check=False)
        except Exception as e:
            tg.log(f"grok: lsof failed: {e!r}")
            return ""
        found = GROK_SESSION.search(out.stdout.decode(errors="replace"))
        if found:
            return found.group(1)
    return ""


def last_reply_grok(cwd: str, pane_id: str = "") -> tuple[str, str]:
    """grok stores per-cwd sessions: ~/.grok/sessions/<urlencoded-cwd>/<id>/chat_history.jsonl"""
    session = grok_session_dir(pane_id, cwd)
    if not session:
        # no live process to ask (pane already gone?): fall back to the newest session
        # under this cwd, which is right whenever only one conversation is in play
        if not cwd:
            return "", ""
        base = os.path.join(os.path.expanduser("~/.grok/sessions"), urllib.parse.quote(cwd, safe=""))
        dirs = [d for d in glob.glob(os.path.join(base, "*")) if os.path.isdir(d)]
        if not dirs:
            return "", ""
        session = max(dirs, key=os.path.getmtime)
        tg.log(f"grok: no session from process, guessing {os.path.basename(session)} by mtime")
    hist = os.path.join(session, "chat_history.jsonl")
    for ln in reversed(_tail_lines(hist)):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("type") == "assistant":
            t = o.get("content")
            t = (t if isinstance(t, str) else _texts_from_content(t)).strip()
            if t:
                return message_identity(o, {}, t), t
    return "", ""


def kimi_wire(session_id: str) -> str:
    """kimi-code: session_index.jsonl maps a session id to its directory."""
    idx = os.path.expanduser("~/.kimi-code/session_index.jsonl")
    sdir = ""
    for ln in _tail_lines(idx):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("sessionId") == session_id:
            sdir = o.get("sessionDir", "")
    if not sdir:
        return ""
    wires = sorted(glob.glob(os.path.join(sdir, "agents", "*", "wire.jsonl")))
    main_wire = os.path.join(sdir, "agents", "main", "wire.jsonl")
    return main_wire if main_wire in wires else (wires[0] if wires else "")


def last_reply_kimi(session_id: str) -> tuple[str, str]:
    """kimi text lives in wire.jsonl content.part events."""
    wire = kimi_wire(session_id)
    if not wire:
        return "", ""
    for ln in reversed(_tail_lines(wire)):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        e = o.get("event", {})
        if o.get("type") == "context.append_loop_event" and e.get("type") == "content.part":
            part = e.get("part", {})
            if part.get("type") == "text" and part.get("text", "").strip():
                t = part["text"].strip()
                return message_identity(part, o, t), t
    return "", ""


def last_reply_hermes(session_id: str) -> tuple[str, str]:
    """hermes keeps transcripts in sqlite: ~/.hermes/state.db messages table.
    The row id is monotonic, so it is a rock-solid message identity."""
    import sqlite3

    db_path = os.path.expanduser("~/.hermes/state.db")
    if not os.path.isfile(db_path):
        return "", ""
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        row = db.execute(
            "select id, content from messages where session_id=? and role='assistant'"
            " and content is not null and content != '' order by id desc limit 1",
            (session_id,),
        ).fetchone()
        db.close()
        if not row:
            return "", ""
        return f"sql:{row[0]}", (row[1] or "").strip()
    except Exception as e:
        tg.log(f"hermes db read failed: {e!r}")
        return "", ""


def last_reply_opencode(session_id: str) -> tuple[str, str]:
    """opencode: sqlite ~/.local/share/opencode/opencode.db, message + part tables."""
    import sqlite3

    db_path = os.path.expanduser("~/.local/share/opencode/opencode.db")
    if not session_id or not os.path.isfile(db_path):
        return "", ""
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        msgs = db.execute(
            "select id, data from message where session_id=? order by time_created desc limit 20",
            (session_id,),
        ).fetchall()
        for mid, data in msgs:
            try:
                if json.loads(data).get("role") != "assistant":
                    continue
            except Exception:
                continue
            parts = db.execute(
                "select data from part where message_id=? order by time_created", (mid,)
            ).fetchall()
            texts = []
            for (pd,) in parts:
                try:
                    pj = json.loads(pd)
                except Exception:
                    continue
                if pj.get("type") == "text" and pj.get("text", "").strip():
                    texts.append(pj["text"].strip())
            if texts:
                db.close()
                return str(mid), "\n".join(texts)
        db.close()
    except Exception as e:
        tg.log(f"opencode db read failed: {e!r}")
    return "", ""


# Agents front-load synthetic context as "user" messages — an AGENTS.md dump, a
# <user_info> block — before anything the human said.
SYNTHETIC = ("<instructions>", "<user_info>", "<system-reminder>", "<environment", "agents.md")


OPENCODE_DB = "~/.local/share/opencode/opencode.db"


def opencode_session(cwd: str, title: str) -> str:
    """Find an opencode session when herdr reports none — which it does intermittently,
    leaving the notification with no body and no title.

    opencode's own database records each session's directory and title, and it names the
    terminal "OC | <session title>", so the pane usually matches exactly. Falling back to
    the newest session in the directory is a guess: 68 of them share one directory here.
    """
    import sqlite3

    path = os.path.expanduser(OPENCODE_DB)
    if not cwd or not os.path.isfile(path):
        return ""
    wanted = title.split("|", 1)[1].strip() if "|" in title else title.strip()
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        if wanted:
            row = db.execute("select id from session where directory=? and title=?"
                             " order by time_updated desc limit 1", (cwd, wanted)).fetchone()
            if row:
                return row[0]
        row = db.execute("select id from session where directory=?"
                         " order by time_updated desc limit 1", (cwd,)).fetchone()
        if row:
            tg.log(f"opencode: no title match for {title!r}, using newest session in {cwd}")
            return row[0]
    except Exception as e:
        tg.log(f"opencode session lookup failed: {e!r}")
    return ""


def _usable_prompt(text: str) -> str:
    """Collapse a candidate first message, or "" if it is machine-generated context."""
    text = " ".join((text or "").split())
    if not text or text[0] in "<#" or any(s in text[:200].lower() for s in SYNTHETIC):
        return ""
    return text


def first_prompt_sql(agent: str, session_id: str) -> str:
    """First human message for the agents that keep transcripts in sqlite."""
    import sqlite3

    path = os.path.expanduser("~/.hermes/state.db" if agent == "hermes"
                              else "~/.local/share/opencode/opencode.db")
    if not session_id or not os.path.isfile(path):
        return ""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        if agent == "hermes":
            rows = db.execute(
                "select content from messages where session_id=? and role='user'"
                " and content is not null and content != '' order by id asc limit 10",
                (session_id,),
            ).fetchall()
            for (content,) in rows:
                found = _usable_prompt(content)
                if found:
                    return found
        else:
            msgs = db.execute(
                "select id, data from message where session_id=? order by time_created asc limit 20",
                (session_id,),
            ).fetchall()
            for mid, data in msgs:
                try:
                    if json.loads(data).get("role") != "user":
                        continue
                except Exception:
                    continue
                texts = []
                for (part,) in db.execute(
                        "select data from part where message_id=? order by time_created", (mid,)):
                    try:
                        parsed = json.loads(part)
                    except Exception:
                        continue
                    if parsed.get("type") == "text":
                        texts.append(parsed.get("text", ""))
                found = _usable_prompt(" ".join(texts))
                if found:
                    return found
    except Exception as e:
        tg.log(f"{agent} first-prompt read failed: {e!r}")
    return ""


def first_prompt(agent: str, session: dict, cwd: str, pane_id: str = "", title: str = "") -> str:
    """The first thing the human actually typed in this session, or ""."""
    if agent in ("hermes", "opencode"):
        return first_prompt_sql(agent, resolve_session(agent, session, cwd, title))
    if agent == "grok":
        found = grok_session_dir(pane_id, cwd)
        path = os.path.join(found, "chat_history.jsonl") if found else ""
    elif agent == "kimi":
        path = kimi_wire(session.get("value", ""))
    else:
        path = session_file(agent, session, cwd)
    if not path:
        return ""
    for ln in _head_lines(path):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        text = ""
        if o.get("type") == "user" and "message" not in o:  # grok's shape
            text = _texts_from_content(o.get("content"))
        elif o.get("type") == "turn.prompt":                # kimi's shape
            text = _texts_from_content(o.get("input"))
        else:
            for holder in (o.get("message"), o.get("payload"), o):
                if isinstance(holder, dict) and holder.get("role") == "user":
                    text = _texts_from_content(holder.get("content"))
                    break
        found = _usable_prompt(text)
        if found:
            return found
    return ""


def uninformative(title: str, cwd: str, agent: str) -> bool:
    """Is herdr's terminal title just a banner? Some CLIs never set a real one: pi shows
    "π - <user>", a fresh Claude Code shows its own name, codex shows the directory."""
    t = title.strip().lower()
    if not t or t.startswith("π"):
        return True
    base = os.path.basename((cwd or "").rstrip("/")).lower()
    user = os.path.basename(os.path.expanduser("~")).lower()
    return t in (base, user, agent.lower(), "claude code", "codex", "pi", "grok")


def display_title(info: dict, agent: str, pane_id: str = "",
                  firstmate_mode: bool = False) -> str:
    """A title worth showing: herdr's, or the session's opening prompt when herdr's says
    nothing. A pane with no conversation yet legitimately has neither.

    Firstmate mode skips the opening-prompt fallback: the opener is the session's
    FIRST message and stays frozen there for the whole conversation, so every push
    would carry it — reading as a reply to the first message of the session. A
    banner title then contributes nothing rather than stale text."""
    title = (info.get("terminal_title_stripped") or info.get("terminal_title") or "").strip()
    cwd = info.get("cwd") or info.get("foreground_cwd") or ""
    if uninformative(title, cwd, agent):
        if firstmate_mode:
            return ""
        return first_prompt(agent, info.get("agent_session") or {}, cwd, pane_id, title) or title
    return title


def resolve_session(agent: str, session: dict, cwd: str, title: str) -> str:
    """The session id herdr reported, or one worked out from the agent's own storage."""
    value = (session or {}).get("value", "")
    if not value and agent == "opencode":
        value = opencode_session(cwd, title)
    return value


def claude_reply_source(agent: str, session: dict, pane_id: str, *,
                       cfg: dict | None = None, info: dict | None = None) -> claude_session.Source | None:
    """Missing Claude references alone use the read-only foreground registry."""
    if agent != "claude" or not claude_session.missing(session) or not firstmate.enabled(cfg):
        return None
    return claude_session.resolve(cfg, pane_id, info if info is not None else agent_info(pane_id))


def reply_identity(agent: str, session: dict, cwd: str,
                   pane_id: str = "", title: str = "", *,
                   cfg: dict | None = None, info: dict | None = None,
                   claude_source: claude_session.Source | None = None) -> tuple[str, str]:
    """(message identity, text) of the pane's last assistant message, via the
    same per-CLI transcript resolution the snippet uses. The identity anchors
    content-anchored delivery and the shared sent-marker; ("", "") when no
    transcript resolves — callers must treat that as "cannot dedupe", never
    as "nothing happened"."""
    session = session or {}
    if firstmate.enabled(cfg) and agent == "codex":
        return codex_binding.read_reply(cfg, pane_id, info or {}, session, cwd)
    if agent == "grok":
        return last_reply_grok(cwd, pane_id)
    if agent == "kimi":
        return last_reply_kimi(session.get("value", ""))
    if agent == "hermes":
        return last_reply_hermes(session.get("value", ""))
    if agent == "opencode":
        return last_reply_opencode(resolve_session(agent, session, cwd, title))
    if agent == "claude" and claude_session.missing(session):
        source = claude_source or claude_reply_source(agent, session, pane_id, cfg=cfg, info=info)
        if not source:
            return "", ""
        reply = last_assistant_jsonl(source.path)
        return reply if claude_session.current(cfg, source) else ("", "")
    sf = session_file(agent, session, cwd)
    return last_assistant_jsonl(sf) if sf else ("", "")


def reply_snippet(agent: str, session: dict, cwd: str, pane_id: str = "", title: str = "") -> str:
    return reply_identity(agent, session, cwd, pane_id, title)[1]


SINGLE_SLOT_AGENTS = ("grok", "kimi", "hermes", "opencode", "codex")


def relay_messages(agent: str, session: dict, cwd: str,
                   pane_id: str = "", title: str = "", *,
                   cfg: dict | None = None, info: dict | None = None,
                   claude_source: claude_session.Source | None = None) -> list[tuple[str, str]] | None:
    """Every sealed assistant text message the pane's transcript resolves, in
    transcript order — the enumerated sibling of reply_identity. None means the
    CLI has no list reader here (grok/kimi/hermes/opencode keep the single-slot
    last-message behavior; codex delivers through the native completion pass,
    never a raw rollout enumeration); [] means no eligible assistant text,
    including an unavailable or changed source. Only the session jsonl formats
    enumerate (claude / pi / generic)."""
    session = session or {}
    if agent in SINGLE_SLOT_AGENTS:
        return None
    if agent == "claude" and claude_session.missing(session):
        source = claude_source or claude_reply_source(agent, session, pane_id, cfg=cfg, info=info)
        if not source:
            return []
        messages = assistant_messages_jsonl(source.path)
        return messages if claude_session.current(cfg, source) else []
    sf = session_file(agent, session, cwd)
    return assistant_messages_jsonl(sf) if sf else []


CHUNK_CHARS = 3600  # Telegram caps a message at 4096; leave room for the header
FOLD_OVER = 280     # shorter replies read better inline than behind an expander


def fold_mode(cfg: dict) -> str:
    """How a long reply is presented: the top-level `fold` key when it says "auto" or
    "never", else the mode default — "never" under firstmate mode, "auto" upstream.

    The fold is an expandable blockquote, and Telegram renders a blockquote like a
    reply quote. In this fork's single-agent chat every push must read as a fresh
    standalone message, so firstmate mode trades folding for plain text (code keeps
    its syntax highlighting and copy buttons); an explicit top-level fold key still
    wins, and upstream behavior is untouched."""
    value = str(cfg.get("fold", "")).strip().lower()
    if value in ("auto", "never"):
        return value
    return "never" if firstmate.enabled(cfg) else "auto"


def suppress_noop_ack(cfg: dict) -> bool:
    """Top-level suppress_noop_ack: true drops the routine no-op acknowledgement
    ("Captain, shipshape.", matched by tts.is_noop_acknowledgement under the
    voice-skip semantics) from Telegram delivery entirely — no text, no voice,
    no notification — so the phone notifies only when something actually
    happened. Default false: the ack keeps arriving as its quiet text bubble
    (never spoken), and every other message delivers unchanged either way."""
    return bool(cfg.get("suppress_noop_ack", False))


def send_message(cfg: dict, text: str, plain: str | None = None, target: dict | None = None) -> bool:
    """Send one message; True when Telegram took it. `plain` enables HTML rendering
    with itself as the fallback, so a bug in the markdown renderer degrades to
    unformatted text instead of losing the notification entirely."""
    chat = tg.chat_id(cfg)
    if not chat:
        tg.log("chat_id not set; skipping send")
        return False
    params = {"chat_id": chat, "text": text, "link_preview_options": '{"is_disabled":true}'}
    if plain is not None:
        params["parse_mode"] = "HTML"
    result, err = tg.api(cfg, "sendMessage", params)
    if err and plain is not None and ("parse" in err.lower() or "entit" in err.lower()):
        tg.log(f"HTML rejected ({err[-160:]}); resending as plain text")
        params.pop("parse_mode")
        params["text"] = plain[:4000]
        result, err = tg.api(cfg, "sendMessage", params)
    if err:
        tg.log(f"send failed: {err}: {text[:120]!r}")
        return False
    tg.log(f"sent ok ({len(text)} chars)")
    if target:
        tg.remember_target((result or {}).get("message_id"), target,
                           int(cfg.get("msg_map_limit", tg.MSG_MAP_LIMIT)))
    return True


def tidy(text: str) -> str:
    """Trim trailing spaces and collapse blank-line runs, keeping the markdown shape."""
    out: list[str] = []
    for line in text.strip().split("\n"):
        line = line.rstrip()
        if not line and out and not out[-1]:
            continue
        out.append(line)
    return "\n".join(out)


def send_notification(cfg: dict, header: str, reply: str, target: dict) -> bool:
    """One notification, split across as many messages as the reply needs. Every
    chunk is mapped back to the same agent, so replying to any of them routes
    correctly. True only if every chunk went out — a partial failure must not
    count as delivered."""
    limit = int(cfg.get("snippet_chars", 0))
    if limit > 0 and len(reply) > limit:
        reply = reply[:limit].rstrip() + "…"

    if not cfg.get("markdown", True):
        flat = " ".join(reply.split())
        text = f"{header}\n{flat}"[:4000] if header and flat else (flat or header)[:4000]
        ok = send_message(cfg, text, target=target)
    elif not reply:
        ok = send_message(cfg, tg.escape_html(header), plain=header, target=target)
    else:
        chunks = tg.split_markdown(reply, CHUNK_CHARS)
        cap = max(1, int(cfg.get("max_messages", 8)))
        trimmed = max(0, len(chunks) - cap)
        chunks = chunks[:cap]
        fold = fold_mode(cfg)
        ok = True
        for i, chunk in enumerate(chunks):
            folded = fold != "never" and (len(chunk) > FOLD_OVER or "\n" in chunk.strip())
            body = tg.md_to_html(chunk, block_code=not folded)
            if folded:
                body = f"<blockquote expandable>{body}</blockquote>"
            if trimmed and i == len(chunks) - 1:
                body += f"\n<i>(+{trimmed} more part{'s' if trimmed > 1 else ''} trimmed)</i>"
            head = tg.escape_html(header) if i == 0 else f"⋯ {i + 1}/{len(chunks)}"
            plain = f"{header}\n{chunk}" if i == 0 and header else chunk
            ok = send_message(cfg, f"{head}\n{body}" if head else body,
                              plain=plain, target=target) and ok
    return ok


def ensure_bot(cfg: dict) -> None:
    """Keep the reply poller alive. The manifest's [[startup]] hook starts it with herdr;
    this is the safety net for a poller that died or a plugin enabled mid-session."""
    if cfg.get("replies", True) and tg.chat_id(cfg):
        tg.spawn_bot()


def settled(pane_id: str, status: str, cfg: dict) -> bool:
    """Has the pane really reached `status`, or is this a flap?

    herdr infers agent state by reading the terminal, and some CLIs blink through a
    state mid-turn: grok flashes `blocked` for about a second while it starts up, which
    used to fire a notification before the agent had even been asked anything. Waiting a
    moment and re-reading the pane costs a few seconds of latency and removes the whole
    class of spurious pushes.
    """
    wait = int(cfg.get("settle_ms", 4000)) / 1000
    if wait <= 0:
        return True
    time.sleep(wait)
    now = agent_info(pane_id).get("agent_status", "")
    if not now or now == status:
        return True  # unchanged, or we can't tell — don't drop a real notification
    # `done` is terminal; herdr reports the pane as idle once it has been observed
    if status == "done" and now in ("idle", "done"):
        return True
    tg.log(f"unsettled: {pane_id} was {status}, now {now} — skipping")
    return False


def swap_last_status(pane_id: str, status: str) -> str:
    """Record this pane's status and return the previous one ("" if unseen)."""
    seen = tg.read_state("last_status.json", {})
    prev = seen.get(pane_id, "")
    if len(seen) > 500:
        seen = {}
    seen[pane_id] = status
    tg.write_state("last_status.json", seen)
    return prev


def recently_sent(key: str, quiet_seconds: int) -> bool:
    """Debounce: skip if the same pane+status fired within quiet_seconds."""
    if quiet_seconds <= 0:
        return False
    now = time.time()
    # the stamp is a read-merge-write on a file two processes share: taken under
    # the file's own lock so one side's key can't be written out from under the
    # other's merge (the delivery lock serializes one identity, not the file)
    with tg.state_file_lock("last_sent.json"):
        seen = tg.read_state("last_sent.json", {})
        last = seen.get(key, 0)
        seen = {k: v for k, v in seen.items() if now - v < 3600}
        seen[key] = now
        tg.write_state("last_sent.json", seen)
    return now - last < quiet_seconds


# The shared sent-marker: every delivered notification for a pane whose transcript
# resolves stamps the identity of the assistant message it carried. The event hook,
# the status diff and the content pass all check it, so the same finished message
# reaches the phone exactly once no matter which path (or paths) noticed it.
CONTENT_SENT = "content_sent.json"   # "pane:message-identity" -> unix time sent
CONTENT_SENT_SECS = 7 * 86400        # markers live a week, then prune
CONTENT_SENT_MAX = 2000              # hard cap, oldest entries dropped first


def content_key(pane_id: str, identity: str) -> str:
    return f"{pane_id}:{identity}"


def content_already_sent(pane_id: str, identity: str) -> bool:
    return content_key(pane_id, identity) in tg.read_state(CONTENT_SENT, {})


def last_delivered(pane_id: str) -> str:
    """Identity of the pane's most recently delivered final message, from the
    sent-marker ("" when the pane has no delivery history). Watcher restart
    priming keys off this: everything up to it is old news, and a transcript
    message beyond it is one that finished without ever reaching the phone —
    which must be delivered, never primed away as known."""
    prefix = f"{pane_id}:"
    best, best_time = "", -1.0
    for key, when in tg.read_state(CONTENT_SENT, {}).items():
        if isinstance(when, (int, float)) and when > best_time and key.startswith(prefix):
            best, best_time = key[len(prefix):], when
    return best


def mark_content_sent(pane_id: str, identity: str) -> None:
    now = time.time()
    key = content_key(pane_id, identity)
    # read-merge-write under the file's own lock: two identities delivering at
    # once (each correctly serialized per identity) must not drop each other's
    # stamps out of the shared marker file
    with tg.state_file_lock(CONTENT_SENT):
        seen = {k: t for k, t in tg.read_state(CONTENT_SENT, {}).items()
                if now - t < CONTENT_SENT_SECS}
        seen[key] = now
        if len(seen) > CONTENT_SENT_MAX:
            for k in sorted(seen, key=seen.get)[:len(seen) - CONTENT_SENT_MAX]:
                del seen[k]
        tg.write_state(CONTENT_SENT, seen)


# Cross-process delivery serialization. The event hook (this file's main, a
# separate herdr plugin process) and the watcher (bot.py's status-watcher
# thread) both land in deliver() for the same pane and message, and the
# sent-marker above is a plain JSON file: check, send, stamp with nothing
# keeping the two processes apart means both can pass the check while the
# first's Telegram send is still on the wire — a window of hundreds of
# milliseconds, the whole HTTP send — and the phone hears one completion
# twice, spoken twice in voice mode. The debounce cannot close it either: its
# keys differ by origin for non-Codex panes, and its own stamp is an
# unguarded read-merge-write. One exclusive flock per pane:content identity
# closes the window: the second delivery waits for the first's stamp, then
# the re-check reads the marker and skips.
DELIVER_LOCKS = "deliver-locks"   # dir under the state dir, one 0-byte file per identity
DELIVER_LOCK_MAX = 256            # past this many files, idle ones are pruned
DELIVER_LOCK_IDLE = 3600          # ...at least this old (nothing rewrites them)

# lock path -> [re-entry depth, fd]; deliver() is single-threaded per process
# (the hook process and the watcher thread each run it alone), so a plain dict
# tracks the holds without needing a guard of its own
_held_delivery_locks: dict[str, list] = {}


def delivery_lock_path(pane_id: str, content_id: str) -> str:
    """The flock file for one delivery identity. Hashed because pane ids and
    message identities are arbitrary text, and the state dir wants a flat,
    bounded set of boring names."""
    digest = hashlib.sha256(content_key(pane_id, content_id).encode()).hexdigest()[:32]
    return os.path.join(tg.state_path(DELIVER_LOCKS), f"{digest}.lock")


def acquire_delivery_lock(pane_id: str, content_id: str) -> str | None:
    """Take the identity's delivery lock -> its path, for release_delivery_lock
    (None when there is no identity to key a lock on).

    Re-entrant on purpose: flock excludes other processes and other threads,
    but a deliver() invoked from inside another deliver()'s send boundary —
    the overlapping-completion shape the tests drive, the only nesting this
    code has — runs on this same thread and would deadlock against its own
    lock. There the debounce's pre-send stamp already marks the identity in
    flight, so the nested pass re-enters instead of blocking, exactly as it
    did before the lock existed.
    """
    path = delivery_lock_path(pane_id, content_id)
    held = _held_delivery_locks.get(path)
    if held:
        held[0] += 1
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _held_delivery_locks[path] = [1, tg.lock_ex(path)]
    return path


def release_delivery_lock(path: str | None) -> None:
    """Drop one acquire_delivery_lock hold; the outermost release frees the
    flock and prunes the lock directory back under its cap."""
    if path is None:
        return
    held = _held_delivery_locks.get(path)
    if not held:
        return
    if held[0] > 1:
        held[0] -= 1
        return
    del _held_delivery_locks[path]
    tg.unlock(held[1])
    tg.prune_lock_dir(tg.state_path(DELIVER_LOCKS), DELIVER_LOCK_MAX, DELIVER_LOCK_IDLE)


def deliver(cfg: dict, pane_id: str, status: str, prev: str,
            agent: str = "agent", cli: str = "", title: str = "",
            origin: str = "status", content_id: str = "",
            completed_update: codex_binding.CompletedUpdate | None = None,
            content_text: str = "", intermediate: bool = False,
            claude_source: claude_session.Source | None = None) -> bool:
    """The one send path for pane and sealed-message notifications, shared by the event
    hook (main below), the poll-based status diff (watch.py), the content
    pass and the mid-run relay (also watch.py): classification, the statuses/agents
    filters, the firstmate posture, the suppress_noop_ack gate, the cross-process
    delivery lock, the sent-marker dedupe, the settle re-check, the quiet debounce
    and the reply-map target all live here so the paths cannot drift apart. `prev` is the pane's previous status from last_status.json — the
    caller has already swapped it in. origin="content" skips the settle
    re-check (the pane not working at observation time is that push's flap
    guard) and trusts the caller's content_id. origin="midrun" additionally
    carries its own sealed message (content_id + content_text, an intermediate
    text block observed while the pane was working) and skips the settle
    re-check for the same reason. origin="completed" requires a
    revalidated native message-completion pair, independent of pane status.
    `intermediate` marks a mid-turn message so the TTS path can restrict
    speech to final replies ([tts] speak_final_only). Returns True iff a
    notification went out; every refusal logs its reason, because a silent
    drop is exactly how a finished turn ends up missing from the phone."""
    denied = firstmate.relay_denied(cfg, pane_id)
    if denied:
        tg.log(denied)
        return False
    completed = origin == "completed"
    midrun = origin == "midrun"
    if completed and (not firstmate.enabled(cfg) or cli != "codex"
                      or status != "done" or completed_update is None):
        tg.log(f"skip pane={pane_id}: completed update requires a firstmate Codex message")
        return False
    if midrun and (not content_id or not content_text):
        tg.log(f"skip pane={pane_id}: midrun delivery requires a sealed message")
        return False
    # herdr only reports "done" for unfocused panes (attention semantics); a
    # focused pane goes working->idle directly. Treat that as done too.
    if status == "idle" and prev == "working":
        status = "done"
        tg.log(f"working->idle on {pane_id}: treating as done (focused completion)")
    if status not in cfg.get("statuses", ["done"]):
        tg.log(f"skip pane={pane_id}: status {status} not in statuses filter")
        return False
    agents = cfg.get("agents", [])
    if agents and cli not in agents:
        tg.log(f"skip pane={pane_id}: agent {cli!r} not in agents filter")
        return False
    info = agent_info(pane_id)
    cwd = info.get("cwd") or info.get("foreground_cwd") or ""
    missing_claude = cli == "claude" and claude_session.missing(info.get("agent_session"))
    if missing_claude and claude_source is None:
        claude_source = claude_reply_source(cli, {}, pane_id, cfg=cfg, info=info)
    if claude_source is not None and (cli != "claude" or claude_source.pane_id != pane_id
                                     or not claude_session.current(cfg, claude_source)):
        tg.log(f"skip pane={pane_id}: foreground Claude source changed; retry")
        return False
    if missing_claude and (content_id or content_text) and claude_source is None:
        tg.log(f"skip pane={pane_id}: foreground Claude source unavailable; retry")
        return False
    if completed:
        if not codex_binding.current_update(cfg, pane_id, info, completed_update):
            tg.log(f"skip pane={pane_id}: completed Codex message/source changed; retry")
            return False
        identity, reply = completed_update.identity, completed_update.text
    elif midrun:
        identity, reply = content_id, content_text
    elif content_text:
        # a caller that already enumerated the transcript hands back its own
        # copy of the message: same identity it dedupes on, same text it read
        identity, reply = content_id, content_text
    elif missing_claude and claude_source is None:
        identity, reply = "", ""  # retain status-only behavior when discovery is unavailable
    else:
        identity, reply = reply_identity(cli, info.get("agent_session") or {},
                                         transcript_cwd(info, cli), pane_id,
                                         info.get("terminal_title_stripped") or "", cfg=cfg, info=info,
                                         claude_source=claude_source)
    if (firstmate.enabled(cfg) and cli == "codex"
            and codex_binding.delivery_denied(pane_id, status, identity, reply, content_id)):
        return False
    content_id = content_id or identity  # the content pass already resolved it
    # The routine no-op acknowledgement is a handled-wake confirmation, not
    # news: with suppress_noop_ack the whole push is dropped — no text, no
    # voice, no notification. The sent-marker is stamped so every other path
    # (the watcher's content and mid-run passes) treats the message as handled
    # instead of retrying it on every poll; the drop is logged so operators can
    # verify it fired. Anything else — extra text around the string, any other
    # message — is untouched here and delivers below.
    if suppress_noop_ack(cfg) and tts.is_noop_acknowledgement(reply):
        if content_id:
            mark_content_sent(pane_id, content_id)
            tg.log(f"skip pane={pane_id}: no-op acknowledgement suppressed, not sent "
                   f"(suppress_noop_ack) ({content_id[:24]})")
        else:
            tg.log(f"skip pane={pane_id}: no-op acknowledgement suppressed, not sent "
                   f"(suppress_noop_ack)")
        return False
    # The cross-process delivery lock (see DELIVER_LOCKS above): taken before
    # the marker read it guards, held through the stamp and the speech after
    # it (only a same-identity delivery ever waits on it), so exactly one
    # process runs this check-send-stamp sequence to completion; the other
    # re-reads the marker once the lock lands and skips. A failed send still
    # stamps nothing, so a delivery that waited on the lock proceeds and
    # delivers — the retry semantics ride through the lock, they are not
    # suppressed by it. A delivery with no resolvable identity has no marker
    # to guard and no lock.
    lock = acquire_delivery_lock(pane_id, content_id) if content_id else None
    try:
        if content_id and content_already_sent(pane_id, content_id):
            tg.log(f"skip pane={pane_id}: final message already pushed ({content_id[:24]})")
            return False
        # confirm before the debounce is stamped, so a flap doesn't suppress the real one
        if origin not in ("content", "completed", "midrun") and not settled(pane_id, status, cfg):
            return False  # settled() logged the flap
        if completed and not codex_binding.current_update(cfg, pane_id, agent_info(pane_id), completed_update):
            tg.log(f"skip pane={pane_id}: completed Codex message/source changed before send; retry")
            return False
        if (not completed and firstmate.enabled(cfg) and cli == "codex"
                and codex_binding.changed_during_settle(cfg, pane_id, identity, reply)):
            return False
        if claude_source is not None and not claude_session.current(cfg, claude_source):
            tg.log(f"skip pane={pane_id}: foreground Claude source changed during settle; retry")
            return False
        # Distinct sealed messages need distinct keys, while all Codex completion
        # paths must share a key before a successful send can stamp the marker.
        # Content-keyed deliveries (content/midrun/completed) dedupe on the message
        # itself: a multi-message turn must not eat its own burst.
        codex_completion = firstmate.enabled(cfg) and cli == "codex"
        debounce_key = (f"{pane_id}:completed:{content_id}"
                        if codex_completion and status == "done" and content_id
                        else (f"{pane_id}:content:{content_id}"
                              if origin in ("content", "midrun") and content_id
                              else f"{pane_id}:{status}"))
        if recently_sent(debounce_key, int(cfg.get("quiet_seconds", 5))):
            tg.log(f"debounced pane={pane_id} status={status}")
            return False

        folder = tg.short_home(cwd)
        # firstmate mode never falls back to the reported title either: it is the same
        # CLI banner display_title just declined, and the reply map keys on the sent
        # message, not the header — routing is untouched by what the header says
        fm = firstmate.enabled(cfg)
        title = display_title(info, cli, pane_id, firstmate_mode=fm) or ("" if fm else title)
        reply = tidy(reply)
        reply = firstmate.calm_excerpt(cfg, pane_id, status, reply)
        if fm and cli == "codex" and codex_binding.delivery_denied(pane_id, status, identity, reply):
            return False

        # one line, so a phone's notification preview spends its few lines on the reply
        # rather than on metadata. ✅/❓ already says done/blocked; the word is redundant.
        emoji = tg.STATUS_EMOJI.get(status, "")
        head = " · ".join(p for p in (f"{emoji} {agent}".strip(), folder or pane_id,
                                      title[:int(cfg.get("title_chars", 48))]) if p)
        target = {"pane": pane_id, "agent": agent, "folder": folder}
        if fm and not firstmate.main_header(cfg):
            # main pane under firstmate mode (relay_denied has already refused every
            # other pane): no header line at all when there is a body. There is exactly
            # one prompt-replyable pane, so "✅ pi · folder" names what the operator
            # already knows — the body (the final reply) is the whole message. A push
            # with no body left keeps the header (an empty text would not send, and a
            # silent drop is worse); [firstmate] main_header = true restores it. The
            # reply map keys on the sent message, so routing is untouched either way.
            head = "" if reply else head
        if claude_source is not None and not claude_session.current(cfg, claude_source):
            tg.log(f"skip pane={pane_id}: foreground Claude source changed before send; retry")
            return False
        sent = send_notification(cfg, head, reply, target)
        if sent:
            tg.log(f"delivered pane={pane_id} status={status} origin={origin}")
            if content_id:
                mark_content_sent(pane_id, content_id)
            if reply:
                tts.maybe_speak(cfg, reply, target, intermediate=intermediate)
        else:
            tg.log(f"send failed pane={pane_id} status={status} origin={origin} — marker not stamped")
        return sent
    finally:
        release_delivery_lock(lock)


def main() -> None:
    cfg = tg.load_config()

    if "--test" in sys.argv:
        if cfg is None:
            print(f"config.toml not found in {tg.CONFIG_DIR}", file=sys.stderr)
            sys.exit(1)
        for warn in firstmate.mode_check(cfg):
            print(warn, file=sys.stderr)
            tg.log(f"notify: {warn}")
        send_message(cfg, "telegram.bridge test message from herdr 🔔")
        ensure_bot(cfg)
        return

    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON", "")
    if not raw:
        tg.log("no HERDR_PLUGIN_EVENT_JSON; exiting")
        return
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as e:
        tg.log(f"bad event json: {e}")
        return
    # payload shape: {"event": "pane_agent_status_changed", "data": {...fields...}}
    if isinstance(event.get("data"), dict):
        event = event["data"]
    if os.environ.get("TG_NOTIFY_DEBUG") or "agent_status" not in event:
        tg.log(f"raw event: {raw[:500]}")

    status = event.get("agent_status", "unknown")
    pane_id = event.get("pane_id", "?")
    agent = event.get("display_agent") or event.get("agent") or "agent"
    title = event.get("title") or ""
    tg.log(f"event: pane={pane_id} agent={agent} status={status}")

    if cfg is None:
        return
    ensure_bot(cfg)
    deliver(cfg, pane_id, status, swap_last_status(pane_id, status),
            agent=agent, cli=event.get("agent") or "", title=title)


if __name__ == "__main__":
    main()
