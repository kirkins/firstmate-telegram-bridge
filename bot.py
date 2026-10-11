#!/usr/bin/env python3
"""herdr plugin daemon: turn Telegram replies back into agent input.

Long-polls getUpdates. A reply to one of the plugin's notifications is submitted to
that notification's agent via `herdr agent prompt`; a plain message goes to the most
recently active agent. Photos and files are saved to the plugin's inbox and the agent is
handed their path. Also serves /agents, /read and /esc.

A watcher thread also polls `herdr agent list` every [watch] poll_seconds and pushes
status transitions through the same send path as the event hook — see watch.py for
why the events alone are not enough.

  bot.py --daemon   run the poll loop in the foreground
  bot.py --ensure   start the poller if it isn't already running, then exit
  bot.py --stop     stop the running poller
  bot.py --restart  stop it and start a fresh one (picks up edited code)
  bot.py --status   report whether the poller is running
  bot.py --announce <text>  send one message to the configured chat — the
                   live smoke test, through the same send plumbing
  bot.py --doctor           check local runtime and config; --telegram checks the API
"""

import json
import os
import shutil
import signal
import sys
import threading
import time

import firstmate
import notify
import stt
import tg
import tts
import watch

OFFSET = "bot_offset.json"
POLL_SECONDS = 50
MAX_BUTTONS = 12
TELEGRAM_TEXT_LIMIT = 3500  # the API caps at 4096; leave room for our own prefix
SWEEP_EVERY = 6 * 3600      # how often the inbox is checked for expired files
HEADER = "dir:"             # callback_data prefix marking a label-only row

HELP = (
    "Reply to any of my notifications and I'll send that text to the agent it came from.\n"
    "A plain message (no reply) goes to the most recently active agent.\n\n"
    "/agents — list agents and pick one\n"
    "/read [lines] — show that agent's recent terminal output\n"
    "/esc — send Escape to interrupt the agent\n"
    "/voice [on|off] — toggle spoken replies (needs a [tts] key in config.toml)\n"
    "/help — this message\n\n"
    "Send a photo or file and I'll save it, then hand the agent its path.\n"
    "Send a voice note and I'll transcribe it and pass the text on the same way "
    "(needs a [stt] API key in config.toml)."
)

COMMANDS = [
    {"command": "agents", "description": "List agents and pick one"},
    {"command": "read", "description": "Recent terminal output"},
    {"command": "esc", "description": "Interrupt the agent"},
    {"command": "voice", "description": "Toggle spoken replies"},
    {"command": "help", "description": "How replies work"},
]


def register_commands(cfg: dict) -> None:
    """Publish the command list to Telegram. Without this the commands still work when
    typed, but nothing advertises them — no menu button, no autocomplete."""
    _, err = tg.api(cfg, "setMyCommands", {"commands": json.dumps(COMMANDS)}, retries=1)
    tg.log(f"bot: setMyCommands failed: {err}" if err else "bot: published the / command menu")


def authorized(cfg: dict, chat: int, user: int) -> bool:
    """Only the configured chat may drive agents; allowed_user_ids narrows it further."""
    if not tg.chat_id(cfg) or str(chat) != tg.chat_id(cfg):
        return False
    allowed = cfg.get("allowed_user_ids", [])
    return not allowed or str(user) in {str(u) for u in allowed}


def target_from_agent(agent: dict) -> dict:
    return {
        "pane": agent.get("pane_id", ""),
        "agent": agent.get("display_agent") or agent.get("agent") or "agent",
        "folder": tg.short_home(agent.get("cwd") or agent.get("foreground_cwd") or ""),
    }


