"""Offline watch.py tests: the poll-based watcher must produce the same
notification flow the event hook produces — done on working->idle for a focused
pane, silence on idle->working, no double-send when both paths catch one
transition, a flapped settle dropping the push without poisoning the debounce,
and the firstmate main-pane-only relay intact through the poll path
(worker transitions are observed and refused with a logged reason; the main
pane pushes as usual).
The watcher must also be accountable: every status change it observes and every
refusal to push lands in bridge.log, and content-anchored delivery pushes a
new final assistant message once the pane is observed non-working — held
while working, deduped against every other path by message identity.
herdr and Telegram are stubbed at the tg module boundary; nothing is live."""

import json
import os
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

import _env  # noqa: F401  (env must be set before tg/notify/watch imports)
import notify
import tg
import watch
from _env import (CHAT, MAIN, WORKER, FakeApi, FakeHerdr, agent, make_home,
                  reset_state, transcript, write_config)

TASK = ("bridge-poll-watch", WORKER, "ship",
        ["working [at=1790000000]: started"])
FINAL_REPLY = "done: watcher shipped"
REPLY_2 = "done: second turn shipped"
REPLY_3 = "done: the frozen turn shipped"


def event(pane, status="done", cli="pi"):
    return {"event": "pane.agent_status_changed",
            "data": {"agent_status": status, "pane_id": pane, "agent": cli,
                     "display_agent": cli, "title": ""}}


