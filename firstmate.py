#!/usr/bin/env python3
"""Firstmate posture: exactly one pane — the firstmate main pane — is prompt-replyable.

Everything here is inert unless config.toml sets [firstmate] enabled = true; with it
off, bot.py and notify.py behave exactly like upstream. With it on:

  * the main pane is identified by an exact cwd match against `herdr agent list`
    (CLI type can't tell main from scouts — both are pi — pane ids churn across
    restarts, and titles are CLI-controlled; the firstmate home cwd is the one
    stable discriminator), with an optional explicit pane-id pin;
  * outbound relay is an allowlist of one: under firstmate mode the operator's
    phone carries the main pane's messages only (firstmate.relay_denied is the
    identity check notify.deliver consults — exact pane-id equality with the
    resolved main pane; workers, scouts and foreign panes never relay, and no
    config key can switch that back on);
  * every other pane (workers, scouts, anything firstmate didn't identify as main)
    is read-only context: replying still delivers text to the MAIN pane — the
    phone names a worker only as context firstmate sees (via /agents sticky
    context), never as a prompt target — and if the main pane can't be
    confidently identified, replies fail closed: a clear Telegram error, never
    a silent retarget to a worker pane;
  * the transcript excerpt honors the firstmate home's Calm preference
    (config/calm) for firstmate-home panes: mid-turn narration that Calm would hide
    in the live transcript is dropped from the notification body too.

See README.md "Firstmate posture" for the operator's view of all of this.
"""

import os

import tg

CALM_PRESERVE_MIN_CHARS = 240


# --- config access -----------------------------------------------------------


def conf(cfg: dict) -> dict:
    value = cfg.get("firstmate", {}) if isinstance(cfg, dict) else {}
    return value if isinstance(value, dict) else {}


def enabled(cfg: dict) -> bool:
    # An absent section preserves generic Herdr compatibility; the public
    # example config includes [firstmate] enabled = true as the shipped default.
    fm = conf(cfg)
    return bool(fm.get("enabled", isinstance(cfg, dict) and
                       isinstance(cfg.get("firstmate"), dict)))


def main_cwd(cfg: dict) -> str:
    value = str(conf(cfg).get("main_cwd", "~/firstmate"))
    return os.path.expanduser(value) if value.startswith("~") else value


def allow_worker_esc(cfg: dict) -> bool:
    """Escape hatch: permit /esc against worker panes (main-only by default)."""
    return bool(conf(cfg).get("allow_worker_esc", False))


def forward_unknown_commands(cfg: dict) -> bool:
    """true = an unrecognized /command is submitted to the agent as ordinary text
    instead of bouncing. Defaults to firstmate mode (agent-side commands like /afk
    must work from the phone); an explicit [firstmate] forward_unknown_commands wins."""
    return bool(conf(cfg).get("forward_unknown_commands", enabled(cfg)))


def main_header(cfg: dict) -> bool:
    """true = the main pane's push keeps its header line under firstmate mode.

    Default false: in this fork's single-agent chat the main pane is the only
    pane that both pushes and takes replies, so "✅ pi · cwd" names a thing the
    operator already knows — the final reply is the whole message."""
    return bool(conf(cfg).get("main_header", False))


def replies_allowed(cfg: dict) -> bool:
    """Firstmate mode makes the top-level allowed_user_ids mandatory: without it the
    control channel (prompting, /esc) degrades to notify-only rather than letting
    "anyone in the configured chat" drive the firstmate main pane."""
    if not enabled(cfg):
        return True
    return bool(cfg.get("allowed_user_ids", []))


def route_error(cfg: dict) -> str:
    if not replies_allowed(cfg):
        return ("🔕 replies are disabled — firstmate mode requires allowed_user_ids "
                "to be set (top level of config.toml)")
    return "🤷 can't identify the firstmate main pane — check [firstmate].main_cwd"


def mode_check(cfg: dict) -> list:
    """Startup validation -> warning lines to log. Empty when everything's fine."""
    if not enabled(cfg):
        return []
    warns = []
    if not replies_allowed(cfg):
        warns.append("firstmate: allowed_user_ids empty — replies disabled (notify-only); "
                     "set allowed_user_ids at the top level of config.toml")
    if "worker_notify" in conf(cfg):
        warns.append("firstmate: [firstmate] worker_notify is no longer read — outbound "
                     "relay is main-pane only; remove the key")
    return warns


# --- firstmate home state (state/*.meta, *.status, config/calm) ---------------


def state_dir(cfg: dict) -> str:
    explicit = str(conf(cfg).get("state_dir", "")).strip()
    return explicit or os.path.join(main_cwd(cfg), "state")