def button_label(cfg: dict, agent: dict, target: dict, with_folder: bool = True) -> str:
    """Label for an /agents button. Several agents routinely share a working directory,
    so the pane title is what tells them apart — but a title is sometimes just the folder
    name or a generic banner, so the directory earns its place too. Only its last
    segment: the buttons are read on a phone. Under a directory header it is dropped."""
    emoji = tg.STATUS_EMOJI.get(agent.get("agent_status", ""), "❔")
    where = os.path.basename(target["folder"].rstrip("/")) or target["folder"]
    title = notify.display_title(agent, agent.get("agent") or "", agent.get("pane_id") or "")
    limit = int(cfg.get("button_title_chars", 32))
    if len(title) > limit:
        title = title[:limit].rstrip() + "…"
    parts = [f"{emoji} {target['agent']}"]
    if with_folder and where:
        parts.append(where)
    if title and title != where:
        parts.append(title)
    return " · ".join(p for p in parts if p)


def header_data(folder: str) -> str:
    """callback_data for a directory header. Capped at Telegram's 64 bytes, cutting on a
    byte boundary that keeps the string decodable."""
    return (HEADER + folder).encode()[:64].decode(errors="ignore")


def agent_keyboard(cfg: dict, agents: list) -> list:
    """Rows grouped by working directory, most recently active group first.

    A keyboard has no way to render a heading between rows, so each group opens with a
    button that is only a label — tapping it just names the directory back.
    """
    groups: dict[str, list] = {}
    for a in agents:
        target = target_from_agent(a)
        groups.setdefault(target["folder"], []).append((a, target))
    order = sorted(groups.items(),
                   key=lambda kv: -max(a.get("state_change_seq", 0) for a, _ in kv[1]))
    rows = []
    for folder, members in order:
        members.sort(key=lambda m: m[0].get("state_change_seq", 0), reverse=True)
        rows.append([{"text": f"📁 {folder or '?'}", "callback_data": header_data(folder)}])
        for a, target in members:
            rows.append([{"text": button_label(cfg, a, target, with_folder=False),
                          "callback_data": target["pane"]}])
    return rows


GONE = ("not found", "agent_not_found", "pane_not_found", "invalid pane")


def report_failure(cfg: dict, chat: int, msg_id, target: dict, err: str) -> None:
    """A closed pane is the common case and deserves plain language — herdr's own
    "agent target w1:p18 not found" is accurate but says nothing about what to do."""
    if any(m in err.lower() for m in GONE):
        say(cfg, chat, f"🚪 {tg.label(target)} is gone — that pane has been closed.\n"
                       "Pick another agent with /agents.", reply_to=msg_id)
    else:
        say(cfg, chat, f"⚠️ {tg.label(target)}: {err}", reply_to=msg_id)


def agent_status(pane: str) -> str:
    info, err = tg.herdr_json("agent", "get", pane, timeout=10)
    return "" if err else info.get("agent", {}).get("agent_status", "")


def submit(cfg: dict, pane: str, text: str) -> str:
    """Send text to an agent and make sure it actually goes in. Returns an error, or "".

    `herdr agent prompt` writes the text and the submit key in one burst. An agent that
    is already working queues that fine, but an idle Claude Code folds the trailing
    Enter into its paste-detection window — the text just sits in the input box. So when
    the agent wasn't working to begin with, wait for it to pick the prompt up, and press
    Enter once more if it never does.
    """
    was_working = agent_status(pane) == "working"
    _, err = tg.herdr_run("agent", "prompt", pane, text, timeout=60)
    if err:
        return err
    if was_working or not cfg.get("submit_fallback", True):
        return ""
    deadline = time.monotonic() + int(cfg.get("submit_wait_ms", 2500)) / 1000
    while time.monotonic() < deadline:
        time.sleep(0.25)
        status = agent_status(pane)
        if not status or status == "working":
            return ""  # picked it up (or we can't tell — better than a double submit)
    tg.log(f"bot: {pane} never started working; pressing enter to submit")
    _, err = tg.herdr_run("agent", "send-keys", pane, "enter")
    return err


