"""Offline tests for the mid-run relay: the 2026-10-10 operator-confirmed miss
(firstmate's miniflux explanation never reached the phone) proved the
single-slot content pass structurally cannot deliver a multi-message turn —
it anchors on "the last message", so every superseded intermediate was held,
replaced and silently lost, and whether one arrived at all depended on a poll
luckily catching the pane idle while it was newest.

The contract under test: under firstmate mode the main pane's relay is
text-block-driven — every sealed assistant text block after the delivery
boundary relays exactly once, in transcript order, with or without any status
transition; the transcript tail keeps the hold-while-working rule and delivers
as the final; a done/idle status push defers behind undelivered messages so
the final never jumps the queue; nothing leaks but text blocks (thinking and
tool-call records never qualify); Calm's narration rule covers intermediates;
speech follows the existing [tts] path with speak_final_only keeping mid-run
messages text-only; and with firstmate mode off nothing changes (the
single-slot behavior stays, upstream parity). herdr and Telegram are stubbed
at the tg module boundary; nothing is live."""

import json
import os
import tempfile
import unittest
from unittest import mock

import _env  # noqa: F401  (env must be set before tg/notify/watch imports)
import notify
import tg
import tts
import watch
from _env import (CHAT, MAIN, WORKER, FakeApi, FakeHerdr, agent, make_home,
                  reset_state, transcript, write_config)

# tonight's real identities, replayed (bridge.log 2026-10-09 20:19-20:29 -05)
MINIFLUX_ID = "91c53b4f@1791595503210"
TURNSTILE_ID = "38f10add@1791595608123"
BUGREPORT_ID = "5e52a776@1791595717977"
FINAL_ID = "a8f4b693@1791595740181"

MINIFLUX_TEXT = ("Operator, the trading pair is retired — both scaled to zero. "
                 "Miniflux is a self-hosted RSS feed reader with its own "
                 "dedicated Postgres database alongside it.")
TURNSTILE_TEXT = "Operator, the turnstile is done and fully landed."
BUGREPORT_TEXT = ("Operator, you were right the first time — filed with the "
                  "proof attached.")
FINAL_TEXT = "Operator, that's the sweep complete for tonight."
NOOP_ACK = "Captain, shipshape."                  # the routine handled-wake all-clear
NOOP_ACK_ID = "ack@1791595800000"


class MidrunCase(unittest.TestCase):
    def setUp(self):
        reset_state()
        watch._NOTES.clear()
        self.base = tempfile.mkdtemp(prefix="fm-midrun-")
        self.home = make_home(self.base)
        self.api = FakeApi()
        tg.api = self.api
        self.addCleanup(setattr, tg, "api", _env.REAL_API)
        herdr = FakeHerdr([])
        tg.herdr_run = herdr.run
        tg.herdr_json = herdr.json
        self.addCleanup(setattr, tg, "herdr_run", _env.REAL_HERDR_RUN)
        self.addCleanup(setattr, tg, "herdr_json", _env.REAL_HERDR_JSON)
        try:
            with open(tg.LOG_PATH, encoding="utf-8") as f:
                self._log0 = len(f.read().splitlines())
        except OSError:
            self._log0 = 0
        self.spoken = []
        self.speak_patcher = mock.patch.object(
            notify.tts, "maybe_speak",
            side_effect=lambda cfg, text, target, intermediate=False:
                self.spoken.append((text, intermediate)))
        self.speak = self.speak_patcher.start()
        self.addCleanup(self.speak_patcher.stop)

    def log_since(self):
        with open(tg.LOG_PATH, encoding="utf-8") as f:
            return f.read().splitlines()[self._log0:]

    def assert_log(self, sub):
        lines = self.log_since()
        self.assertTrue(any(sub in ln for ln in lines),
                        f"no bridge.log line contains {sub!r}; log since setUp:\n"
                        + "\n".join(lines))

    def config(self, **fm):
        cfg = {"chat_id": str(CHAT), "bot_token": "T0K:EN", "replies": False,
               "settle_ms": 0, "quiet_seconds": 0, "statuses": ["done", "blocked"]}
        if fm or fm == {}:
            cfg["firstmate"] = {"enabled": True, "main_cwd": self.home, **fm}
        return cfg

    def session_file(self, replies, ids=None, tss=None):
        path = os.path.join(self.base, "w.jsonl")
        return transcript(path, "run the fleet", replies, ids=ids, tss=tss)

    def poll(self, status="idle", prev=None, replies=(), ids=None, tss=None,
             cfg=None, prime=False, session=None):
        session = session or self.session_file(list(replies), ids=ids, tss=tss)
        entry = agent(MAIN, self.home, status=status,
                      session={"kind": "path", "value": session})
        return self.run_once(entry, prev if prev is not None else {MAIN: status},
                             cfg if cfg is not None else self.config(),
                             prime=prime), session

    def run_once(self, entry, prev, cfg, prime=False):
        herdr = FakeHerdr([entry])
        tg.herdr_run = herdr.run
        tg.herdr_json = herdr.json
        tg.write_state("last_status.json", prev)
        write_config(cfg)
        watch.poll_once(cfg, prime=prime)
        return entry

    def sent_texts(self):
        return self.api.texts

    def sent_count(self):
        return len(self.api.sent)

    def marker(self):
        return tg.read_state("content_sent.json", {})

    def content_last(self):
        return tg.read_state(watch.CONTENT_LAST, {}).get(MAIN)


