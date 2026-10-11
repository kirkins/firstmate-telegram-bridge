"""Shared offline test harness.

MUST be imported before tg/bot/notify/firstmate: it points the plugin's state and
config dirs at throwaway directories (via the env vars tg.py reads at import) and
puts the repo root on sys.path. No test in this suite touches the network, a real
herdr, a real firstmate home, or a real bot token — herdr and the Telegram API are
stubbed at the tg module boundary and all fixtures are synthetic.
"""

import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

STATE_DIR = os.environ.get("FMTEST_STATE") or tempfile.mkdtemp(prefix="fmb-state-")
CONFIG_DIR = os.environ.get("FMTEST_CONFIG") or tempfile.mkdtemp(prefix="fmb-config-")
os.environ["HERDR_PLUGIN_STATE_DIR"] = STATE_DIR
os.environ["HERDR_PLUGIN_CONFIG_DIR"] = CONFIG_DIR

import tg  # noqa: E402  (after the env above is in place)

MAIN = "w3:p1"       # the firstmate main pane
WORKER = "w9:p2"     # a worker pane, claimed by a task meta
SCOUT = "wD:p2"      # a scout pane, claimed by a task meta
FOREIGN = "wF:p2"    # a pane that has nothing to do with the firstmate fleet

CHAT = 100
USER = 1

STATE_FILES = ("msg_targets.json", "current_target.json", "last_status.json",
               "last_sent.json", "bot_offset.json", "content_sent.json",
               "content_last.json", "codex_main.json", "voice_mode.json",
               "tts_warn.json")

REAL_HERDR_RUN = tg.herdr_run
REAL_HERDR_JSON = tg.herdr_json
REAL_API = tg.api
REAL_DOWNLOAD = tg.download
ROOT = _ROOT


def reset_state() -> None:
    for name in STATE_FILES:
        try:
            os.remove(os.path.join(STATE_DIR, name))
        except FileNotFoundError:
            pass


def agent(pane: str, cwd: str, cli: str = "pi", status: str = "idle",
          title: str | None = None, session: dict | None = None,
          focused: bool = False) -> dict:
    """A synthetic `herdr agent list` entry — the recorded shape, made-up values."""
    shown = title if title is not None else f"π - {os.path.basename(cwd.rstrip('/'))}"
    entry = {"agent": cli, "agent_status": status, "cwd": cwd, "focused": focused,
             "foreground_cwd": cwd, "pane_id": pane, "revision": 1, "state_change_seq": 1,
             "tab_id": pane.replace("p", "t"), "terminal_id": f"term_{pane}",
             "terminal_title": shown, "terminal_title_stripped": shown,
             "workspace_id": pane.split(":")[0]}
    if session is not None:
        entry["agent_session"] = session
    return entry


class FakeHerdr:
    """Stands in for the herdr CLI: records argv, answers from a fixture pane list."""

    def __init__(self, agents):
        self.agents = agents
        self.calls = []

    def run(self, *args, timeout=30):
        self.calls.append(args)
        return ('{"result":{}}', "")

    def json(self, *args, timeout=30):
        self.calls.append(args)
        if args[:2] == ("agent", "list"):
            return {"agents": list(self.agents)}, ""
        if args[:2] == ("agent", "get"):
            for a in self.agents:
                if a.get("pane_id") == args[2]:
                    return {"agent": a}, ""
            return {}, "agent_not_found"
        return {}, ""

    def prompts(self):
        """Every `herdr agent prompt` argv: [(pane, text), ...]."""
        return [(c[2], c[3]) for c in self.calls if c[:2] == ("agent", "prompt")]

    def panes_touched(self, sub):
        """Panes any herdr subcommand was pointed at."""
        return [c[2] for c in self.calls if len(c) > 2 and c[0] == "agent" and c[1] == sub]


class FakeApi:
    """Stands in for the Telegram API: records sendMessage params, hands out ids."""

    def __init__(self):
        self.sent = []
        self.next_id = 900
        self.fail_send = ""

    def __call__(self, cfg, method, params, timeout=20, retries=3):
        if method == "sendMessage":
            if self.fail_send:
                return None, self.fail_send
            self.next_id += 1
            self.sent.append(params)
            return {"message_id": self.next_id}, ""
        if method == "getFile":
            return {"file_path": "docs/file.txt"}, ""
        return {}, ""

    @property
    def texts(self):
        return [p.get("text", "") for p in self.sent]


def write_config(cfg: dict) -> str:
    """Write a config.toml (notify.py reads it fresh on every run)."""
    def toml(value, indent=""):
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return json.dumps(value)
        if isinstance(value, list):
            return "[" + ", ".join(toml(v) for v in value) + "]"
        return str(value)
    lines = []
    for key, value in cfg.items():
        if isinstance(value, dict):
            lines.append(f"\n[{key}]")
            lines.extend(f"{k} = {toml(v)}" for k, v in value.items())
        else:
            lines.append(f"{key} = {toml(value)}")
    path = os.path.join(CONFIG_DIR, "config.toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def make_home(base: str, calm: str | None = None, tasks=()) -> str:
    """A synthetic firstmate home: config/calm plus state/<task>.meta/.status files
    in the recorded shapes (all values made up)."""
    home = os.path.join(base, "home")
    os.makedirs(os.path.join(home, "state"), exist_ok=True)
    if calm is not None:
        os.makedirs(os.path.join(home, "config"), exist_ok=True)
        with open(os.path.join(home, "config", "calm"), "w", encoding="utf-8") as f:
            f.write(f"{calm}\n")
    for task, pane, kind, status_lines in tasks:
        with open(os.path.join(home, "state", f"{task}.meta"), "w", encoding="utf-8") as f:
            f.write(f"window=default:{pane}\n"
                    f"endpoint_task_id={task}\n"
                    f"worktree=/tmp/fmtest/{task}\n"
                    f"project=/tmp/fmtest/project\n"
                    f"harness=pi\n"
                    f"kind={kind}\n"
                    f"herdr_pane_id={pane}\n")
        with open(os.path.join(home, "state", f"{task}.status"), "w", encoding="utf-8") as f:
            f.writelines(f"{ln}\n" for ln in status_lines)
    return home


def transcript(path: str, user_line: str, assistant_lines: list,
               ids: list | None = None, tss: list | None = None) -> str:
    """A synthetic session jsonl in the generic (pi/claude) shape last_reply reads.
    ids/tss optionally attach a message id / timestamp to each assistant entry —
    the fields message_identity keys the sent-marker on."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "message", "message": {"role": "user",
                "content": [{"type": "text", "text": user_line}]}}) + "\n")
        for i, text in enumerate(assistant_lines):
            msg = {"role": "assistant", "content": [{"type": "text", "text": text}]}
            if ids and i < len(ids) and ids[i]:
                msg["id"] = ids[i]
            if tss and i < len(tss) and tss[i]:
                msg["ts"] = tss[i]
            f.write(json.dumps({"type": "message", "message": msg}) + "\n")
    return path
