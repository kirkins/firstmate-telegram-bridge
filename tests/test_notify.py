"""Offline notify.py tests: under firstmate mode outbound relay is an allowlist
of one — worker/sub-agent events never send, main-pane events do — while the Calm
preference shapes the main pane's excerpt through fixture transcripts, and pi
session resolution is pinned in upstream mode (where every pane still pushes).
Drives notify.main() with synthetic herdr events — nothing live, nothing networked.
"""

import json
import os
import re
import tempfile
import unittest

import _env  # noqa: F401  (env must be set before tg/notify imports)
import notify
import tg
from _env import (CHAT, MAIN, WORKER, FakeApi, FakeHerdr, agent, make_home,
                  reset_state, transcript, write_config)

TASK = ("bridge-fork-build", WORKER, "ship", ["done [at=1790000100]: PR opened"])

NARRATION = "Let me check that file."                       # short, single-line
SUBSTANTIVE = "The race was in the fixture teardown.\nAll three tests pass now."
FINAL_REPLY = "done: all green, PR opened"                  # short but final
NOOP_ACK = "Captain, shipshape."                            # the handled-wake all-clear


def event(pane, status="done", cli="pi"):
    return {"event": "pane.agent_status_changed",
            "data": {"agent_status": status, "pane_id": pane, "agent": cli,
                     "display_agent": cli, "title": ""}}


class NotifyCase(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.base = tempfile.mkdtemp(prefix="fm-notify-")
        self.api = FakeApi()
        tg.api = self.api
        self.addCleanup(setattr, tg, "api", _env.REAL_API)
        self.sessions = {}

    def agents(self, home):
        main_session = {"kind": "path", "value": self.sessions.get(MAIN, "/none.jsonl")}
        worker_session = {"kind": "path", "value": self.sessions.get(WORKER, "/none.jsonl")}
        return [agent(MAIN, home, status="done", session=main_session),
                agent(WORKER, f"{home}-treehouse/w1", cli="opencode", status="done",
                      title="OC | build the fork", session=worker_session)]

    def run_notify(self, home, ev, agents_override=None, firstmate=True, top=None,
                   **fm):
        """One notify.py main() run with herdr stubbed to the fixture fleet.
        `top` merges extra top-level config keys (suppressions, toggles)."""
        herdr = FakeHerdr(agents_override if agents_override is not None
                          else self.agents(home))
        tg.herdr_run = herdr.run
        tg.herdr_json = herdr.json
        self.addCleanup(setattr, tg, "herdr_run", _env.REAL_HERDR_RUN)
        self.addCleanup(setattr, tg, "herdr_json", _env.REAL_HERDR_JSON)
        fm_cfg = ({"enabled": True, "main_cwd": home, **fm} if firstmate
                  else {"enabled": False})
        write_config({"chat_id": str(CHAT), "bot_token": "T0K:EN", "replies": False,
                      "settle_ms": 0, "quiet_seconds": 0,
                      "statuses": ["done", "blocked"], **(top or {}),
                      "firstmate": fm_cfg})
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(ev)
        self.addCleanup(os.environ.pop, "HERDR_PLUGIN_EVENT_JSON", None)
        notify.main()

    def map_entries(self):
        return tg.read_state(tg.MSG_MAP, {})


class TestMainOnlyRelay(NotifyCase):
    """The operator's contract: under firstmate mode the phone carries the main
    pane's messages only. A worker/sub-agent event never sends and never maps,
    whatever the old worker_notify key said; a main event sends exactly once;
    an unidentifiable main pane denies everything (fail closed, not a guess)."""

    def setUp(self):
        super().setUp()
        self.home = make_home(self.base, tasks=[TASK])
        self.sessions[WORKER] = transcript(os.path.join(self.base, "w.jsonl"),
                                           "please build the fork", [SUBSTANTIVE])

    def test_worker_event_never_sends_or_maps(self):
        self.run_notify(self.home, event(WORKER))
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.map_entries(), {})

    def test_worker_event_with_legacy_worker_notify_key_still_never_sends(self):
        # the old knob is inert — no config can turn worker relay back on
        self.run_notify(self.home, event(WORKER), worker_notify=True)
        self.assertEqual(self.api.sent, [])

    def test_scout_and_foreign_events_never_send(self):
        from _env import SCOUT, FOREIGN
        self.run_notify(self.home, event(SCOUT))
        self.run_notify(self.home, event(FOREIGN))
        self.assertEqual(self.api.sent, [])

    def test_main_event_sends_and_targets_itself(self):
        self.sessions[MAIN] = transcript(os.path.join(self.base, "m.jsonl"),
                                         "supervise", [FINAL_REPLY])
        self.run_notify(self.home, event(MAIN))
        self.assertEqual(len(self.api.sent), 1)
        self.assertTrue(any(FINAL_REPLY in t for t in self.api.texts))
        entry = next(iter(self.map_entries().values()))
        self.assertEqual(entry["pane"], MAIN)
        self.assertNotIn("worker_pane", entry)
        self.assertNotIn("context", entry)

    def test_worker_event_without_main_stays_silent(self):
        # no pane matches main_cwd -> fail closed: a worker must not slip through
        # just because the supervisor pane can't be identified
        home = make_home(self.base + "x", tasks=[TASK])
        self.sessions[WORKER] = transcript(os.path.join(self.base, "w4.jsonl"),
                                           "please build", [SUBSTANTIVE])
        fleet = [a for a in self.agents(home) if a["pane_id"] != MAIN]
        self.run_notify(home, event(WORKER), agents_override=fleet)
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.map_entries(), {})