class TestTriggerGap(MidrunCase):
    """The diagnosed failure mode: a operator-facing message with NO status
    change (the pane works straight through) still relays."""

    def test_interior_message_relays_with_no_status_change_ever(self):
        """Tonight's exact shape: pane working -> working across polls while
        the transcript gains messages. The superseded interior must reach the
        phone even though no transition ever fires; only the tail waits."""
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["first word"], ids=["m1"])          # baseline
        self.assertEqual(self.sent_count(), 0)
        # the miniflux message lands and is superseded while the pane keeps
        # working: it is complete by supersession, and it must relay now
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["first word", MINIFLUX_TEXT, "a newer still"],
                  ids=["m1", MINIFLUX_ID, "newer@1791595600000"])
        self.assertEqual(self.sent_count(), 1)                  # interior relayed
        self.assertTrue(any(MINIFLUX_TEXT in t for t in self.sent_texts()))
        self.assertIn(notify.content_key(MAIN, MINIFLUX_ID), self.marker())
        self.assert_log(f"new mid-run message {MINIFLUX_ID[:24]}")

    def test_tail_still_held_while_working_then_delivers_as_final(self):
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["m1 text"], ids=["m1"])
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["m1 text", MINIFLUX_TEXT, FINAL_TEXT],
                  ids=["m1", MINIFLUX_ID, FINAL_ID])
        # the interior relayed immediately; the tail is held, not recorded
        self.assertEqual(self.sent_count(), 1)
        self.assertTrue(any(MINIFLUX_TEXT in t for t in self.sent_texts()))
        self.assertFalse(any(FINAL_TEXT in t for t in self.sent_texts()))
        self.assert_log(f"holding new message {FINAL_ID[:24]} — pane still working")
        self.assertEqual(self.content_last(), MINIFLUX_ID)
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["m1 text", MINIFLUX_TEXT, FINAL_TEXT],
                  ids=["m1", MINIFLUX_ID, FINAL_ID])
        self.assertEqual(self.sent_count(), 2)                  # the final, once
        self.assertTrue(any(FINAL_TEXT in t for t in self.sent_texts()))
        self.assertEqual(self.content_last(), FINAL_ID)

    def test_regression_tonights_window_replay(self):
        """The operator's proof, replayed: a working turn that gains one
        operator-facing message per poll (tonight: 20:19, 20:20, 20:22, 20:25,
        20:27, 20:28 -05) and finishes. Before the fix only the
        transition-time tail ever reached the phone; four messages were
        proven lost (bridge.log 'holding' lines with no sent-marker entry)."""
        ids = ["prev@1791594000000",
               "1094027d@1791595130564", "0b799673@1791595185447",
               "69fcc595@1791595332944", MINIFLUX_ID, TURNSTILE_ID,
               BUGREPORT_ID, FINAL_ID]
        texts = ["the previous turn's close",
                 "the memory map", "the leak parking note",
                 "the pairing note", MINIFLUX_TEXT, TURNSTILE_TEXT,
                 BUGREPORT_TEXT, FINAL_TEXT]
        # baseline: only the previous turn's final is in the transcript
        self.poll(status="working", prev={MAIN: "working"},
                  replies=texts[:1], ids=ids[:1])
        self.assertEqual(self.sent_count(), 0)
        # the turn grows one operator-facing message per working poll — each
        # new message seals the one before it, which must relay immediately
        for landed in range(2, len(texts)):
            self.poll(status="working", prev={MAIN: "working"},
                      replies=texts[:landed], ids=ids[:landed])
        # the turn finishes between polls
        self.poll(status="idle", prev={MAIN: "working"},
                  replies=texts, ids=ids)
        joined = " || ".join(self.sent_texts())
        for text in texts[1:]:
            self.assertIn(text, joined)
        self.assertEqual(self.sent_count(), len(texts) - 1)   # every turn message
        for mid in ids[1:]:
            self.assertIn(notify.content_key(MAIN, mid), self.marker())

    def test_turn_entirely_inside_one_poll_interval_relayed_in_order(self):
        replies = [MINIFLUX_TEXT, TURNSTILE_TEXT, FINAL_TEXT]
        ids = [MINIFLUX_ID, TURNSTILE_ID, FINAL_ID]
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline"], ids=["m0"])
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", *replies], ids=["m0", *ids])
        self.assertEqual(self.sent_count(), 3)
        order = [t for t in self.sent_texts()]
        self.assertLess(order.index(next(t for t in order if MINIFLUX_TEXT in t)),
                        order.index(next(t for t in order if TURNSTILE_TEXT in t)))
        self.assertLess(order.index(next(t for t in order if TURNSTILE_TEXT in t)),
                        order.index(next(t for t in order if FINAL_TEXT in t)))
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", *replies], ids=["m0", *ids])
        self.assertEqual(self.sent_count(), 3)                  # never twice