def say(cfg: dict, chat: int, text: str, target: dict | None = None, reply_to=None,
        markup: dict | None = None, plain: str | None = None) -> tuple[dict | None, str]:
    """Send a message, remembering which agent it is about so replies to it route back.
    `plain` enables HTML rendering with itself as the fallback if Telegram rejects it.
    Returns (result, err) so one-shot callers can report the outcome."""
    params = {"chat_id": chat, "text": text[:TELEGRAM_TEXT_LIMIT + 500],
              "link_preview_options": '{"is_disabled":true}'}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    if markup:
        params["reply_markup"] = json.dumps(markup)
    if plain is not None:
        params["parse_mode"] = "HTML"
    result, err = tg.api(cfg, "sendMessage", params)
    if err and plain is not None and ("parse" in err.lower() or "entit" in err.lower()):
        tg.log(f"bot: HTML rejected ({err[-160:]}); resending as plain text")
        params.pop("parse_mode")
        params["text"] = plain[:TELEGRAM_TEXT_LIMIT]
        result, err = tg.api(cfg, "sendMessage", params)
    if err:
        tg.log(f"bot: send failed: {err}")
        return None, err
    if target:
        tg.remember_target((result or {}).get("message_id"), target,
                           int(cfg.get("msg_map_limit", tg.MSG_MAP_LIMIT)))
    return result, err


def resolve(cfg: dict, chat: int, reply_to, msg_id) -> dict:
    """Which agent does this message address? The replied-to notification, else the
    most recently active agent (unless reply_to_latest is turned off). Under the
    firstmate posture every answer becomes the main pane: relay is main-only, so
    every push's map entry already points there, and an empty current target
    falls back to it — a plain message always reaches firstmate, even on a
    brand-new install."""
    target = tg.target_for_reply(reply_to)
    if target:
        if firstmate.enabled(cfg):
            target = firstmate.resolve_target(cfg, target)
            if not target.get("pane"):
                say(cfg, chat, firstmate.route_error(cfg), reply_to=msg_id)
        return target
    if reply_to:
        # An explicit reply names an agent. If that mapping has aged out of the map,
        # say so — silently redirecting to whoever is current would send your message
        # to the wrong agent with a confirmation that looks perfectly fine.
        tg.log(f"bot: no target recorded for message {reply_to}")
        say(cfg, chat, "🕰 that notification is too old for me to route — "
                       "reply to a newer one, or pick an agent with /agents", reply_to=msg_id)
        return {}
    if not cfg.get("reply_to_latest", True):
        say(cfg, chat, "↩️ reply to one of my notifications to choose an agent", reply_to=msg_id)
        return {}
    target = tg.current_target()
    if firstmate.enabled(cfg):
        target = firstmate.resolve_target(cfg, target)
        if not target.get("pane"):
            say(cfg, chat, firstmate.route_error(cfg), reply_to=msg_id)
    elif not target.get("pane"):
        say(cfg, chat, "🤷 no agent picked yet — use /agents", reply_to=msg_id)
    return target