class TestPiSessionResolution(NotifyCase):
    """herdr reports no session ref at all for pi panes, which used to leave the
    excerpt empty (a 'π - firstmate' push with no body). Resolution falls back to
    pi's own storage: ~/.pi/agent/sessions/<cwd, separators as dashes, --wrapped>."""

    CWD = "/tmp/fmw-pi-proj"

    def setUp(self):
        super().setUp()
        self.home = make_home(self.base, tasks=[])
        notify.PI_SESSIONS = os.path.join(self.base, "pi-sessions")
        self.addCleanup(setattr, notify, "PI_SESSIONS",
                        os.path.join(os.path.expanduser("~"), ".pi", "agent", "sessions"))
        self.sessions_dir = os.path.join(notify.PI_SESSIONS, "--tmp-fmw-pi-proj--")
        os.makedirs(self.sessions_dir, exist_ok=True)

    def pi_session(self, name, replies, age):
        path = os.path.join(self.sessions_dir, name)
        transcript(path, "please watch the panes", replies)
        os.utime(path, (age, age))
        return path

    def pi_fleet(self, session=None):
        pane = agent(MAIN, self.CWD, cli="pi", status="done", session=session)
        return [pane]

    def test_no_session_ref_resolves_from_pi_storage(self):
        self.pi_session("old.jsonl", ["stale reply from the rolled-over session"], 1000)
        self.pi_session("2026-09-25T16-47-13-682Z_01a0d976.jsonl", [FINAL_REPLY], 2000)
        self.run_notify(self.home, event(MAIN), agents_override=self.pi_fleet(),
                        firstmate=False)
        self.assertEqual(len(self.api.sent), 1)
        self.assertTrue(any(FINAL_REPLY in t for t in self.api.texts))
        self.assertFalse(any("stale reply" in t for t in self.api.texts))

    def test_stale_path_ref_falls_back_to_pi_storage(self):
        # the recorded ref points into a directory that no longer holds any jsonl
        gone = os.path.join(self.base, "gone-dir", "missing.jsonl")
        self.pi_session("2026-09-25T16-47-13-682Z_01a0d976.jsonl", [FINAL_REPLY], 2000)
        self.run_notify(self.home, event(MAIN),
                        agents_override=self.pi_fleet({"kind": "path", "value": gone}),
                        firstmate=False)
        self.assertTrue(any(FINAL_REPLY in t for t in self.api.texts))

    def test_live_path_ref_still_wins_over_the_fallback(self):
        live = self.pi_session("pinned.jsonl", ["the ref's own reply"], 100)
        self.pi_session("2026-09-25T16-47-13-682Z_01a0d976.jsonl", [FINAL_REPLY], 2000)
        self.run_notify(self.home, event(MAIN),
                        agents_override=self.pi_fleet({"kind": "path", "value": live}),
                        firstmate=False)
        self.assertTrue(any("the ref's own reply" in t for t in self.api.texts))

    def test_unknown_cwd_finds_nothing_and_still_notifies(self):
        pane = agent(MAIN, "/nowhere/at/all", cli="pi", status="done")
        self.run_notify(self.home, event(MAIN), agents_override=[pane],
                        firstmate=False)
        self.assertEqual(len(self.api.sent), 1)         # header-only, not silent

    def test_worker_spawn_cwd_resolves_via_foreground_cwd(self):
        """Herdr 0.8.2 reports a spawned worker pane's cwd as its spawn origin
        (the firstmate home) and no session ref for pi panes. Keying pi storage
        on that cwd would read the MAIN session's transcript as the worker's
        own — mis-attributing main's last message. foreground_cwd (the worktree
        pi actually runs in) must win for pi. Pinned here in upstream mode,
        where every pane still pushes; under firstmate mode that same pane
        never sends at all (TestMainOnlyRelay)."""
        worktree = os.path.join(self.base, "treehouse", "bridge-w1")
        home_dir = os.path.join(notify.PI_SESSIONS, "--" +
                                re.sub(r"[/\\:]", "-", self.home[1:]) + "--")
        work_dir = os.path.join(notify.PI_SESSIONS, "--" +
                                re.sub(r"[/\\:]", "-", worktree[1:]) + "--")
        os.makedirs(home_dir, exist_ok=True)
        os.makedirs(work_dir, exist_ok=True)
        transcript(os.path.join(home_dir, "main.jsonl"), "run the fleet",
                   ["main's private conversation reply"])
        transcript(os.path.join(work_dir, "worker.jsonl"), "please build",
                   ["worker's own final reply"])
        main = agent(MAIN, self.home, cli="pi", status="done",
                     session={"kind": "path", "value": "/none-main.jsonl"})
        worker = agent(WORKER, self.home, cli="pi", status="done")  # cwd = spawn origin
        worker["foreground_cwd"] = worktree
        self.run_notify(self.home, event(WORKER), agents_override=[main, worker],
                        firstmate=False)
        self.assertEqual(len(self.api.sent), 1)
        self.assertTrue(any("worker's own final reply" in t for t in self.api.texts))
        self.assertFalse(any("main's private conversation" in t for t in self.api.texts))