class TestOrderingAndDedup(MidrunCase):
    def test_status_push_never_jumps_ahead_of_pending_intermediates(self):
        """working->done in the same poll that carries undelivered interiors:
        the pass runs first and the status delivery defers behind anything
        unsent, so the phone reads interiors then the final."""
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["baseline"], ids=["m0"])
        self.api.fail_send = "500 boom"
        self.poll(status="idle", prev={MAIN: "working"},
                  replies=["baseline", MINIFLUX_TEXT, FINAL_TEXT],
                  ids=["m0", MINIFLUX_ID, FINAL_ID])
        self.assertEqual(self.sent_count(), 0)                  # the send failed
        self.assert_log("deferred for ordered retry")
        self.api.fail_send = ""
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", MINIFLUX_TEXT, FINAL_TEXT],
                  ids=["m0", MINIFLUX_ID, FINAL_ID])
        self.assertEqual(self.sent_count(), 2)
        self.assertLess(" || ".join(self.sent_texts()).index(MINIFLUX_TEXT[:20]),
                        " || ".join(self.sent_texts()).index(FINAL_TEXT[:20]))

    def test_capped_drain_defers_the_status_push_until_order_restored(self):
        """Boundary-lost recovery on a working->idle poll: the capped batch
        takes the transcript tail out of the first drain, so the same-poll
        status push must defer — the final may never jump ahead of the
        intermediates the drain has not delivered yet."""
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["baseline"], ids=["m0"])
        new = os.path.join(self.base, "w2.jsonl")
        backlog = [f"message number {i} of the backlog" for i in range(1, 13)]
        entry = agent(MAIN, self.home, status="idle",
                      session={"kind": "path",
                               "value": transcript(new, "new session", backlog)})
        self.run_once(entry, {MAIN: "working"}, self.config())   # working->idle
        self.assertEqual(self.sent_count(), watch.LOST_BOUNDARY_MAX_PER_POLL)
        first = " || ".join(self.sent_texts())
        self.assertIn("message number 1 of the backlog", first)  # oldest first
        self.assertNotIn("message number 12 of the backlog", first)  # tail held back
        self.assert_log("deferred for ordered retry")
        self.run_once(entry, {MAIN: "idle"}, self.config())      # steady idle
        self.assertEqual(self.sent_count(), len(backlog))        # fully drained
        texts = self.sent_texts()
        self.assertIn("message number 12 of the backlog", texts[-1])  # final last

    def test_send_failure_stops_the_sequence_and_retries_in_order(self):
        cfg = self.config()
        session = self.poll(status="idle", prev={MAIN: "idle"},
                            replies=["baseline"], ids=["m0"], cfg=cfg)
        self.api.fail_send = "500 boom"
        # two pending; the FIRST fails: the second must not jump the queue
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", MINIFLUX_TEXT, FINAL_TEXT],
                  ids=["m0", MINIFLUX_ID, FINAL_ID], cfg=cfg)
        self.assertEqual(self.sent_count(), 0)
        self.assertEqual(self.content_last(), "m0")             # boundary unmoved
        self.assert_log("not delivered — will retry next poll")
        self.api.fail_send = ""
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", MINIFLUX_TEXT, FINAL_TEXT],
                  ids=["m0", MINIFLUX_ID, FINAL_ID], cfg=cfg)
        self.assertEqual(self.sent_count(), 2)
        self.assertIn(notify.content_key(MAIN, MINIFLUX_ID), self.marker())
        self.assertIn(notify.content_key(MAIN, FINAL_ID), self.marker())

    def test_final_already_relayed_is_never_resent_by_the_status_path(self):
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["baseline"], ids=["m0"])
        # the final lands while the pane still reads working, then goes idle:
        # the mid-run pass delivers it as the tail-hold release before the
        # status diff could, and the status path must stay quiet
        session = self.poll(status="idle", prev={MAIN: "idle"},
                            replies=["baseline", MINIFLUX_TEXT],
                            ids=["m0", MINIFLUX_ID])
        self.assertEqual(self.sent_count(), 1)
        entry = agent(MAIN, self.home, status="idle",
                      session={"kind": "path", "value": session})
        tg.write_state("last_status.json", {MAIN: "working"})
        write_config(self.config())
        watch.poll_once(self.config())
        self.assertEqual(self.sent_count(), 1)                  # no re-send
        self.assert_log("final message already pushed")

    def test_boundary_lost_drains_backlog_oldest_first_across_polls(self):
        """A boundary identity that fell out of the transcript with no
        parseable timestamp (hash-only identities, session rolled over):
        dedupe by the sent-marker alone, draining the never-delivered backlog
        oldest-first at a per-poll cap — flood-safe, in order, and nothing
        dropped for being old."""
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline"], ids=["m0"])
        new = os.path.join(self.base, "w2.jsonl")
        backlog = [f"message number {i} of the backlog" for i in range(1, 13)]
        entry = agent(MAIN, self.home, status="idle",
                      session={"kind": "path",
                               "value": transcript(new, "new session", backlog)})
        self.run_once(entry, {MAIN: "idle"}, self.config())
        capped = watch.LOST_BOUNDARY_MAX_PER_POLL
        self.assertEqual(self.sent_count(), capped)
        first = " || ".join(self.sent_texts())
        self.assertIn("message number 1 of the backlog", first)  # oldest first
        self.assertNotIn("message number 9 of the backlog", first)
        self.run_once(entry, {MAIN: "idle"}, self.config())
        self.assertEqual(self.sent_count(), len(backlog))        # fully drained
        joined = " || ".join(self.sent_texts())
        self.assertIn("message number 12 of the backlog", joined)
        self.assertLess(joined.index("message number 1 of the backlog"),
                        joined.index("message number 12 of the backlog"))
        self.run_once(entry, {MAIN: "idle"}, self.config())
        self.assertEqual(self.sent_count(), len(backlog))        # never twice
        with open(new, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "message", "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "one more after the cap"}]}}) + "\n")
        self.run_once(entry, {MAIN: "idle"}, self.config())
        self.assertEqual(self.sent_count(), len(backlog) + 1)


class TestPostureAndPriming(MidrunCase):
    def test_firstmate_off_keeps_the_single_slot_behavior(self):
        """Fork posture: with [firstmate] enabled = false the mid-run relay
        must not exist — the upstream single-slot content pass delivers only
        the last message of a multi-message turn, exactly as before."""
        cfg = {"chat_id": str(CHAT), "bot_token": "T0K:EN", "replies": False,
               "settle_ms": 0, "quiet_seconds": 0, "statuses": ["done"]}
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["baseline"], ids=["m0"], cfg=cfg)
        self.poll(status="idle", prev={MAIN: "working"},
                  replies=["baseline", MINIFLUX_TEXT, FINAL_TEXT],
                  ids=["m0", MINIFLUX_ID, FINAL_ID], cfg=cfg)
        # upstream behavior: one push, the final message only
        self.assertEqual(self.sent_count(), 1)
        self.assertTrue(any(FINAL_TEXT in t for t in self.sent_texts()))
        self.assertFalse(any(MINIFLUX_TEXT in t for t in self.sent_texts()))

    def test_worker_panes_never_relay_through_midrun(self):
        main_session = os.path.join(self.base, "main.jsonl")
        worker_session = os.path.join(self.base, "worker.jsonl")
        transcript(main_session, "run the fleet", ["main baseline"], ids=["mm"])
        transcript(worker_session, "worker task", ["worker said"])
        worker = agent(WORKER, f"{self.home}-treehouse/w1", cli="pi",
                       status="idle", title="π - worker",
                       session={"kind": "path", "value": worker_session})
        main = agent(MAIN, self.home, status="working",
                     session={"kind": "path", "value": main_session})
        herdr = FakeHerdr([main, worker])
        tg.herdr_run = herdr.run
        tg.herdr_json = herdr.json
        tg.write_state("last_status.json", {WORKER: "idle", MAIN: "working"})
        write_config(self.config())
        watch.poll_once(self.config())                          # primes baselines
        self.assertEqual(self.sent_count(), 0)
        transcript(worker_session, "worker task",
                   ["worker's newer message", "worker's newest"], ids=["w1", "w2"])
        watch.poll_once(self.config())
        self.assertEqual(self.sent_count(), 0)                  # main-only relay
        self.assertEqual(tg.read_state(tg.MSG_MAP, {}), {})
        self.assert_log(f"content: pane={WORKER} is not the firstmate main pane")

    def test_fresh_install_primes_without_announcing_history(self):
        replies = [MINIFLUX_TEXT, FINAL_TEXT]
        ids = [MINIFLUX_ID, FINAL_ID]
        self.poll(status="idle", prev={}, prime=True, replies=replies, ids=ids)
        self.assertEqual(self.sent_count(), 0)
        self.assertEqual(self.content_last(), FINAL_ID)
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=[*replies, "a brand new one"], ids=[*ids, "m9@9"])
        self.assertEqual(self.sent_count(), 1)
        self.assertTrue(any("a brand new one" in t for t in self.sent_texts()))

    def test_restart_never_primes_away_an_undelivered_message(self):
        """The 2026-09-25 swallow rule under the new path: a watcher death
        mid-turn leaves interiors undelivered; the recovery restart delivers
        them rather than recording them as known."""
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline"], ids=["m0"])
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", MINIFLUX_TEXT], ids=["m0", MINIFLUX_ID])
        self.assertEqual(self.sent_count(), 1)
        # the watcher dies; a whole turn completes unseen, then a restart
        session = self.session_file(
            ["baseline", MINIFLUX_TEXT, TURNSTILE_TEXT, FINAL_TEXT],
            ids=["m0", MINIFLUX_ID, TURNSTILE_ID, FINAL_ID])
        tg.write_state(watch.CONTENT_LAST, {MAIN: MINIFLUX_ID})  # frozen mid-turn
        self.poll(status="idle", prev={MAIN: "idle"}, prime=True, session=session)
        self.assertEqual(self.sent_count(), 3)                  # everything after
        texts = " || ".join(self.sent_texts())
        for text in (TURNSTILE_TEXT, FINAL_TEXT):
            self.assertIn(text, texts)

    def test_blocked_pane_leaves_messages_to_the_status_diff(self):
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["baseline"], ids=["m0"])
        # blocked: the status diff speaks (its push carries the pane's last
        # reply, upstream behavior — body-only under firstmate framing); the
        # mid-run pass holds off entirely
        self.poll(status="blocked", prev={MAIN: "working"},
                  replies=["baseline", MINIFLUX_TEXT], ids=["m0", MINIFLUX_ID])
        self.assertEqual(self.sent_count(), 1)                  # the blocked push only
        self.assertTrue(any(MINIFLUX_TEXT in t for t in self.sent_texts()))
        self.assertIn(notify.content_key(MAIN, MINIFLUX_ID), self.marker())
        self.assertEqual(self.content_last(), "m0")             # pass deferred
        self.assert_log(f"{MINIFLUX_ID[:24]} left for the status diff")
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", MINIFLUX_TEXT, "one after"],
                  ids=["m0", MINIFLUX_ID, "after@1791595800000"])
        self.assertEqual(self.sent_count(), 2)                  # only the new one
        self.assertTrue(any("one after" in t for t in self.sent_texts()))