def handle_command(cfg: dict, chat: int, msg_id, text: str, target: dict) -> None:
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()

    if cmd in ("/start", "/help"):
        say(cfg, chat, HELP, reply_to=msg_id)
        return

    if cmd in ("/agents", "/a"):
        result, err = tg.herdr_json("agent", "list")
        if err:
            say(cfg, chat, f"⚠️ {err}", reply_to=msg_id)
            return
        agents = [a for a in result.get("agents", []) if a.get("pane_id")]
        if not agents:
            say(cfg, chat, "🫙 no agents running", reply_to=msg_id)
            return
        agents.sort(key=lambda a: a.get("state_change_seq", 0), reverse=True)
        shown, extra = agents[:MAX_BUTTONS], len(agents) - MAX_BUTTONS
        say(cfg, chat, "🤖 pick an agent" + (f"\n(+{extra} more)" if extra > 0 else ""),
            reply_to=msg_id, markup={"inline_keyboard": agent_keyboard(cfg, shown)})
        return

    if cmd == "/voice":
        # the toggle needs no agent target — it is about the chat, not a pane
        arg = parts[1].lower() if len(parts) > 1 else ""
        say(cfg, chat, tts.voice_reply(cfg, arg), reply_to=msg_id)
        return

    if not target.get("pane"):
        say(cfg, chat, "🤷 no agent picked yet — use /agents", reply_to=msg_id)
        return

    if cmd == "/read":
        lines = parts[1] if len(parts) > 1 and parts[1].isdigit() else str(cfg.get("read_lines", 40))
        out, err = tg.herdr_run("agent", "read", firstmate.read_pane(cfg, target),
                                "--lines", lines, "--format", "text")
        if err:
            report_failure(cfg, chat, msg_id, target, err)
            return
        body = out.strip() or "(no output)"
        if len(body) > TELEGRAM_TEXT_LIMIT:
            body = "…\n" + body[-TELEGRAM_TEXT_LIMIT:]
        head = f"📄 {tg.label(target)}"
        say(cfg, chat, f"{tg.escape_html(head)}\n"
                       f'<pre><code class="language-plaintext">{tg.escape_html(body)}</code></pre>',
            plain=f"{head}\n\n{body}", target=target, reply_to=msg_id)
        return

    if cmd == "/esc":
        pane = firstmate.esc_target(cfg, target)
        if not pane:
            say(cfg, chat, "🚫 under firstmate mode only the main pane can be interrupted — "
                           "a plain message here reaches firstmate, which owns stopping its workers",
                reply_to=msg_id)
            return
        _, err = tg.herdr_run("agent", "send-keys", pane, "esc")
        if err:
            report_failure(cfg, chat, msg_id, target, err)
            return
        who = tg.label(target) if pane == target.get("pane") else pane
        say(cfg, chat, f"⎋ escape sent to {who}", target=target, reply_to=msg_id)
        return

    if firstmate.forward_unknown_commands(cfg):
        # not one of ours: the operator typed an agent-side command (/afk, /quiet…).
        # Submit it as ordinary reply text, slash intact, so the agent sees exactly
        # what was typed — same path as a plain message.
        err = submit(cfg, target["pane"], firstmate.prefixed(cfg, target, text))
        if err:
            tg.log(f"bot: prompt {target['pane']} failed: {err}")
            report_failure(cfg, chat, msg_id, target, err)
            return
        tg.set_current_target(firstmate.retarget(cfg, target))
        tg.log(f"bot: forwarded unknown command {cmd} to {target['pane']}")
        say(cfg, chat, f"⬆️ {tg.label(target)}", target=target, reply_to=msg_id)
        return

    say(cfg, chat, f"❓ unknown command {cmd} — try /help", reply_to=msg_id)


def attachment(msg: dict) -> tuple[str, str, int]:
    """(file_id, sender-supplied name, size) for whatever file this message carries."""
    if msg.get("photo"):
        best = max(msg["photo"], key=lambda p: p.get("file_size") or p.get("width", 0))
        return best.get("file_id", ""), "", best.get("file_size", 0)
    for key in ("document", "video", "animation", "audio", "voice", "video_note", "sticker"):
        obj = msg.get(key)
        if isinstance(obj, dict) and obj.get("file_id"):
            return obj["file_id"], obj.get("file_name", ""), obj.get("file_size", 0)
    return "", "", 0