class TestCalmExcerpt(NotifyCase):
    """Calm shapes the MAIN pane's pushes — under firstmate mode the only pane
    that relays. The filter's per-pane rules (foreign panes untouched, mode off
    untouched) are unit-tested in test_firstmate.TestCalm."""

    def calm_home(self, calm):
        home = make_home(self.base, calm=calm, tasks=[TASK])
        self.sessions[MAIN] = transcript(os.path.join(self.base, "m.jsonl"),
                                         "please build the fork", [SUBSTANTIVE, NARRATION])
        # settled() re-reads the pane: whatever status we fire must still be current
        return home

    def test_calm_off_keeps_narration(self):
        home = self.calm_home("off")
        self.run_notify(home, event(MAIN, status="blocked"))
        self.assertTrue(any(NARRATION in t for t in self.api.texts))

    def test_calm_on_drops_midturn_narration(self):
        home = self.calm_home("on")
        self.run_notify(home, event(MAIN, status="blocked"))
        self.assertEqual(len(self.api.sent), 1)        # header-only notification
        self.assertNotIn(NARRATION, self.api.sent[0]["text"])
        self.assertIn("❓", self.api.sent[0]["text"])  # the bodiless status line stays

    def test_calm_on_preserves_final_reply_even_if_short(self):
        home = make_home(self.base, calm="on", tasks=[TASK])
        self.sessions[MAIN] = transcript(os.path.join(self.base, "m2.jsonl"),
                                         "please build", [FINAL_REPLY])
        self.run_notify(home, event(MAIN, status="done"))
        self.assertTrue(any(FINAL_REPLY in t for t in self.api.texts))

    def test_calm_on_preserves_substantive_midturn_block(self):
        home = make_home(self.base, calm="on", tasks=[TASK])
        self.sessions[MAIN] = transcript(os.path.join(self.base, "m3.jsonl"),
                                         "please build", [SUBSTANTIVE])
        self.run_notify(home, event(MAIN, status="blocked"))
        self.assertTrue(any(SUBSTANTIVE.split("\n")[0] in t for t in self.api.texts))

    def test_calm_filters_only_under_firstmate_mode(self):
        # upstream mode (enabled = false): Calm never filters, and a foreign
        # pane pushes its narration untouched — the same pane under firstmate
        # mode never sends at all (TestMainOnlyRelay)
        home = make_home(self.base, calm="on", tasks=[])
        foreign = "wF:p2"
        self.sessions[foreign] = transcript(os.path.join(self.base, "f.jsonl"),
                                            "unrelated work", [NARRATION])
        fleet = [agent(foreign, "/srv/unrelated", status="blocked",
                       session={"kind": "path", "value": self.sessions[foreign]})]
        self.run_notify(home, event(foreign, "blocked"), agents_override=fleet,
                        firstmate=False)
        self.assertTrue(any(NARRATION in t for t in self.api.texts))