class TestLeakSafety(MidrunCase):
    def session_raw(self, lines):
        path = os.path.join(self.base, "raw.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")
        return path

    def test_thinking_and_toolcall_records_never_relay(self):
        """pi's real shape (tonight's session): assistant records whose content
        is thinking/toolCall carry no text block and must never reach the phone."""
        head = [
            {"type": "message", "message": {"role": "user",
             "content": [{"type": "text", "text": "go"}]}},
            {"type": "message", "id": "m0", "timestamp": 1791594000000,
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "warm baseline"}] }},
            {"type": "message", "id": "t1", "timestamp": 1791595000000,
             "message": {"role": "assistant",
                         "content": [{"type": "thinking", "thinking": "hmm"}] }},
            {"type": "message", "id": "t2", "timestamp": 1791595010000,
             "message": {"role": "assistant",
                         "content": [{"type": "toolCall", "toolCall": "bash"}] }},
        ]
        path = self.session_raw(head)                           # baseline only
        self.poll(status="idle", prev={MAIN: "idle"}, session=path, replies=[])
        self.assertEqual(self.sent_count(), 0)                  # baseline primed
        tail = {"type": "message", "id": MINIFLUX_ID.split("@")[0],
                "timestamp": 1791595503210,
                "message": {"role": "assistant",
                            "content": [{"type": "thinking", "thinking": "plan"},
                                        {"type": "text", "text": MINIFLUX_TEXT}] }}
        path = self.session_raw(head + [tail])
        self.poll(status="idle", prev={MAIN: "idle"}, session=path, replies=[])
        self.assertEqual(self.sent_count(), 1)                  # the text block only
        self.assertTrue(any(MINIFLUX_TEXT in t for t in self.sent_texts()))
        self.assertFalse(any("hmm" in t or "plan" in t or "bash" in t
                             for t in self.sent_texts()))
        self.assertIn(notify.content_key(MAIN, MINIFLUX_ID), self.marker())

    def test_partial_trailing_line_is_ignored_until_complete(self):
        path = os.path.join(self.base, "partial.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "message", "message": {"role": "user",
                    "content": [{"type": "text", "text": "go"}]}}) + "\n")
            f.write('{"type":"message","id":"half","timestamp":1791595000000,'
                    '"message":{"role":"assistant","content":[{"type":"tex')  # mid-write
        self.poll(status="idle", prev={MAIN: "idle"}, session=path, replies=[])
        self.assertEqual(self.sent_count(), 0)