class WatchCase(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.base = tempfile.mkdtemp(prefix="fm-watch-")
        self.home = make_home(self.base, tasks=[TASK])
        self.api = FakeApi()
        tg.api = self.api
        self.addCleanup(setattr, tg, "api", _env.REAL_API)
        self.session = transcript(os.path.join(self.base, "w.jsonl"),
                                  "please watch the panes", [FINAL_REPLY])

    def config(self, firstmate=False, settle_ms=0, quiet_seconds=0, **fm):
        cfg = {"chat_id": str(CHAT), "bot_token": "T0K:EN", "replies": False,
               "settle_ms": settle_ms, "quiet_seconds": quiet_seconds,
               "statuses": ["done", "blocked"]}
        if firstmate:
            cfg["firstmate"] = {"enabled": True, "main_cwd": self.home, **fm}
        return cfg

    def run_watch(self, agents, prev=None, cfg=None, prime=False, herdr=None):
        herdr = herdr or FakeHerdr(agents)
        tg.herdr_run = herdr.run
        tg.herdr_json = herdr.json
        self.addCleanup(setattr, tg, "herdr_run", _env.REAL_HERDR_RUN)
        self.addCleanup(setattr, tg, "herdr_json", _env.REAL_HERDR_JSON)
        if prev:
            tg.write_state("last_status.json", prev)
        config = cfg if cfg is not None else self.config()
        write_config(config)
        watch.poll_once(config, prime=prime)
        return herdr

    def main_agent(self, **kw):
        kw.setdefault("session", {"kind": "path", "value": self.session})
        return agent(MAIN, self.home, **kw)


class TestPollDiff(WatchCase):
    def test_working_to_idle_on_a_focused_pane_pushes_done(self):
        """The operator's requirement: focus must not gate completion pushes —
        herdr only reports done for panes you aren't looking at, and the whole
        point is being away with just the phone."""
        self.run_watch([self.main_agent(status="idle", focused=True)],
                       prev={MAIN: "working"})
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("✅", self.api.sent[0]["text"])
        self.assertTrue(any(FINAL_REPLY in t for t in self.api.texts))

    def test_idle_to_working_is_silent(self):
        self.run_watch([self.main_agent(status="working")], prev={MAIN: "idle"})
        self.assertEqual(self.api.sent, [])
        self.assertEqual(tg.read_state("last_status.json", {}), {MAIN: "working"})

    def test_entering_blocked_pushes(self):
        self.run_watch([self.main_agent(status="blocked")], prev={MAIN: "working"})
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("❓", self.api.sent[0]["text"])

    def test_idle_after_blocked_is_not_news_by_default(self):
        self.run_watch([self.main_agent(status="idle")], prev={MAIN: "blocked"})
        self.assertEqual(self.api.sent, [])

    def test_first_pass_primes_the_baseline_without_pushing(self):
        # a poller restart must not re-announce every already-finished pane
        self.run_watch([self.main_agent(status="done")], prev={}, prime=True)
        self.assertEqual(self.api.sent, [])
        self.assertEqual(tg.read_state("last_status.json", {}), {MAIN: "done"})

    def test_new_pane_appearing_done_pushes(self):
        # unseen pane id + a status worth pushing: that IS a transition
        self.run_watch([self.main_agent(status="done")], prev={})
        self.assertEqual(len(self.api.sent), 1)

    def test_agents_filter_applies_through_the_poll_path(self):
        self.run_watch([self.main_agent(status="done", cli="codex")],
                       prev={MAIN: "working"},
                       cfg={"chat_id": str(CHAT), "bot_token": "T0K:EN",
                            "replies": False, "settle_ms": 0, "quiet_seconds": 0,
                            "statuses": ["done"], "agents": ["claude"]})
        self.assertEqual(self.api.sent, [])


class TestDebounce(WatchCase):
    def test_event_and_poll_catching_the_same_transition_send_once(self):
        """The race both paths can win: the event hook and the watcher each read
        the pane as working before either records the idle. Whichever delivers
        first stamps pane:status; quiet_seconds eats the other."""
        cfg = self.config(quiet_seconds=60)
        herdr = FakeHerdr([self.main_agent(status="idle")])
        tg.herdr_run = herdr.run
        tg.herdr_json = herdr.json
        self.addCleanup(setattr, tg, "herdr_run", _env.REAL_HERDR_RUN)
        self.addCleanup(setattr, tg, "herdr_json", _env.REAL_HERDR_JSON)
        tg.write_state("last_status.json", {MAIN: "working"})
        write_config(cfg)
        watch.poll_once(cfg)                              # the poll side delivers
        self.assertEqual(len(self.api.sent), 1)
        tg.write_state("last_status.json", {MAIN: "working"})  # replay the race
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(event(MAIN, "idle"))
        self.addCleanup(os.environ.pop, "HERDR_PLUGIN_EVENT_JSON", None)
        notify.main()                                     # the event side is eaten
        self.assertEqual(len(self.api.sent), 1)

    def test_late_event_after_the_poll_recorded_is_not_a_transition(self):
        # the common order needs no debounce at all: the poll's swap already
        # made the pane idle, so the event diffs idle->idle and stays quiet
        cfg = self.config(quiet_seconds=0)
        herdr = FakeHerdr([self.main_agent(status="idle")])
        tg.herdr_run = herdr.run
        tg.herdr_json = herdr.json
        self.addCleanup(setattr, tg, "herdr_run", _env.REAL_HERDR_RUN)
        self.addCleanup(setattr, tg, "herdr_json", _env.REAL_HERDR_JSON)
        tg.write_state("last_status.json", {MAIN: "working"})
        write_config(cfg)
        watch.poll_once(cfg)
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(event(MAIN, "idle"))
        self.addCleanup(os.environ.pop, "HERDR_PLUGIN_EVENT_JSON", None)
        notify.main()
        self.assertEqual(len(self.api.sent), 1)


class LogMixin:
    """bridge.log assertions scoped to this test's own lines."""

    def log_setUp(self):
        watch._NOTES.clear()
        try:
            with open(tg.LOG_PATH, encoding="utf-8") as f:
                self._log0 = len(f.read().splitlines())
        except OSError:
            self._log0 = 0

    def log_since(self):
        with open(tg.LOG_PATH, encoding="utf-8") as f:
            return f.read().splitlines()[self._log0:]

    def assert_log(self, sub):
        lines = self.log_since()
        self.assertTrue(any(sub in ln for ln in lines),
                        f"no bridge.log line contains {sub!r}; log since setUp:\n"
                        + "\n".join(lines))


class TestSettleFlap(WatchCase):
    def test_flapped_transition_is_dropped_and_debounce_not_stamped(self):
        # list says idle (the transition), agent get says working again (the
        # flap): no push — and last_sent stays empty, so the real done that
        # follows can still send
        idle = self.main_agent(status="idle")
        herdr = FakeHerdr([idle])
        real_json = herdr.json

        def json(*args, timeout=30):
            if args[:2] == ("agent", "get"):
                return {"agent": {**idle, "agent_status": "working"}}, ""
            return real_json(*args, timeout=timeout)

        herdr.json = json
        self.run_watch([idle], prev={MAIN: "working"}, herdr=herdr,
                       cfg=self.config(settle_ms=1))
        self.assertEqual(self.api.sent, [])
        self.assertEqual(tg.read_state("last_sent.json", {}), {})


class TestFirstmatePosture(WatchCase, LogMixin):
    """The operator's contract through the poll path: under firstmate mode only
    the main pane's completions reach the phone. A worker transition is still
    observed (one log line) and its refusal logged; the main pane pushes."""

    def setUp(self):
        super().setUp()
        self.log_setUp()
        self.worker = agent(WORKER, f"{self.home}-treehouse/w1", cli="opencode",
                            status="idle", focused=True, title="OC | watch panes",
                            session={"kind": "path", "value": self.session})

    def test_worker_done_through_the_poll_is_silent(self):
        self.run_watch([agent(MAIN, self.home, status="working"), self.worker],
                       prev={WORKER: "working"}, cfg=self.config(firstmate=True))
        self.assertEqual(self.api.sent, [])
        self.assertEqual(tg.read_state(tg.MSG_MAP, {}), {})   # nothing mapped, either
        self.assert_log(f"outbound relay is main-pane only — not sending for {WORKER}")

    def test_legacy_worker_notify_key_is_inert_through_the_poll(self):
        # the old knob cannot turn worker relay back on
        self.run_watch([agent(MAIN, self.home, status="working"), self.worker],
                       prev={WORKER: "working"},
                       cfg=self.config(firstmate=True, worker_notify=True))
        self.assertEqual(self.api.sent, [])

    def test_main_done_through_the_poll_still_pushes(self):
        self.run_watch([self.main_agent(status="idle", focused=True)],
                       prev={MAIN: "working"}, cfg=self.config(firstmate=True))
        self.assertEqual(len(self.api.sent), 1)
        self.assertTrue(any(FINAL_REPLY in t for t in self.api.texts))


class TestPollSeconds(unittest.TestCase):
    def test_default_clamp_and_off(self):
        self.assertEqual(watch.poll_seconds({}), 20)
        self.assertEqual(watch.poll_seconds({"watch": {}}), 20)
        self.assertEqual(watch.poll_seconds({"watch": "junk"}), 20)
        self.assertEqual(watch.poll_seconds({"watch": {"poll_seconds": "x"}}), 20)
        self.assertEqual(watch.poll_seconds({"watch": {"poll_seconds": 3}}), 5)
        self.assertEqual(watch.poll_seconds({"watch": {"poll_seconds": 60}}), 60)
        self.assertEqual(watch.poll_seconds({"watch": {"poll_seconds": 0}}), 0)
        self.assertEqual(watch.poll_seconds({"watch": {"poll_seconds": -5}}), 0)

    def test_content_delivery_default_and_off(self):
        self.assertTrue(watch.content_delivery({}))
        self.assertTrue(watch.content_delivery({"watch": {}}))
        self.assertTrue(watch.content_delivery({"watch": "junk"}))
        self.assertFalse(watch.content_delivery({"watch": {"content_delivery": False}}))
        self.assertFalse(watch.content_delivery({"watch": {"content_delivery": "false"}}))
        self.assertTrue(watch.content_delivery({"watch": {"content_delivery": True}}))


class TestRestartDelivery(WatchCase, LogMixin):
    """The swallow-on-restart: a watcher death mid-message used to leave the
    completed-but-undelivered final message recorded as known by the recovery
    restart's priming pass — a permanent swallow. Priming must consult the
    sent-marker: a final message newer than the pane's last delivered one is
    delivered, never primed; a delivered one is never re-sent."""

    def setUp(self):
        super().setUp()
        self.log_setUp()
        self.session = transcript(os.path.join(self.base, "w.jsonl"),
                                  "please watch the panes", [FINAL_REPLY], ids=["m1"])

    def steady(self, status="idle", cfg=None):
        self.run_watch([self.main_agent(status=status)], prev={MAIN: status},
                       cfg=cfg if cfg is not None else self.config())

    def restart(self, status="idle"):
        # the recovery restart: the watcher's first pass runs with prime=True
        self.run_watch([self.main_agent(status=status)], prev={MAIN: status},
                       prime=True)

    def transcript_now(self, *replies):
        ids = [f"m{i + 1}" for i in range(len(replies) + 1)]
        transcript(self.session, "please watch the panes",
                   [FINAL_REPLY, *replies], ids=ids)

    def test_freeze_then_restart_mid_message_still_delivers(self):
        """The 19:41 incident replayed: m2 delivered; m3 lands and its turn
        completes while the watcher is frozen (no poll ever observes it); the
        poller restarts. The prime pass must push m3, not record it as known."""
        self.steady()                                   # baseline: m1 recorded
        self.transcript_now(REPLY_2)
        self.steady()                                   # healthy poll pushed m2
        self.assertEqual(len(self.api.sent), 1)
        self.transcript_now(REPLY_2, REPLY_3)
        self.restart()                                  # frozen through m3's turn
        self.assertEqual(len(self.api.sent), 2)
        self.assertTrue(any(REPLY_3 in t for t in self.api.texts))
        self.assert_log("undelivered on restart m3")
        self.assertIn(notify.content_key(MAIN, "m3"),
                      tg.read_state("content_sent.json", {}))
        self.assertEqual(tg.read_state(watch.CONTENT_LAST, {}), {MAIN: "m3"})
        self.restart()                                  # a second restart: quiet
        self.assertEqual(len(self.api.sent), 2)

    def test_restart_with_everything_already_sent_never_resends(self):
        self.steady()
        self.transcript_now(REPLY_2)
        self.steady()                                   # m2 delivered
        self.assertEqual(len(self.api.sent), 1)
        self.restart()                                  # transcript still ends at m2
        self.assertEqual(len(self.api.sent), 1)         # no re-send
        self.steady()                                   # and stays quiet after it
        self.assertEqual(len(self.api.sent), 1)

    def test_restart_holds_an_undelivered_message_while_pane_working(self):
        # the restart lands mid-turn: nothing pushes while the pane works, and
        # the message is NOT primed away — it delivers on the first idle poll
        self.steady()
        self.transcript_now(REPLY_2)
        self.steady()                                   # m2 delivered
        self.transcript_now(REPLY_2, REPLY_3)
        self.restart(status="working")
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(tg.read_state(watch.CONTENT_LAST, {}), {MAIN: "m2"})
        self.assert_log("holding new message m3 — pane still working")
        self.steady()                                   # pane observed idle later
        self.assertEqual(len(self.api.sent), 2)
        self.assertTrue(any(REPLY_3 in t for t in self.api.texts))

    def test_restart_blocked_pane_leaves_the_message_unprimed(self):
        # blocked is the status diff's business, but the content pass must
        # still not prime the undelivered message away
        self.steady()
        self.transcript_now(REPLY_2)
        self.steady()                                   # m2 delivered
        self.transcript_now(REPLY_2, REPLY_3)
        self.restart(status="blocked")
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(tg.read_state(watch.CONTENT_LAST, {}), {MAIN: "m2"})
        self.assert_log("left unprimed")
        self.steady(status="blocked")
        self.assertEqual(len(self.api.sent), 1)         # still blocked: nothing
        self.steady()                                   # idle at last: delivers
        self.assertEqual(len(self.api.sent), 2)


class TestSlowCycleWatchdog(WatchCase, LogMixin):
    """The stall tripwire: a poll cycle that overruns 2x poll_seconds is logged
    loudly, because a silent overrun — process alive, no observation lines — is
    exactly how the 2026-09-25 freeze looked from the outside."""

    def setUp(self):
        super().setUp()
        self.log_setUp()

    def test_note_logs_loudly_only_past_2x_poll_seconds(self):
        watch.slow_cycle_note(20, 44.9)
        self.assert_log("watch: slow cycle 44.9s")
        watch.slow_cycle_note(20, 40.0)      # exactly 2x is not yet slow
        watch.slow_cycle_note(20, 5.0)       # a healthy cycle says nothing
        watch.slow_cycle_note(0, 999.0)      # watcher off: nothing to say
        lines = [ln for ln in self.log_since() if "slow cycle" in ln]
        self.assertEqual(len(lines), 1)

    def test_a_cycle_that_overruns_its_interval_logs_from_the_loop(self):
        # poll_seconds min-clamps at 5s, so the loop is driven with a patched
        # 1s interval and a poll_once that stalls 2.2s: one wedge, one loud line
        write_config(self.config())
        stop = threading.Event()

        def stall(cfg, prime=False):
            time.sleep(2.2)
            stop.set()                     # one cycle is all this test needs

        with mock.patch.object(watch, "poll_seconds", return_value=1), \
             mock.patch.object(watch, "poll_once", side_effect=stall):
            started = time.monotonic()
            watch.watch_loop(stop)
        self.assertLess(time.monotonic() - started, 20)  # exited on its own
        self.assert_log("watch: status watcher running")
        self.assert_log("watch: slow cycle")


class TestMessageIdentity(unittest.TestCase):
    """The sent-marker is only as good as the identity it keys on."""

    def test_id_and_timestamp_ride_along(self):
        self.assertEqual(notify.message_identity({"id": "m1"}, {"ts": "7"}, "text"), "m1@7")
        self.assertEqual(notify.message_identity({}, {"id": "m2", "timestamp": "8"}, "t"), "m2@8")

    def test_no_id_falls_back_to_text_hash(self):
        a = notify.message_identity({}, {}, "same words")
        b = notify.message_identity({}, {}, "same words")
        c = notify.message_identity({}, {}, "other words")
        d = notify.message_identity({}, {"ts": "9"}, "same words")
        self.assertTrue(a.startswith("h") and a == b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)  # a later timestamp is a different message

    def test_jsonl_identity_and_text(self):
        path = transcript(os.path.join(tempfile.mkdtemp(prefix="fm-id-"), "s.jsonl"),
                          "prompt", ["one", "two"], ids=["m1", "m2"])
        identity, text = notify.last_assistant_jsonl(path)
        self.assertEqual((identity, text), ("m2", "two"))


class TestObservability(WatchCase, LogMixin):
    """No silent drops: every observation and every skip leaves a log line."""

    def setUp(self):
        super().setUp()
        self.log_setUp()

    def test_change_observation_logs_pane_prev_new_seq(self):
        self.run_watch([self.main_agent(status="idle", focused=True)],
                       prev={MAIN: "working"})
        self.assert_log("watch: pane=w3:p1 working->idle seq=1")

    def test_missing_seq_is_omitted_not_fabricated(self):
        entry = self.main_agent(status="idle")
        entry.pop("state_change_seq")
        self.run_watch([entry], prev={MAIN: "working"})
        line = next(ln for ln in self.log_since() if "working->idle" in ln)
        self.assertNotIn("seq=", line)

    def test_prime_and_working_skips_log_their_reasons(self):
        self.run_watch([self.main_agent(status="done")], prev={}, prime=True)
        self.assert_log("priming baseline")
        self.run_watch([self.main_agent(status="working")], prev={MAIN: "idle"})
        self.assert_log("->working is not news")

    def test_filter_skips_log_their_reasons(self):
        self.run_watch([self.main_agent(status="done")], prev={MAIN: "working"},
                       cfg={"chat_id": str(CHAT), "bot_token": "T0K:EN", "replies": False,
                            "settle_ms": 0, "quiet_seconds": 0, "statuses": ["blocked"]})
        self.assert_log("status done not in statuses filter")
        self.run_watch([self.main_agent(status="done", cli="codex")], prev={MAIN: "working"},
                       cfg={"chat_id": str(CHAT), "bot_token": "T0K:EN", "replies": False,
                            "settle_ms": 0, "quiet_seconds": 0, "statuses": ["done"],
                            "agents": ["claude"]})
        self.assert_log("not in agents filter")

    def test_debounce_skip_logs_its_reason(self):
        # no session ref and an unresolvable cwd: identity "" so the marker
        # cannot apply and the debounce itself is what must speak up
        cfg = self.config(quiet_seconds=30)
        self.run_watch([agent(MAIN, "/nowhere/fm-obs", status="done", session=None)],
                       prev={MAIN: "working"}, cfg=cfg)
        self.assertEqual(len(self.api.sent), 1)
        notify.deliver(cfg, MAIN, "done", "working", agent="pi", cli="pi")
        self.assertEqual(len(self.api.sent), 1)
        self.assert_log("debounced pane=w3:p1")

    def test_send_failure_logs_and_does_not_stamp_the_marker(self):
        cfg = self.config()
        self.run_watch([self.main_agent(status="idle")], prev={MAIN: "idle"}, cfg=cfg)
        transcript(self.session, "please watch the panes", [FINAL_REPLY, "done: retry me"],
                   ids=["m1", "m2"])
        self.api.fail_send = "500 boom"
        self.run_watch([self.main_agent(status="idle")], prev={MAIN: "idle"}, cfg=cfg)
        self.assertEqual(self.api.sent, [])           # the push was eaten by the failure
        self.assert_log("send failed")
        self.assertEqual(tg.read_state("content_sent.json", {}), {})
        self.api.fail_send = ""
        self.run_watch([self.main_agent(status="idle")], prev={MAIN: "idle"}, cfg=cfg)
        self.assertEqual(len(self.api.sent), 1)
        self.assertTrue(any("retry me" in t for t in self.api.texts))

    def test_unresolvable_transcript_logs_once_not_every_poll(self):
        cfg = self.config()
        entry = agent(MAIN, "/nowhere/fm-obs2", status="idle", session=None)
        self.run_watch([entry], prev={MAIN: "idle"}, cfg=cfg)
        self.run_watch([entry], prev={MAIN: "idle"}, cfg=cfg)
        hits = [ln for ln in self.log_since() if "no transcript" in ln]
        self.assertEqual(len(hits), 1)


class TestContentDelivery(WatchCase, LogMixin):
    """Content-anchored delivery: a new final assistant message pushes once the
    pane is observed non-working, held while working, deduped by identity."""

    REPLY_2 = "done: second turn shipped"

    def setUp(self):
        super().setUp()
        self.log_setUp()
        self.session = transcript(os.path.join(self.base, "w.jsonl"),
                                  "please watch the panes", [FINAL_REPLY], ids=["m1"])

    def steady(self, status="idle", cfg=None):
        # prev == status: no status transition sampled, only the content pass speaks
        self.run_watch([self.main_agent(status=status)], prev={MAIN: status},
                       cfg=cfg if cfg is not None else self.config())

    def test_new_final_message_pushes_with_no_status_transition(self):
        """The operator's missed case: a whole turn fits inside one poll interval,
        so the sampled status never changes (idle -> idle) — the transcript's
        new final message is the only witness, and it must reach the phone."""
        self.steady()                                   # baseline: m1 recorded
        self.assertEqual(self.api.sent, [])
        transcript(self.session, "please watch the panes", [FINAL_REPLY, self.REPLY_2],
                   ids=["m1", "m2"])
        self.steady()
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("✅", self.api.sent[0]["text"])
        self.assertTrue(any(self.REPLY_2 in t for t in self.api.texts))
        self.assert_log("content: pane=w3:p1 new final message m2")
        self.steady()                                   # same message again: never twice
        self.assertEqual(len(self.api.sent), 1)

    def test_working_samples_hold_the_message_until_non_working(self):
        """Status samples working -> working across polls while the transcript
        gained a final message: nothing pushes mid-working, and the held
        message pushes on the first non-working observation — even when that
        observation shows no status transition either."""
        self.run_watch([self.main_agent(status="working")], prev={MAIN: "working"})
        transcript(self.session, "please watch the panes", [FINAL_REPLY, self.REPLY_2],
                   ids=["m1", "m2"])
        self.run_watch([self.main_agent(status="working")], prev={MAIN: "working"})
        self.assertEqual(self.api.sent, [])
        self.assert_log("holding new message m2 — pane still working")
        self.steady()                                   # idle -> idle: no transition at all
        self.assertEqual(len(self.api.sent), 1)
        self.assertTrue(any(self.REPLY_2 in t for t in self.api.texts))

    def test_same_message_not_pushed_twice_across_poll_and_event(self):
        cfg = self.config()                             # quiet_seconds 0: only the marker dedupes
        self.steady(cfg=cfg)
        transcript(self.session, "please watch the panes", [FINAL_REPLY, self.REPLY_2],
                   ids=["m1", "m2"])
        self.steady(cfg=cfg)                            # the content pass delivers m2
        self.assertEqual(len(self.api.sent), 1)
        tg.write_state("last_status.json", {MAIN: "working"})  # replay the race: the
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(event(MAIN, "idle"))
        self.addCleanup(os.environ.pop, "HERDR_PLUGIN_EVENT_JSON", None)
        notify.main()                                   # event side must be eaten
        self.assertEqual(len(self.api.sent), 1)
        self.assert_log("final message already pushed")

    def test_status_push_dedupes_against_the_following_content_pass(self):
        self.run_watch([self.main_agent(status="idle")], prev={MAIN: "working"})
        self.assertEqual(len(self.api.sent), 1)         # the status diff delivered m1
        self.steady()                                   # content pass: marker says no
        self.assertEqual(len(self.api.sent), 1)

    def test_first_pass_primes_the_content_baseline(self):
        # a watcher restart must not announce whatever was already sitting in
        # the transcript, but a message that lands after the prime still pushes
        self.run_watch([self.main_agent(status="idle")], prev={}, prime=True)
        self.assertEqual(self.api.sent, [])
        transcript(self.session, "please watch the panes", [FINAL_REPLY, self.REPLY_2],
                   ids=["m1", "m2"])
        self.steady()
        self.assertEqual(len(self.api.sent), 1)

    def test_blocked_pane_is_the_status_diffs_business(self):
        self.steady()
        transcript(self.session, "please watch the panes", [FINAL_REPLY, self.REPLY_2],
                   ids=["m1", "m2"])
        self.steady(status="blocked")
        self.assertEqual(self.api.sent, [])

    def test_content_delivery_false_restores_the_pure_status_diff(self):
        cfg = self.config()
        cfg["watch"] = {"content_delivery": False}
        self.steady(cfg=cfg)
        transcript(self.session, "please watch the panes", [FINAL_REPLY, self.REPLY_2],
                   ids=["m1", "m2"])
        self.steady(cfg=cfg)
        self.assertEqual(self.api.sent, [])            # no content push
        self.run_watch([self.main_agent(status="idle")], prev={MAIN: "working"}, cfg=cfg)
        self.assertEqual(len(self.api.sent), 1)        # status diff unaffected

    def test_firstmate_posture_holds_on_the_content_path(self):
        """A worker's brand-new final message never pushes under firstmate
        mode, however the transcript moves: the content pass skips non-main
        panes up front (one standing log line) and the send-path gate stays
        the authority."""
        worker = agent(WORKER, f"{self.home}-treehouse/w1", cli="pi",
                      status="idle", focused=True, title="π - worker",
                      session={"kind": "path", "value": self.session})
        cfg = self.config(firstmate=True)
        self.run_watch([agent(MAIN, self.home, status="working"), worker],
                       prev={WORKER: "idle"}, cfg=cfg)  # worker baseline
        transcript(self.session, "please watch the panes", [FINAL_REPLY, self.REPLY_2],
                   ids=["m1", "m2"])
        self.run_watch([agent(MAIN, self.home, status="working"), worker],
                       prev={WORKER: "idle"}, cfg=cfg)
        self.assertEqual(self.api.sent, [])            # never pushed, never mapped
        self.assertEqual(tg.read_state(tg.MSG_MAP, {}), {})
        self.assert_log(f"content: pane={WORKER} is not the firstmate main pane")

    def test_spawned_pane_resolves_its_transcript_by_foreground_cwd(self):
        """Herdr 0.8.2 reports a spawned pane's cwd as its spawn origin (the
        firstmate home) and no session ref for pi; the content pass must key
        pi storage on foreground_cwd — the worktree pi runs in — or it would
        track and push the MAIN session's transcript as this pane's output."""
        worktree = os.path.join(self.base, "treehouse", "bridge-w1")
        pi_root = os.path.join(self.base, "pi-sessions")
        notify.PI_SESSIONS = pi_root
        self.addCleanup(setattr, notify, "PI_SESSIONS",
                        os.path.join(os.path.expanduser("~"), ".pi", "agent", "sessions"))
        home_dir = os.path.join(pi_root, "--" +
                                re.sub(r"[/\\:]", "-", self.home[1:]) + "--")
        work_dir = os.path.join(pi_root, "--" +
                                re.sub(r"[/\\:]", "-", worktree[1:]) + "--")
        os.makedirs(home_dir, exist_ok=True)
        os.makedirs(work_dir, exist_ok=True)
        transcript(os.path.join(home_dir, "main.jsonl"), "run the fleet",
                   ["main's private m1"], ids=["z1"])
        session = transcript(os.path.join(work_dir, "worker.jsonl"), "please build",
                             [FINAL_REPLY], ids=["m1"])
        pane = agent(MAIN, self.home, cli="pi", status="idle")  # cwd = spawn origin
        pane["foreground_cwd"] = worktree

        def steady():
            self.run_watch([dict(pane)], prev={MAIN: "idle"}, cfg=self.config())

        steady()                                        # baseline: m1 recorded
        self.assertEqual(self.api.sent, [])
        transcript(session, "please build", [FINAL_REPLY, self.REPLY_2],
                   ids=["m1", "m2"])
        steady()                                        # m2 is new: push
        self.assertEqual(len(self.api.sent), 1)
        self.assertTrue(any(self.REPLY_2 in t for t in self.api.texts))
        self.assertFalse(any("main's private" in t for t in self.api.texts))
        self.assertEqual(os.path.dirname(notify.pi_session_file(worktree)), work_dir)


if __name__ == "__main__":
    unittest.main()
