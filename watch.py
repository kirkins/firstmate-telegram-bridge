#!/usr/bin/env python3
"""Poll-based status watcher: notifications that don't depend on event delivery.

herdr's pane.agent_status_changed events are the fast path — when they arrive.
On this pod's herdr 0.9.1 they essentially never do: panes visibly transition
working/idle all day while the event hook sees nothing (exactly one 'unknown'
event since install), so the phone stays silent no matter what statuses or
focus would say. The watcher closes that gap from inside the reply poller
(bot.py starts it as a thread under the same flock/self-heal singleton): every
[watch] poll_seconds it runs `herdr agent list` (cheap JSON), diffs each pane's
agent_status against last_status.json — the same swap mechanism the event hook
keeps — and hands transitions to notify.deliver, the one shared send path
(classification, settle re-check, quiet debounce, firstmate posture, reply-map
target). Whichever side notices a completion, the push looks identical.

The diff rules are deliberately focus-independent. herdr reports "done" only
for unfocused panes — attention semantics — so the watcher treats working->idle
as done for EVERY pane (the CLI left open on the laptop counts too; the whole
point is being away with just the phone), stays silent on idle->working
(starting something is not news), and lets any pane entering blocked or done
push via the shared classifier. The event hook stays wired alongside as the
fast path; the pane:status quiet debounce keeps a transition both catch from
being sent twice.

Nothing here drops silently. Every status change the poller observes is one
bridge.log line (pane, prev->new, and state_change_seq when herdr reports it),
and every refusal to push — priming, not-news, a held message, an unsettle
flap, a debounce, a filtered status, a send failure — logs its reason, so "the
pane finished but the phone never rang" is always answerable from the log.

Sampling statuses is still not the whole truth: a turn that starts and ends
between two polls leaves the sampled status untouched (idle->idle), and a
missing herdr event leaves the sampled diff with nothing to say. So a second,
content-anchored pass runs on the same poll for every pane whose transcript
resolves (same per-CLI resolution the notification snippet uses, pi's
cwd-keyed fallback included): it tracks the identity of the pane's LAST
assistant message — the CLI's message id, else a hash of the text, plus its
timestamp — and pushes when a NEW final message exists that the sent-marker
has never recorded AND the pane is not actively working at observation time.
This catches completions shorter than the poll interval and completions herdr
never reported at all, and dedupes strictly by message identity: the shared
sent-marker (content_sent.json, checked and stamped through notify.deliver) is
recorded by every path, so the event hook, the status diff and the content
pass together deliver one push per finished message, never two. While the pane reads working the
new message is
held (logged, not recorded as seen) so it pushes when the pane goes idle/done;
Firstmate Codex instead delivers paired native public message completions during
work (completed_codex_pass); partial and private records stay held/excluded.
Blocked and unknown statuses stay the status diff's business. [watch]
content_delivery = false turns the content pass off (the status diff alone,
exactly the previous behaviour).

That single-slot tracking is still not the whole truth either, and the
2026-10-10 operator-confirmed miss proved it: firstmate's main-pane turns carry
SEVERAL operator-facing text messages (narration between tool calls), and a
pipeline anchored on "the last message" can only ever deliver the last one —
every intermediate that a newer message superseded was held, replaced and
silently lost, whether or not a status transition ever fired (the miniflux
message, held 2026-10-09 20:25 -05, never reached the phone). So under
firstmate mode the MAIN pane (non-codex CLIs) runs midrun_pass instead: text-
block-driven detection over the same session jsonl — every sealed assistant
text block is a operator-facing message, complete the moment its parseable
record lands, and thinking/tool-call records never qualify. Every message
after the delivery boundary (content_last, else the sent-marker's last
delivered) relays exactly once, in transcript order: interior messages push
immediately even while the pane works (origin="midrun", so TTS can keep them
text-only via [tts] speak_final_only); the transcript TAIL keeps the
hold-while-working rule — it may still be superseded, and it delivers as the
final reply when the pane reads idle/done. The pass runs BEFORE the status
diff in every poll, and a done/idle status push defers behind an undelivered
message (the completed_codex_pass ordering rule, generalized) so the final
never jumps ahead of the intermediates it follows. Calm's narration rule
applies to intermediates exactly as it does mid-turn in the live transcript.

Two liveness invariants back this up (the 2026-09-25 incident: a frozen watch
thread plus restart priming permanently swallowed a finished turn). Every
subprocess the watcher runs is group-killed at a hard timeout — a herdr helper
that inherits the output pipe outlives the CLI and turns subprocess.run's own
timeout into a permanent hang (tg.run_bounded) — and each poll cycle is timed
against 2x poll_seconds, logging "watch: slow cycle" when it overruns. And a
restart never primes away a finished message: the baseline the first pass
records is bounded by the sent-marker (notify.last_delivered), so a final
message newer than the last delivered one is delivered, not recorded as known
(already-delivered identities are never re-sent).
"""