class TestCalm(MidrunCase):
    def calm_config(self):
        cfg = self.config()
        make_home(self.base, calm="on")                          # config/calm = on
        return cfg

    def test_calm_drops_short_intermediate_narration(self):
        cfg = self.calm_config()
        substantive = "Operator, the substantive block:\n" + (MINIFLUX_TEXT + " ") * 2
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["baseline"], ids=["m0"], cfg=cfg)
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["baseline", "working on it", substantive, FINAL_TEXT],
                  ids=["m0", "n1@1", MINIFLUX_ID, FINAL_ID], cfg=cfg)
        # narration dropped, substantive interior relayed, tail held
        self.assertEqual(self.sent_count(), 1)
        self.assertTrue(any(substantive.rstrip() in t for t in self.sent_texts()))
        self.assertFalse(any("working on it" in t for t in self.sent_texts()))
        self.assert_log("calm drops")

    def test_calm_never_drops_the_final(self):
        cfg = self.calm_config()
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline"], ids=["m0"], cfg=cfg)
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", "done: short"], ids=["m0", "f1@1"], cfg=cfg)
        self.assertEqual(self.sent_count(), 1)
        self.assertTrue(any("done: short" in t for t in self.sent_texts()))


class TestSpeech(MidrunCase):
    """Speech rides the real maybe_speak/_speak logic (voice mode on, nothing
    unavailable, synthesis stubbed) so [tts] speak_final_only is exercised as
    shipped, not re-implemented in the stub."""

    def speech_on(self, cfg, **tts_opts):
        cfg["tts"] = {"enabled": True, "api_key": "sk-x", **tts_opts}
        self.spoken.clear()
        self.synth = []
        mock.patch.object(tts, "voice_mode", return_value=True).start()
        mock.patch.object(tts, "unavailability", return_value="").start()
        mock.patch.object(
            tts, "synthesize",
            side_effect=lambda cfg, text: (self.synth.append(text), (b"", "ogg", ""))[1]).start()
        mock.patch.object(tts, "to_ogg", side_effect=lambda cfg, data, fmt: ("/dev/null", "")).start()
        mock.patch.object(tts, "voice_duration", return_value=1.0).start()
        self.voices = []
        mock.patch.object(tg, "send_voice",
                          side_effect=lambda cfg, path, duration=0:
                              (self.voices.append(path), ({"message_id": 99}, ""))[1]).start()
        self.addCleanup(mock.patch.stopall)

    def test_speak_final_only_false_speaks_intermediates(self):
        cfg = self.config()
        self.speak_patcher.stop()                            # the real path runs
        self.speech_on(cfg, speak_final_only=False)
        self.assertFalse(tts.speak_final_only(cfg))
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["baseline"], ids=["m0"], cfg=cfg)
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", MINIFLUX_TEXT, FINAL_TEXT],
                  ids=["m0", MINIFLUX_ID, FINAL_ID], cfg=cfg)
        self.assertEqual(self.sent_count(), 2)
        self.assertEqual(self.synth, [MINIFLUX_TEXT, FINAL_TEXT])   # both speak
        self.assertEqual(len(self.voices), 2)                   # two voice notes

    def test_speak_final_only_keeps_midrun_text_only(self):
        cfg = self.config()
        self.speak_patcher.stop()
        self.speech_on(cfg, speak_final_only=True)
        self.assertTrue(tts.speak_final_only(cfg))
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["baseline"], ids=["m0"], cfg=cfg)
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["baseline", MINIFLUX_TEXT, FINAL_TEXT],
                  ids=["m0", MINIFLUX_ID, FINAL_ID], cfg=cfg)
        # the interior relayed as text; the tail held; nothing spoken yet
        self.assertEqual(self.sent_count(), 1)
        self.assertEqual(self.synth, [])
        self.assert_log("speak_final_only — mid-run message stays text-only")
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", MINIFLUX_TEXT, FINAL_TEXT],
                  ids=["m0", MINIFLUX_ID, FINAL_ID], cfg=cfg)
        self.assertEqual(self.sent_count(), 2)
        self.assertEqual(self.synth, [FINAL_TEXT])              # the final spoke
        self.assertEqual(self.voices, ["/dev/null"])            # one voice note

    def test_default_speaks_midrun_and_final_messages(self):
        cfg = self.config()
        self.speak_patcher.stop()
        self.speech_on(cfg)
        self.assertFalse(tts.speak_final_only(cfg))          # the shipped default
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["baseline"], ids=["m0"], cfg=cfg)
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["baseline", MINIFLUX_TEXT, FINAL_TEXT],
                  ids=["m0", MINIFLUX_ID, FINAL_ID], cfg=cfg)
        # The interior is relayed and spoken; the tail is held until completion.
        self.assertEqual(self.sent_count(), 1)
        self.assertEqual(self.synth, [MINIFLUX_TEXT])
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", MINIFLUX_TEXT, FINAL_TEXT],
                  ids=["m0", MINIFLUX_ID, FINAL_ID], cfg=cfg)
        self.assertEqual(self.sent_count(), 2)
        self.assertEqual(self.synth, [MINIFLUX_TEXT, FINAL_TEXT])
        self.assertEqual(self.voices, ["/dev/null", "/dev/null"])

    def test_deliver_passes_the_intermediate_flag(self):
        """The flag itself: notify.deliver hands maybe_speak intermediate=True
        for a mid-run message and False for the transcript tail."""
        self.poll(status="working", prev={MAIN: "working"},
                  replies=["baseline"], ids=["m0"])
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", MINIFLUX_TEXT, FINAL_TEXT],
                  ids=["m0", MINIFLUX_ID, FINAL_ID])
        self.assertEqual([flag for _, flag in self.spoken], [True, False])