def handle_attachment(cfg: dict, chat: int, msg_id, reply_to, msg: dict) -> None:
    """Save an incoming file to the inbox and hand the agent its path.

    herdr's injection channel is text only, so the file itself can't be piped into the
    terminal — the agent gets an absolute path (plus the caption) and reads it itself.
    """
    file_id, name, size = attachment(msg)
    if not cfg.get("accept_files", True):
        say(cfg, chat, "📎 attachments are off — set accept_files = true", reply_to=msg_id)
        return
    cap = float(cfg.get("max_file_mb", 20))
    if size and size > cap * 1024 * 1024:
        say(cfg, chat, f"📎 {size / 1048576:.1f} MB is over the {cap:g} MB limit "
                       "(Telegram bots can't fetch anything above 20 MB)", reply_to=msg_id)
        return

    target = resolve(cfg, chat, reply_to, msg_id)
    if not target.get("pane"):
        return

    info, err = tg.api(cfg, "getFile", {"file_id": file_id})
    remote = (info or {}).get("file_path", "")
    if err or not remote:
        say(cfg, chat, f"⚠️ couldn't locate that file: {tg.redact(err or 'no file_path', cfg)}",
            reply_to=msg_id)
        return

    saved = tg.safe_name(name or os.path.basename(remote))
    dest = os.path.join(tg.inbox_dir(), f"{time.strftime('%Y%m%d-%H%M%S')}-{msg_id}-{saved}")
    err = tg.download(cfg, remote, dest)
    if err:
        tg.log(f"bot: download failed: {err}")
        say(cfg, chat, f"⚠️ download failed: {err}", reply_to=msg_id)
        return
    tg.log(f"bot: saved {dest} ({os.path.getsize(dest)} bytes)")

    caption = (msg.get("caption") or "").strip()
    prompt = f"{caption}\n{dest}" if caption else dest
    tg.log(f"bot: prompting {target['pane']} with {saved}"
           f"{f' + caption ({len(caption)} chars)' if caption else ' (no caption)'}")
    err = submit(cfg, target["pane"], firstmate.prefixed(cfg, target, prompt))
    if err:
        report_failure(cfg, chat, msg_id, target, err)
        return
    tg.set_current_target(firstmate.retarget(cfg, target))
    say(cfg, chat, f"📎 {saved} → {tg.label(target)}", target=target, reply_to=msg_id)


def handle_voice(cfg: dict, chat: int, msg_id, reply_to, msg: dict) -> None:
    """Transcribe a voice note and submit the transcript like a typed reply.

    Voice bubbles only — music sent as an audio file stays on the attachment path,
    and so does every voice note while [stt] is absent or disabled. Otherwise the
    gateway semantics apply: the audio is still saved to the inbox,
    the transcript goes in as a quoted line, an empty transcript becomes an explicit
    sentinel rather than an empty prompt, and a transcription failure is reported to
    the operator with a why-class (the redacted detail goes to the log) instead of a
    silent drop. Routing, size gate and caption handling are the attachment path's.
    """
    if not stt.configured(cfg):
        handle_attachment(cfg, chat, msg_id, reply_to, msg)
        return
    problem = stt.config_error(cfg)
    if problem:
        tg.log(f"bot: voice note refused: {problem}")
        say(cfg, chat, f"🎙️ voice transcription is unavailable: {problem}", reply_to=msg_id)
        return
    voice = msg["voice"]
    size = voice.get("file_size") or 0
    cap = float(cfg.get("max_file_mb", 20))
    if size and size > cap * 1024 * 1024:
        say(cfg, chat, f"🎙 {size / 1048576:.1f} MB is over the {cap:g} MB limit "
                       "(Telegram bots can't fetch anything above 20 MB)", reply_to=msg_id)
        return

    target = resolve(cfg, chat, reply_to, msg_id)
    if not target.get("pane"):
        return

    info, err = tg.api(cfg, "getFile", {"file_id": voice["file_id"]})
    remote = (info or {}).get("file_path", "")
    if err or not remote:
        say(cfg, chat, f"⚠️ couldn't locate that voice note: {tg.redact(err or 'no file_path', cfg)}",
            reply_to=msg_id)
        return

    dest = os.path.join(tg.inbox_dir(), f"{time.strftime('%Y%m%d-%H%M%S')}-{msg_id}-voice.ogg")
    err = tg.download(cfg, remote, dest)
    if err:
        tg.log(f"bot: voice download failed: {err}")
        say(cfg, chat, f"⚠️ download failed: {err}", reply_to=msg_id)
        return
    tg.log(f"bot: saved {dest} ({os.path.getsize(dest)} bytes)")

    transcript, stt_err = stt.transcribe(cfg, dest)
    if stt_err:
        tg.log(f"bot: stt failed for message {msg_id} ({stt.duration_note(voice)}): {stt_err}")
        say(cfg, chat, f"🎙️ transcription failed — {stt.error_class(stt_err)}. "
                       f"The audio is kept at {dest}", reply_to=msg_id)
        return

    prompt = stt.prompt_for(transcript)
    caption = (msg.get("caption") or "").strip()
    if caption:
        prompt = f"{caption}\n{prompt}"
    err = submit(cfg, target["pane"], firstmate.prefixed(cfg, target, prompt))
    if err:
        report_failure(cfg, chat, msg_id, target, err)
        return
    tg.set_current_target(firstmate.retarget(cfg, target))
    tg.log(f"bot: transcribed message {msg_id} ({stt.duration_note(voice)}, "
           f"{len(transcript)} chars) -> {target['pane']}")
    if not transcript.strip():
        say(cfg, chat, f"🎙️ no intelligible speech in that note → {tg.label(target)}",
            target=target, reply_to=msg_id)
    elif stt.stt_config(cfg).get("echo_transcript", True):
        shown = transcript.strip()
        if len(shown) > 400:
            shown = shown[:400].rstrip() + "…"
        say(cfg, chat, f'🎙️ "{shown}" → {tg.label(target)}', target=target, reply_to=msg_id)
    else:
        say(cfg, chat, f"🎙️ voice → {tg.label(target)}", target=target, reply_to=msg_id)