import threading
import time
from datetime import datetime

import firstmate
import notify
import codex_binding
import tg

DEFAULT_POLL_SECONDS = 20
MIN_POLL_SECONDS = 5

# Observability memo: standing conditions (a pane with no resolvable
# transcript, a new message held while its pane keeps working) must be visible
# in bridge.log without repeating every poll. Keyed "kind:pane" -> the last
# value logged; a changed value logs again.
_NOTES: dict[str, str] = {}

CONTENT_LAST = "content_last.json"   # pane -> identity of its last final message


def _note(key: str, value: str, line: str) -> None:
    """Log `line` only when this standing condition is new or has changed."""
    if _NOTES.get(key) == value:
        return
    _NOTES[key] = value
    tg.log(line)


def poll_seconds(cfg: dict) -> int:
    """[watch] poll_seconds: default 20, clamped up to 5 so a typo can't spin
    herdr; 0 or less turns the watcher off (the event hook works alone again)."""
    watch = cfg.get("watch", {})
    if not isinstance(watch, dict):
        watch = {}
    try:
        value = int(watch.get("poll_seconds", DEFAULT_POLL_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_POLL_SECONDS
    if value <= 0:
        return 0
    return max(MIN_POLL_SECONDS, value)


def content_delivery(cfg: dict) -> bool:
    """[watch] content_delivery: default true. false restores the pure status
    diff — no transcript reads, no content-anchored pushes."""
    watch = cfg.get("watch", {})
    if not isinstance(watch, dict):
        return True
    value = watch.get("content_delivery", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off")
    return bool(value)


def _content_last(pane_id: str) -> str | None:
    return tg.read_state(CONTENT_LAST, {}).get(pane_id)


def _set_content_last(pane_id: str, identity: str) -> None:
    seen = tg.read_state(CONTENT_LAST, {})
    if len(seen) > 500:  # same reset policy as the status baseline
        seen = {}
    seen[pane_id] = identity
    tg.write_state(CONTENT_LAST, seen)


def poll_once(cfg: dict, prime: bool = False) -> None:
    """One diff pass over `herdr agent list`. prime=True only records the
    baselines: on the watcher's first pass every pane is already sitting in its
    current status with its current last message, which is old news rather than
    a transition worth pushing — and a poller restart must not re-announce
    every finished agent. The content pass is the exception: a restart must
    never swallow a finished message that was never delivered, so its priming
    consults the sent-marker instead of trusting the transcript (see
    content_pass). Every subprocess here carries an explicit, group-killed
    timeout — see tg.run_bounded for why that is not optional."""
    result, err = tg.herdr_json("agent", "list", timeout=10)
    if err:
        tg.log(f"watch: agent list failed: {err}")
        return
    content = content_delivery(cfg)
    fm = firstmate.enabled(cfg)
    main = firstmate.main_pane_id(cfg) if content and fm else None
    pending_completions = set()
    if content and fm:
        # Deliver sealed main-pane messages in transcript order BEFORE a status
        # transition can push the last answer ahead of its mid-run updates
        # (codex via native paired records, other CLIs via the session jsonl's
        # sealed text blocks), and defer the status push behind anything that
        # did not go out.
        for pane in result.get("agents", []):
            pane_id = pane.get("pane_id", "")
            if pane_id != main:
                continue
            if pane.get("agent") == "codex":
                if content_pass(cfg, pane, prime, main):
                    pending_completions.add(pane_id)
            elif midrun_pass(cfg, pane, prime):
                pending_completions.add(pane_id)
    for pane in result.get("agents", []):
        pane_id = pane.get("pane_id", "")
        if not pane_id:
            continue
        status = pane.get("agent_status", "unknown")
        seq = pane.get("state_change_seq")
        prev = notify.swap_last_status(pane_id, status)
        # focused panes are deliberately not filtered anywhere here: the operator
        # wants to know what finished on the laptop he walked away from too
        if prev == status:
            continue  # steady state: this pass observed no change for the pane
        seq_note = f" seq={seq}" if seq is not None else ""
        tg.log(f"watch: pane={pane_id} {prev or 'unseen'}->{status}{seq_note}")
        if prime:
            tg.log(f"watch: skip pane={pane_id}: priming baseline (no re-announce on restart)")
            continue
        if status == "working":
            tg.log(f"watch: skip pane={pane_id}: ->working is not news")
            continue
        if (pane_id in pending_completions
                and (status == "done" or (status == "idle" and prev == "working"))):
            tg.log(f"watch: pane={pane_id} completion pending; status delivery deferred for ordered retry")
            continue
        notify.deliver(cfg, pane_id, status, prev,
                       agent=pane.get("display_agent") or pane.get("agent") or "agent",
                       cli=pane.get("agent") or "",
                       title=pane.get("terminal_title_stripped")
                       or pane.get("terminal_title") or "")
    if content:
        # one main-pane resolution per cycle for the content pass's allowlist
        # check (relay_denied re-resolves per send on the status path, where a
        # deliver attempt is rare; the content pass runs for every pane on
        # every poll). Under firstmate mode the main pane is excluded here:
        # midrun_pass/completed_codex_pass already owned it ahead of the status
        # diff, in transcript order.
        for pane in result.get("agents", []):
            if fm and (pane.get("agent") == "codex" or pane.get("pane_id", "") == main):
                continue
            content_pass(cfg, pane, prime, main)


def _native_message_time(identity: str):
    """Delivery boundary only; completion is proved by native records."""
    try:
        stamp = identity.rsplit("@", 1)[1]
        value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return value if value.tzinfo else None
    except (ValueError, IndexError):
        return None


def completed_codex_pass(cfg: dict, pane: dict) -> tuple[bool, bool]:
    """Return (handled, pending) for the paired native completion path."""
    status = pane.get("agent_status", "unknown")
    if status not in ("working", "idle", "done"):
        return False, False
    pane_id = pane["pane_id"]
    updates = codex_binding.completed_updates(cfg, pane_id, pane)
    if updates is None:
        return False, False  # legacy/unavailable source retains the existing status gate
    last = _content_last(pane_id)
    delivered = notify.last_delivered(pane_id)
    if not updates:
        if status == "working":
            if last is None and not delivered:
                # A partial response is never recorded as the initial baseline.
                _set_content_last(pane_id, "")
            _note(f"completehold:{pane_id}", "none",
                  f"content: pane={pane_id} no complete native Codex message pair; holding")
        return status == "working", False
    if last is None and not delivered:
        # Preserve fresh-install priming of already-completed history.
        _set_content_last(pane_id, updates[-1].identity)
        tg.log(f"content: pane={pane_id} completed Codex baseline primed")
        return True, False
    boundary = last if last is not None else delivered
    identities = [update.identity for update in updates]
    if (boundary and boundary not in identities and _native_message_time(boundary) is None
            and delivered and delivered != boundary):
        boundary = delivered  # a normal final can establish a native boundary after a legacy one
    if boundary in identities:
        updates = updates[identities.index(boundary) + 1:]
    elif boundary:
        # A bounded tail may no longer contain the delivered record. Its
        # native timestamp still defines history, without guessing completion.
        cutoff = _native_message_time(boundary)
        if cutoff is None:
            _note(f"completeboundary:{pane_id}", boundary,
                  f"content: pane={pane_id} cannot locate native Codex delivery boundary; holding")
            return True, False
        updates = [update for update in updates
                   if (stamp := _native_message_time(update.identity)) is not None and stamp > cutoff]
    for update in updates:
        if not notify.content_already_sent(pane_id, update.identity):
            if not notify.deliver(cfg, pane_id, "done", "working",
                                  agent=pane.get("display_agent") or "codex", cli="codex",
                                  origin="completed", content_id=update.identity,
                                  completed_update=update):
                _note(f"completepending:{pane_id}", update.identity,
                      f"content: pane={pane_id} completed Codex message not delivered; retrying")
                return True, True  # retain ordering and the undelivered boundary on failure
        _set_content_last(pane_id, update.identity)
    return True, False


MIDRUN_MAX_PER_POLL = 8  # boundary lost: per-poll catch-up cap, drained oldest first


def _after_boundary(messages: list[tuple[str, str]], boundary: str,
                    pane_id: str) -> list[tuple[str, str]] | None:
    """The messages that follow `boundary`, in transcript order. None when the
    boundary cannot be located at all (not in the tail window, no parseable
    timestamp): the caller then dedupes purely by the sent-marker, draining
    oldest-first at a per-poll cap."""
    identities = [m[0] for m in messages]
    if boundary in identities:
        return messages[identities.index(boundary) + 1:]
    cutoff = notify.identity_time(boundary)
    if cutoff is not None:
        kept = [m for m in messages
                if (stamp := notify.identity_time(m[0])) is None or stamp > cutoff]
        return kept
    _note(f"boundary:{pane_id}", boundary,
          f"content: pane={pane_id} cannot locate delivery boundary {boundary[:24]} in transcript; "
          f"deduping by sent-marker (oldest {MIDRUN_MAX_PER_POLL} per poll)")
    return None


def midrun_pass(cfg: dict, pane: dict, prime: bool = False) -> bool:
    """Text-block-driven relay for the firstmate main pane (non-codex CLIs):
    every sealed assistant text message after the delivery boundary relays
    exactly once, in transcript order. The single-slot content pass anchored on
    "the last message" and could only ever deliver the tail of a multi-message
    turn — the 2026-10-10 operator-confirmed miss (a held intermediate was
    replaced and silently lost; see the module docstring). Here an interior
    message is proven complete by supersession — a newer sealed record landed
    after it — and pushes immediately, even while the pane works
    (origin="midrun", intermediate=True so [tts] speak_final_only keeps it
    text-only). The transcript TAIL keeps the operator-ratified hold-while-
    working rule: it may still be superseded, and it delivers as the final
    reply on the first idle/done observation. Returns True when a sealed
    message remains undelivered (a done/idle status push defers behind it, so
    the final never jumps the queue); every skip and every hold logs its
    reason. Calm's narration rule drops short single-line intermediates the
    live transcript would hide. Blocked/unknown panes stay the status diff's
    business, boundary unmoved past an undelivered message."""
    pane_id = pane.get("pane_id", "")
    cli = pane.get("agent") or ""
    status = pane.get("agent_status", "unknown")
    title = pane.get("terminal_title_stripped") or pane.get("terminal_title") or ""
    agent_name = pane.get("display_agent") or cli or "agent"
    messages = notify.relay_messages(cli, pane.get("agent_session") or {},
                                     notify.transcript_cwd(pane, cli), pane_id,
                                     title, cfg=cfg, info=pane)
    if messages is None:
        # no list reader for this CLI's storage: the single-slot pass stays
        # (main-allowed), exactly the pre-midrun behavior for this pane
        return bool(content_pass(cfg, pane, prime, main=pane_id))
    if not messages:
        _note(f"notranscript:{pane_id}", "none",
              f"content: pane={pane_id} agent={cli or 'agent'} no transcript — status diff only")
        return False
    boundary = _content_last(pane_id)
    if boundary is None:
        boundary = notify.last_delivered(pane_id)
    if not boundary:
        if prime or not notify.last_delivered(pane_id):
            # fresh install / first read: whatever sits in the transcript
            # predates the watcher's news — record the newest as the baseline
            # (the restart rule below still delivers anything newer than the
            # sent-marker's last delivery, never priming away a finished turn)
            _set_content_last(pane_id, messages[-1][0])
            tg.log(f"content: pane={pane_id} baseline primed ({messages[-1][0][:24]})")
            return False
        boundary = notify.last_delivered(pane_id)
    after = _after_boundary(messages, boundary, pane_id)
    drain_capped = False
    if after is None:
        unsent = [m for m in messages if not notify.content_already_sent(pane_id, m[0])]
        drain_capped = len(unsent) > MIDRUN_MAX_PER_POLL
        after = unsent[:MIDRUN_MAX_PER_POLL]
    pending = [m for m in after if not notify.content_already_sent(pane_id, m[0])]
    if not pending:
        if messages[-1][0] != boundary:
            _set_content_last(pane_id, messages[-1][0])  # everything handled: advance
        return False
    if status in ("blocked", "unknown"):
        # the status diff's business; nothing primed past an undelivered message
        _note(f"unprimed:{pane_id}", pending[-1][0],
              f"content: pane={pane_id} {status}: {pending[-1][0][:24]} left for the status diff")
        return True
    tail_identity = messages[-1][0]
    for index, (identity, text) in enumerate(pending):
        is_tail = identity == tail_identity
        if status == "working" and is_tail:
            # held, not recorded as seen: the tail may still be superseded; it
            # pushes (as the final) on the first idle/done observation
            _note(f"hold:{pane_id}", identity,
                  f"content: pane={pane_id} holding new message {identity[:24]} — pane still working")
            return index > 0
        if not is_tail and firstmate.calm_excerpt(cfg, pane_id, "working", text) == "":
            # Calm would hide this mid-turn narration in the live transcript; the
            # phone never sees more than the pane shows. Handled: advance past it.
            _set_content_last(pane_id, identity)
            continue
        if status == "working":
            tg.log(f"content: pane={pane_id} new mid-run message {identity[:24]}")
            sent = notify.deliver(cfg, pane_id, "done", "working",
                                  agent=agent_name, cli=cli, title=title,
                                  origin="midrun", content_id=identity,
                                  content_text=text, intermediate=True)
        else:
            note = "new final message" if is_tail else "new mid-run message"
            tg.log(f"content: pane={pane_id} {note} {identity[:24]} (pane {status})")
            sent = notify.deliver(cfg, pane_id, "done", "idle",
                                  agent=agent_name, cli=cli, title=title,
                                  origin="content", content_id=identity,
                                  content_text=text, intermediate=not is_tail)
        if not sent:
            # deliver logged why; keep the boundary at this message and defer
            # the status push — ordering survives the retry
            _note(f"undelivered:{pane_id}", identity,
                  f"content: pane={pane_id} not delivered — will retry next poll")
            return True
        _set_content_last(pane_id, identity)
    return drain_capped


def content_pass(cfg: dict, pane: dict, prime: bool = False,
                 main: str | None = None) -> bool | None:
    """Content-anchored delivery for one pane: push when the transcript's LAST
    assistant message is one the sent-marker has never recorded AND the pane is
    not actively working at observation time. Immune to sub-poll-interval
    turns and to missing herdr events; dedupes with every other path through
    notify.deliver and the shared marker. Firstmate Codex has a separate proven
    message-completion path: completed_codex_pass also runs while working.
    A true return defers this poll's status completion behind an unsent update.

    `main` is the poll cycle's resolved firstmate main pane id, None when
    firstmate mode is off: outbound relay under firstmate mode is an allowlist
    of one (the main pane), so a non-main pane is never tracked here — logged
    once as a standing condition instead of retried every poll. The gate in
    notify.deliver (firstmate.relay_denied) stays the authority on sends."""
    pane_id = pane.get("pane_id", "")
    if not pane_id:
        return
    if main is not None and pane_id != main:
        _note(f"mainonly:{pane_id}", main,
              f"content: pane={pane_id} is not the firstmate main pane"
              + (f" (main={main})" if main else " (main pane unidentifiable; failing closed)")
              + " — outbound relay is main-only, status diff only")
        return
    cli = pane.get("agent") or ""
    status = pane.get("agent_status", "unknown")
    if firstmate.enabled(cfg) and cli == "codex":
        handled, pending = completed_codex_pass(cfg, pane)
        if handled:
            return pending
    title = pane.get("terminal_title_stripped") or pane.get("terminal_title") or ""
    identity, _text = notify.reply_identity(
        cli, pane.get("agent_session") or {},
        notify.transcript_cwd(pane, cli), pane_id, title, cfg=cfg, info=pane)
    if not identity:
        _note(f"notranscript:{pane_id}", "none",
              f"content: pane={pane_id} agent={cli or 'agent'} no transcript — status diff only")
        return
    if notify.content_already_sent(pane_id, identity):
        # some path already delivered this message (usually the status diff on
        # the same poll); keep the pane's baseline current and stay quiet
        if _content_last(pane_id) != identity:
            _set_content_last(pane_id, identity)
        return
    last = _content_last(pane_id)
    if prime or last is None:
        delivered = notify.last_delivered(pane_id)
        if delivered and identity != delivered:
            # A restart (or first read of a pane with delivery history) whose
            # transcript holds a final message newer than the last one the phone
            # got: recording it as the baseline is exactly how a completed turn
            # gets swallowed when the watcher dies mid-message. Deliver it
            # instead; a pane still working keeps the hold, and blocked/unknown
            # panes stay the status diff's business, unrecorded so a later pass
            # can still deliver. Everything already delivered stays primed and
            # quiet — the sent-marker dedupe still applies on every path.
            if status == "working":
                _note(f"hold:{pane_id}", identity,
                      f"content: pane={pane_id} holding new message {identity[:24]} — pane still working")
                return
            if status in ("idle", "done"):
                tg.log(f"content: pane={pane_id} undelivered on restart {identity[:24]} (pane {status})")
                if notify.deliver(cfg, pane_id, "done", "idle",
                                  agent=pane.get("display_agent") or cli or "agent",
                                  cli=cli, title=title, origin="content", content_id=identity):
                    _set_content_last(pane_id, identity)
                else:
                    # deliver already logged why; say "retrying" once per message
                    _note(f"undelivered:{pane_id}", identity,
                          f"content: pane={pane_id} not delivered — will retry next poll")
                return
            _note(f"unprimed:{pane_id}", identity,
                  f"content: pane={pane_id} {status}: {identity[:24]} left unprimed — status diff's business")
            return
        # no delivery history (fresh install, new pane) or the final message is
        # the delivered one: whatever is sitting in the transcript predates the
        # watcher's news, so record it as the baseline — never announce old news
        _set_content_last(pane_id, identity)
        tg.log(f"content: pane={pane_id} baseline primed ({identity[:24]})")
        return
    if identity == last:
        return  # no new message — steady state
    if status == "working":
        # held, not recorded as seen: the message pushes when the pane is
        # observed idle/done, even if that observation shows no status change
        _note(f"hold:{pane_id}", identity,
              f"content: pane={pane_id} holding new message {identity[:24]} — pane still working")
        return
    if status not in ("idle", "done"):
        return  # blocked/unknown panes are the status diff's business
    tg.log(f"content: pane={pane_id} new final message {identity[:24]} (pane {status})")
    if notify.deliver(cfg, pane_id, "done", "idle",
                      agent=pane.get("display_agent") or cli or "agent",
                      cli=cli, title=title, origin="content", content_id=identity):
        _set_content_last(pane_id, identity)
    else:
        # deliver already logged why (filter, posture, debounce, send failure);
        # say "retrying" once per message, not once per poll
        _note(f"undelivered:{pane_id}", identity,
              f"content: pane={pane_id} not delivered — will retry next poll")


def slow_cycle_note(seconds: int, elapsed: float) -> None:
    """The stall tripwire: one poll cycle must never take 2x its interval. The
    2026-09-25 freeze sat half a minute with the process alive and not one
    observation line; this makes the wedge visible the moment it happens
    instead of at the next restart. Watcher off (seconds 0) says nothing."""
    if seconds > 0 and elapsed > 2 * seconds:
        tg.log(f"watch: slow cycle {elapsed:.1f}s (> 2x poll_seconds={seconds}s) — "
               "IO overran the schedule; suspect a call without a timeout or a hung subprocess")


def watch_loop(stop: threading.Event) -> None:
    """The poller's watcher thread: baseline first, then diff every poll_seconds.
    Config is reloaded each pass, so editing config.toml takes effect without a
    restart; a failing pass backs off instead of killing the thread. Each cycle
    is timed against 2x poll_seconds — a cycle that overruns is logged loudly,
    because a silent overrun is the stall that swallowed a finished turn."""
    tg.log("watch: status watcher running")
    primed = False
    failures = 0
    while not stop.is_set():
        cfg = tg.load_config()
        seconds = poll_seconds(cfg or {})
        if cfg is None or not tg.chat_id(cfg) or seconds <= 0:
            primed = False  # nothing watched meanwhile: re-baseline on return
            stop.wait(30)
            continue
        try:
            started = time.monotonic()
            poll_once(cfg, prime=not primed)
            slow_cycle_note(seconds, time.monotonic() - started)
            primed = True
            failures = 0
            stop.wait(seconds)
        except Exception as e:
            failures += 1
            tg.log(f"watch: poll failed: {e!r}")
            stop.wait(min(300, 10 * failures))