class TestEnumeration(MidrunCase):
    def test_identity_time_parses_epoch_ms_and_iso(self):
        self.assertAlmostEqual(notify.identity_time("m@1791595503210"), 1791595503.210,
                               places=3)
        stamp = notify.identity_time("m@2026-10-09T20:25:03.210Z")
        self.assertIsNotNone(stamp)
        self.assertIsNone(notify.identity_time("m@notatime"))
        self.assertIsNone(notify.identity_time("hashonly"))

    def test_session_rollover_orders_by_timestamp(self):
        """The boundary sits in a previous session file: its @ts still orders
        the new session's messages (the cwd-keyed resolution rolls over)."""
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline"], ids=["m0@1791590000000"])
        # a brand new session file replaces the resolution target
        new = os.path.join(self.base, "w2.jsonl")
        transcript(new, "new session", [MINIFLUX_TEXT, FINAL_TEXT],
                   ids=[MINIFLUX_ID, FINAL_ID])
        entry = agent(MAIN, self.home, status="idle",
                      session={"kind": "path", "value": new})
        self.run_once(entry, {MAIN: "idle"}, self.config())
        self.assertEqual(self.sent_count(), 2)
        self.assertIn(notify.content_key(MAIN, MINIFLUX_ID), self.marker())

    def test_codex_main_pane_stays_on_the_native_completed_path(self):
        cfg = self.config()
        session = self.session_file(["codex's reply"], ids=["c1"])
        entry = agent(MAIN, self.home, cli="codex", status="idle",
                      session={"kind": "path", "value": session})
        with mock.patch.object(watch, "content_pass", wraps=watch.content_pass) as cp, \
             mock.patch.object(watch, "midrun_pass", wraps=watch.midrun_pass) as mp:
            self.run_once(entry, {MAIN: "idle"}, cfg)
        self.assertTrue(cp.called)                               # the codex branch
        self.assertFalse(mp.called)                              # never midrun

    def test_codex_never_enumerates_a_raw_rollout(self):
        """Posture: the codex main pane delivers through paired native records
        (completed_codex_pass), so the list reader must hand codex back None —
        no raw rollout jsonl enumeration, whatever session ref resolves."""
        raw = self.session_file(["a raw rollout line"])
        self.assertIsNone(notify.relay_messages(
            "codex", {"kind": "path", "value": raw}, self.home, MAIN,
            cfg=self.config()))

    def test_cli_without_a_list_reader_falls_back_to_single_slot(self):
        """A grok main pane (no jsonl list reader): the pre-midrun single-slot
        behavior must keep serving it rather than losing content delivery."""
        cfg = self.config()
        grok_dir = os.path.join(self.base, "grok", "s1")
        os.makedirs(grok_dir)
        hist = os.path.join(grok_dir, "chat_history.jsonl")
        with open(hist, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "content": "go"}) + "\n")
            f.write(json.dumps({"type": "assistant", "id": "g1",
                                "content": "grok's answer"}) + "\n")
        entry = agent(MAIN, self.home, cli="grok", status="idle", session=None)
        with mock.patch.object(notify, "grok_session_dir", return_value=grok_dir):
            self.run_once(entry, {MAIN: "idle"}, cfg)           # baseline primed
            self.assertEqual(self.sent_count(), 0)
            with open(hist, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "assistant", "id": "g2",
                                    "content": "grok's second"}) + "\n")
            self.run_once(entry, {MAIN: "idle"}, cfg)
        self.assertEqual(self.sent_count(), 1)
        self.assertTrue(any("grok's second" in t for t in self.sent_texts()))