def handle_message(cfg: dict, msg: dict) -> None:
    chat = msg.get("chat", {}).get("id")
    user = msg.get("from", {}).get("id")
    if not authorized(cfg, chat, user):
        tg.log(f"bot: ignoring message from chat={chat} user={user}")
        return
    text = (msg.get("text") or "").strip()
    msg_id = msg.get("message_id")
    reply_to = (msg.get("reply_to_message") or {}).get("message_id")

    # voice must be intercepted before the generic attachment match — attachment()
    # counts a voice bubble as a file, and the whole point of [stt] is that it isn't one
    if msg.get("voice", {}).get("file_id"):
        handle_voice(cfg, chat, msg_id, reply_to, msg)
        return
    if attachment(msg)[0]:
        handle_attachment(cfg, chat, msg_id, reply_to, msg)
        return
    if not text:
        return

    if text.startswith("/"):
        handle_command(cfg, chat, msg_id, text,
                       firstmate.command_target(cfg, tg.target_for_reply(reply_to) or tg.current_target()))
        return

    target = resolve(cfg, chat, reply_to, msg_id)
    if not target.get("pane"):
        return
    err = submit(cfg, target["pane"], firstmate.prefixed(cfg, target, text))
    if err:
        tg.log(f"bot: prompt {target['pane']} failed: {err}")
        report_failure(cfg, chat, msg_id, target, err)
        return
    tg.set_current_target(firstmate.retarget(cfg, target))
    tg.log(f"bot: prompted {target['pane']} ({len(text)} chars)")
    say(cfg, chat, f"➡️ sent to {tg.label(target)}", target=target, reply_to=msg_id)


def handle_callback(cfg: dict, query: dict) -> None:
    """An /agents button press: make that agent the target for plain messages."""
    message = query.get("message", {})
    chat = message.get("chat", {}).get("id")
    if not authorized(cfg, chat, query.get("from", {}).get("id")):
        tg.log(f"bot: ignoring callback from chat={chat}")
        return
    pane = query.get("data", "")
    if pane.startswith(HEADER):  # a directory heading, not an agent
        tg.api(cfg, "answerCallbackQuery",
               {"callback_query_id": query.get("id"), "text": pane[len(HEADER):] or "directory"})
        return
    result, err = tg.herdr_json("agent", "get", pane)
    agent = result.get("agent", {})
    if err or not agent:
        tg.api(cfg, "answerCallbackQuery",
               {"callback_query_id": query.get("id"), "text": err or "agent not found"})
        return
    target = target_from_agent(agent)
    if firstmate.enabled(cfg):
        target = firstmate.retarget(cfg, target)
        if not target.get("pane"):
            tg.api(cfg, "answerCallbackQuery",
                   {"callback_query_id": query.get("id"), "text": firstmate.route_error(cfg)})
            return
    tg.set_current_target(target)
    note = (f"\nnext plain message goes to firstmate with: {target['context']}"
            if firstmate.enabled(cfg) and target.get("context") else "")
    tg.api(cfg, "answerCallbackQuery", {"callback_query_id": query.get("id"), "text": f"→ {tg.label(target)}"})
    say(cfg, chat, f"🎯 {tg.label(target)}{note}", target=target)