class TestSuppressNoopAck(NotifyCase):
    """suppress_noop_ack: the routine no-op acknowledgement is a handled-wake
    confirmation, not news. With the key on, a message that is nothing but
    'Captain, shipshape.' never reaches Telegram at all — no text, no voice,
    no notification — and the sent-marker records it as handled so the
    watcher's content passes treat it as delivered news instead of retrying
    it on every poll. With the key off (the default) the ack still arrives as
    its quiet text bubble, and with it on every other message — extra text
    around the string, a substantive reply — still delivers unchanged."""

    def ack_home(self, reply):
        home = make_home(self.base, tasks=[TASK])
        self.sessions[MAIN] = transcript(os.path.join(self.base, "m.jsonl"),
                                         "please check the fleet", [reply],
                                         ids=["m1"])
        return home

    def log_text(self):
        with open(tg.LOG_PATH, encoding="utf-8") as f:
            return f.read()

    def test_off_by_default_the_ack_still_delivers_as_text(self):
        home = self.ack_home(NOOP_ACK)
        self.run_notify(home, event(MAIN))
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn(NOOP_ACK, self.api.texts[0])

    def test_on_the_ack_never_reaches_telegram(self):
        home = self.ack_home(NOOP_ACK)
        self.run_notify(home, event(MAIN), top={"suppress_noop_ack": True})
        self.assertEqual(self.api.sent, [])        # no text, no notification
        self.assertEqual(self.map_entries(), {})   # nothing routable either
        self.assertIn(f"skip pane={MAIN}: no-op acknowledgement suppressed",
                      self.log_text())             # the drop is logged
        # the marker is stamped: the watcher's content passes see the ack as
        # handled instead of retrying it every poll
        self.assertTrue(notify.content_already_sent(MAIN, "m1"))

    def test_on_surrounding_whitespace_still_matches(self):
        # the voice-skip match semantics: whitespace-collapsed whole-message
        home = self.ack_home("\n  Captain, shipshape.  \n")
        self.run_notify(home, event(MAIN), top={"suppress_noop_ack": True})
        self.assertEqual(self.api.sent, [])
        self.assertTrue(notify.content_already_sent(MAIN, "m1"))

    def test_on_extra_text_still_delivers(self):
        home = self.ack_home("Captain, shipshape. Two tests fail; fixing now.")
        self.run_notify(home, event(MAIN), top={"suppress_noop_ack": True})
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("Two tests fail", self.api.texts[0])

    def test_on_substantive_reply_delivers_unchanged(self):
        home = self.ack_home(SUBSTANTIVE)
        self.run_notify(home, event(MAIN), top={"suppress_noop_ack": True})
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn(SUBSTANTIVE.split("\n")[0], self.api.texts[0])


if __name__ == "__main__":
    unittest.main()