class TestNoopSuppression(MidrunCase):
    """suppress_noop_ack through the watcher: the routine no-op
    acknowledgement must not reach the phone — no text, no voice — and must
    not wedge the relay either: the stamped sent-marker settles the delivery
    boundary after one pass instead of retrying the suppressed message on
    every poll, and later substantive messages still deliver."""

    def cfg(self):
        cfg = self.config()
        cfg["suppress_noop_ack"] = True
        return cfg

    def suppressed_lines(self):
        return [ln for ln in self.log_since()
                if "no-op acknowledgement suppressed" in ln]

    def test_ack_suppressed_once_then_settled_and_later_news_delivers(self):
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline"], ids=["m0"], cfg=self.cfg())
        self.assertEqual(self.sent_count(), 0)                  # baseline primed
        # the ack lands as the pane's only new message: suppressed, not sent
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", NOOP_ACK], ids=["m0", NOOP_ACK_ID],
                  cfg=self.cfg())
        self.assertEqual(self.sent_count(), 0)                  # nothing sent
        self.assertEqual(self.spoken, [])                       # nothing spoken
        self.assertIn(notify.content_key(MAIN, NOOP_ACK_ID), self.marker())
        self.assertEqual(len(self.suppressed_lines()), 1)       # logged once
        # the next poll must not retry it: no second suppression line, no send
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", NOOP_ACK], ids=["m0", NOOP_ACK_ID],
                  cfg=self.cfg())
        self.assertEqual(self.sent_count(), 0)
        self.assertEqual(len(self.suppressed_lines()), 1)
        # and a later substantive message still delivers — the boundary moved
        # past the suppressed ack, so the relay is not wedged
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", NOOP_ACK, TURNSTILE_TEXT],
                  ids=["m0", NOOP_ACK_ID, TURNSTILE_ID], cfg=self.cfg())
        self.assertEqual(self.sent_count(), 1)
        self.assertIn(TURNSTILE_TEXT, self.sent_texts()[0])

    def test_ack_still_relays_when_the_key_is_off(self):
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline"], ids=["m0"])            # default config
        self.poll(status="idle", prev={MAIN: "idle"},
                  replies=["baseline", NOOP_ACK], ids=["m0", NOOP_ACK_ID])
        self.assertEqual(self.sent_count(), 1)
        self.assertIn(NOOP_ACK, self.sent_texts()[0])


if __name__ == "__main__":
    unittest.main()