def _read_kv(path: str) -> dict:
    """key=value lines -> dict (the state/*.meta shape)."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                if "=" in ln:
                    key, _, value = ln.partition("=")
                    out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def claims(cfg: dict) -> dict:
    """{pane_id: meta} for every firstmate task whose state/*.meta claims a pane.
    Every pane firstmate spawns has a meta; the main pane has none — that asymmetry
    is what disambiguates a worker that cd'd into the firstmate home."""
    out = {}
    try:
        names = os.listdir(state_dir(cfg))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".meta"):
            continue
        meta = _read_kv(os.path.join(state_dir(cfg), name))
        pane = meta.get("herdr_pane_id", "")
        if pane:
            out[pane] = meta
    return out


def claims_pane(cfg: dict, pane_id: str) -> bool:
    return pane_id in claims(cfg)


def task_context(cfg: dict, pane_id: str) -> dict:
    """What firstmate knows about a worker pane: {"task","kind","project","status"},
    each omitted when unknown (foreign panes have no meta at all). `status` is the
    state word of the last line of state/<task>.status."""
    meta = claims(cfg).get(pane_id, {})
    ctx = {}
    for key, out in (("endpoint_task_id", "task"), ("kind", "kind"), ("project", "project")):
        if meta.get(key):
            ctx[out] = meta[key]
    task = ctx.get("task", "")
    if task:
        try:
            with open(os.path.join(state_dir(cfg), f"{task}.status"), encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            if lines:
                ctx["status"] = lines[-1].split()[0]
        except OSError:
            pass
    return ctx


# --- main-pane identification ------------------------------------------------


def _target_from(agent: dict) -> dict:
    return {"pane": agent.get("pane_id", ""),
            "agent": agent.get("display_agent") or agent.get("agent") or "agent",
            "folder": tg.short_home(agent.get("cwd") or agent.get("foreground_cwd") or "")}


def main_pane(cfg: dict) -> dict | None:
    """Resolve the firstmate main pane -> {"pane","agent","folder"} or None.

    1. [firstmate].main_pane pin, revalidated live via `herdr agent get`;
    2. else exact cwd == main_cwd over `herdr agent list`. Several matches (a worker
       that cd'd into the firstmate home) resolve to the one candidate no
       state/*.meta claims; still ambiguous -> None.
    None means fail closed: callers refuse to route replies anywhere.
    """
    if not enabled(cfg):
        return None
    pin = str(conf(cfg).get("main_pane", "")).strip()
    if pin:
        result, err = tg.herdr_json("agent", "get", pin, timeout=10)
        agent = result.get("agent", {})
        if not err and agent.get("cwd"):
            return _target_from(agent)
        tg.log(f"firstmate: pinned main_pane {pin} not live ({err or 'no cwd'}); scanning by cwd")
    result, err = tg.herdr_json("agent", "list", timeout=10)
    if err:
        tg.log(f"firstmate: agent list failed: {err}")
        return None
    want = main_cwd(cfg)
    cands = [a for a in result.get("agents", [])
             if a.get("pane_id") and a.get("cwd") == want]
    if len(cands) == 1:
        return _target_from(cands[0])
    if not cands:
        tg.log(f"firstmate: no pane has cwd == {want}")
        return None
    unclaimed = [a for a in cands if not claims_pane(cfg, a["pane_id"])]
    if len(unclaimed) == 1:
        return _target_from(unclaimed[0])
    tg.log(f"firstmate: {len(cands)} panes match main_cwd={want} and none is "
           f"unambiguously the main pane — failing closed")
    return None


def main_pane_id(cfg: dict) -> str:
    found = main_pane(cfg)
    return found["pane"] if found else ""


def main_target(cfg: dict) -> dict:
    """The main pane as a routing target, or {} when it can't be identified."""
    found = main_pane(cfg)
    return found or {}


def is_main(cfg: dict, pane_id: str) -> bool:
    return bool(pane_id) and pane_id == main_pane_id(cfg)


# --- outbound relay allowlist -------------------------------------------------


def relay_denied(cfg: dict, pane_id: str) -> str:
    """"" when this pane may relay outbound to the operator's phone, else the log
    line saying why it may not.

    Under firstmate mode the allowlist holds exactly one identity: the main
    pane. The check is exact pane-id equality with the resolved main pane (the
    live-revalidated pin, else the exact-cwd scan with the meta tiebreak) — an
    identity match on the primary supervisor session, never a name/title
    substring. Workers, scouts and foreign panes never relay, and a momentary
    failure to resolve the main pane denies everything (fail closed) rather
    than guessing. With firstmate mode off, everything may relay (upstream)."""
    if not enabled(cfg):
        return ""
    main = main_pane_id(cfg)
    if pane_id == main:
        return ""
    if main:
        return (f"firstmate: outbound relay is main-pane only — not sending for "
                f"{pane_id} (main={main})")
    return (f"firstmate: main pane unidentifiable — not sending for {pane_id} "
            f"(failing closed; check [firstmate].main_cwd)")


# --- reply routing -----------------------------------------------------------


def context_line(cfg: dict, pane_id: str, agent: str = "") -> str:
    """The prefix prepended to a operator reply that named a worker pane (via
    /agents sticky context or a pre-main-only map entry), so firstmate sees who
    the message was actually for."""
    ctx = task_context(cfg, pane_id)
    who = agent or "worker"
    task = ctx.get("task") or pane_id
    return f"[{who}/{task} pane {pane_id}]"


def retarget(cfg: dict, target: dict) -> dict:
    """Under firstmate mode every promptable target becomes the main pane; any worker
    identity rides along as context + worker_pane. A target already on main (e.g.
    set by an /agents pick) keeps its context untouched, so sticky context
    survives; an already-empty target passes through. Returns {} when the main pane
    can't be identified — callers treat that as fail-closed."""
    if not enabled(cfg) or not target.get("pane"):
        return target
    main = main_target(cfg)
    if not main:
        return {}
    if target["pane"] == main["pane"]:
        return target
    return {**main, "agent": "firstmate",
            "worker_pane": target["pane"],
            "worker_agent": target.get("agent", ""),
            "context": target.get("context") or context_line(cfg, target["pane"], target.get("agent", ""))}


def resolve_target(cfg: dict, target: dict) -> dict:
    """Full firstmate-mode routing for a would-be prompt target.

    empty -> the main pane (a plain message always reaches firstmate, even before
    any notification or current target has ever been recorded);
    worker pane -> the main pane with context naming the worker;
    main pane -> unchanged (context preserved). {} = refused: either the
    allowed_user_ids degrade or an unidentifiable main pane."""
    if not enabled(cfg):
        return target
    if not replies_allowed(cfg):
        return {}
    if not target.get("pane"):
        return main_target(cfg)
    return retarget(cfg, target)


def prefixed(cfg: dict, target: dict, text: str) -> str:
    """Prepend worker routing context to submitted text: present iff the target
    carries worker context (a sticky /agents pick, or a map entry carrying one)."""
    if not enabled(cfg) or not (target and target.get("context")):
        return text
    return f"{target['context']}\n{text}"


def command_target(cfg: dict, target: dict) -> dict:
    """/read and /esc need a target too; under firstmate mode an empty one defaults
    to the main pane, so they work before any notification has ever been sent."""
    if target.get("pane") or not enabled(cfg):
        return target
    return main_target(cfg)


def read_pane(cfg: dict, target: dict) -> str:
    """Workers stay readable: /read uses the worker pane when the target carries
    one (a sticky /agents pick, or a map entry carrying one). Read-only, never
    prompts."""
    if enabled(cfg) and target.get("worker_pane"):
        return target["worker_pane"]
    return target.get("pane", "")


def esc_target(cfg: dict, target: dict) -> str:
    """Pane /esc may interrupt, or "" to refuse. Firstmate mode makes an interrupt
    main-only — interrupting a worker is firstmate's job — unless the
    allow_worker_esc hatch is open (then the worker the target names)."""
    pane = target.get("pane", "")
    if not enabled(cfg) or not pane:
        return pane
    main = main_pane_id(cfg)
    if pane == main and not target.get("worker_pane"):
        return pane
    if allow_worker_esc(cfg):
        return target.get("worker_pane") or pane
    return ""


# --- Calm excerpt rule -------------------------------------------------------


def is_firstmate_pane(cfg: dict, pane_id: str) -> bool:
    """Main pane or any pane a firstmate task meta claims (workers, scouts)."""
    return claims_pane(cfg, pane_id) or is_main(cfg, pane_id)


def calm_on(cfg: dict) -> bool:
    """The firstmate home's Calm preference (config/calm): 'on' — and the legacy
    'max' — mean on; absent, unreadable or anything else means off."""
    try:
        with open(os.path.join(main_cwd(cfg), "config", "calm"), encoding="utf-8") as f:
            return f.read().strip().lower() in ("on", "max")
    except OSError:
        return False


def calm_excerpt(cfg: dict, pane_id: str, status: str, text: str) -> str:
    """Apply Calm's preservation rule to a transcript excerpt.

    A settled single-line assistant block under CALM_PRESERVE_MIN_CHARS trimmed
    characters is narration and is dropped; a block containing a newline or at least
    that many trimmed characters is substantive and preserved; the genuine final
    reply that ends the turn — a done notification — is always preserved. Only
    firstmate-home panes are filtered, and only while firstmate mode is on; /read
    output is raw terminal by design and never passes through here."""
    if not text or not enabled(cfg) or not calm_on(cfg):
        return text
    if not is_firstmate_pane(cfg, pane_id):
        return text
    if status == "done":
        return text
    if "\n" in text or len(text.strip()) >= CALM_PRESERVE_MIN_CHARS:
        return text
    tg.log(f"firstmate: calm drops a {len(text.strip())}-char narration line from {pane_id}")
    return ""