def handle_update(cfg: dict, update: dict) -> None:
    if "callback_query" in update:
        handle_callback(cfg, update["callback_query"])
    elif "message" in update:
        handle_message(cfg, update["message"])


def run_daemon() -> None:
    lock = tg.bot_lock()
    if lock is None:
        tg.log("bot: another poller holds the lock; exiting")
        return
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()

    # Raising out of the handler is what makes a stop prompt: the loop spends most of
    # its life blocked in a 50s long poll, and PEP 475 would otherwise just resume it.
    # subprocess.run kills its child when an exception unwinds through it.
    def stop(_sig, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    tg.log(f"bot: poller started pid={os.getpid()}")
    # the status watcher rides this same singleton: one flock, one self-heal path
    watch_stop = threading.Event()
    threading.Thread(target=watch.watch_loop, args=(watch_stop,),
                     name="status-watcher", daemon=True).start()
    try:
        poll_loop()
    finally:
        watch_stop.set()
        tg.log("bot: poller stopped")


def skip_backlog(cfg: dict) -> int | None:
    """Telegram queues updates server-side for ~24h. On the very first run, jump past
    whatever is already sitting there — otherwise messages you sent the bot before this
    plugin existed (setting up chat_id, say) would land in an agent as prompts.
    Returns the starting offset, or None if the call failed and priming should retry."""
    updates, err = tg.api(cfg, "getUpdates", {"offset": -1, "timeout": 0}, timeout=20, retries=2)
    if err:
        tg.log(f"bot: could not read the backlog ({err}); retrying")
        return None
    if not updates:
        return 0
    offset = updates[-1].get("update_id", 0) + 1
    tg.log(f"bot: first run — skipping queued updates through {offset - 1}")
    return offset


def poll_loop() -> None:
    offset = tg.read_state(OFFSET, {}).get("offset", 0)
    primed = offset > 0
    announced = False
    next_sweep = 0.0
    failures = 0
    while True:
        cfg = tg.load_config()
        if cfg is None or not tg.chat_id(cfg):
            time.sleep(30)
            continue
        if not announced:
            register_commands(cfg)
            announced = True
            for warn in firstmate.mode_check(cfg):
                tg.log(f"bot: {warn}")
            for warn in stt.startup_warnings(cfg):
                tg.log(f"bot: {warn}")
            for warn in tts.startup_warnings(cfg):
                tg.log(f"bot: {warn}")
        if time.monotonic() >= next_sweep:
            gone = tg.sweep_inbox(float(cfg.get("inbox_days", 7)))
            if gone:
                tg.log(f"bot: swept {gone} expired file(s) from the inbox")
            next_sweep = time.monotonic() + SWEEP_EVERY
        if not primed:
            start = skip_backlog(cfg)
            if start is None:
                time.sleep(5)
                continue
            offset, primed = start, True
            tg.write_state(OFFSET, {"offset": offset})
        updates, err = tg.api(
            cfg, "getUpdates",
            {"offset": offset, "timeout": POLL_SECONDS, "allowed_updates": '["message","callback_query"]'},
            timeout=POLL_SECONDS + 15, retries=1,
        )
        if err:
            failures += 1
            tg.log(f"bot: getUpdates failed ({err}); backing off")
            time.sleep(min(60, 5 * failures))
            continue
        failures = 0
        for update in updates or []:
            # advance the offset before handling: a poisonous update must not be
            # replayed forever on every restart
            offset = update.get("update_id", 0) + 1
            tg.write_state(OFFSET, {"offset": offset})
            try:
                handle_update(cfg, update)
            except Exception as e:
                tg.log(f"bot: handler error on update {update.get('update_id')}: {e!r}")


def stop_poller() -> int:
    """SIGTERM the running poller and wait for it to drop the lock. Returns its pid, or 0."""
    pid = tg.bot_pid()
    if not pid or not tg.bot_running():
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        tg.log(f"bot: stop failed: {e!r}")
        return 0
    for _ in range(10):
        if not tg.bot_running():
            break
        time.sleep(1)
    return pid


def announce(text: str) -> None:
    """One outbound message through the bridge's own send plumbing — the live
    smoke test: proves config, token resolution and the Telegram path without
    typing into any pane. The text rides argv; no credential ever does."""
    cfg = tg.load_config()
    if not cfg:
        print("announce: no config")
        return
    chat = tg.chat_id(cfg)
    if not chat:
        print("announce: chat_id not set")
        return
    result, err = say(cfg, int(chat), text)
    if err:
        print(f"announce failed: {err}")
    else:
        print(f"announced to chat {chat} as message "
              f"{(result or {}).get('message_id', '?')}")


def doctor(check_telegram: bool = False, send_test: bool = False) -> int:
    """Check the runtime and resolved config without printing credentials."""
    checks = []
    checks.append((sys.version_info >= (3, 11), "Python 3.11+"))
    checks.append((shutil.which("curl") is not None, "curl available"))
    cfg = tg.load_config()
    if not cfg:
        checks.append((False, "config.toml found and parsed"))
    else:
        checks.append((True, "config.toml found and parsed"))
        checks.append((bool(str(cfg.get("bot_token", "")).strip()),
                       "bot token resolved (file or Secret Service)"))
        checks.append((bool(tg.chat_id(cfg)), "chat_id configured"))
        if firstmate.enabled(cfg):
            checks.append((bool(cfg.get("allowed_user_ids")),
                           "allowed_user_ids set for Firstmate mode"))
            checks.append((bool(firstmate.main_cwd(cfg)), "Firstmate main_cwd configured"))
        speech = cfg.get("tts") if isinstance(cfg.get("tts"), dict) else {}
        if speech.get("enabled", False) and speech.get("provider", "openai") == "cloudflare":
            checks.append((shutil.which("ffmpeg") is not None,
                           "ffmpeg available for Cloudflare TTS audio conversion"))
        if check_telegram and cfg.get("bot_token"):
            me, err = tg.api(cfg, "getMe", {}, timeout=15, retries=1)
            checks.append((not err and bool(me),
                           "Telegram getMe reachable" if not err else
                           f"Telegram getMe failed: {err}"))
        if send_test and cfg.get("bot_token") and tg.chat_id(cfg):
            sent, err = tg.api(cfg, "sendMessage", {
                "chat_id": tg.chat_id(cfg), "text": "Firstmate Telegram Bridge doctor test"
            }, timeout=15, retries=1)
            checks.append((not err and bool(sent),
                           "Telegram test message sent" if not err else
                           f"Telegram test send failed: {err}"))
    failed = False
    for ok, label in checks:
        print(f"{'OK' if ok else 'FAIL'} {label}")
        failed |= not ok
    return 1 if failed else 0


def main() -> None:
    if "--doctor" in sys.argv:
        raise SystemExit(doctor("--telegram" in sys.argv, "--send-test" in sys.argv))
    elif "--announce" in sys.argv:
        i = sys.argv.index("--announce")
        text = " ".join(sys.argv[i + 1:]).strip()
        if not text:
            print("usage: bot.py --announce <text>")
        else:
            announce(text)
    elif "--daemon" in sys.argv:
        run_daemon()
    elif "--stop" in sys.argv:
        pid = stop_poller()
        print(f"stopped poller pid={pid}" if pid else "poller not running")
    elif "--restart" in sys.argv:
        stop_poller()
        print("poller started" if tg.spawn_bot() else "poller still running — try again")
    elif "--status" in sys.argv:
        print(f"poller running pid={tg.bot_pid()}" if tg.bot_running() else "poller not running")
    else:  # --ensure, and the default for the [[startup]] hook
        print("poller started" if tg.spawn_bot() else f"poller already running pid={tg.bot_pid()}")


if __name__ == "__main__":
    main()
