"""Offline framing tests: every push must read as a fresh standalone message.

The operator's complaint, reproduced from the screenshot: each notification carried
the session's OPENING user line in its header (display_title's first_prompt
fallback — frozen at session start, repeated on every push of the conversation)
and wrapped the body in an expandable blockquote, which Telegram renders like a
reply quote. Together: "every message appears framed as a reply to the first
message of the session". Firstmate mode drops both — no session-opener text in
the header, no fold by default — while enabled = false keeps upstream behavior
exactly (opener fallback, fold = auto). Main-pane pushes go one step further and
carry no header line at all ("✅ pi · cwd" names the only prompt-replyable pane
— and, under firstmate mode, the only pane that pushes at all;
[firstmate] main_header = true restores it). The reply's excerpt source is
pinned to the pane's LATEST final assistant message, and inbound reply routing
is asserted untouched end to end.
herdr and Telegram are stubbed at the tg module boundary.
"""

import json
import os
import re
import tempfile
import unittest

import _env  # noqa: F401  (env must be set before tg/notify imports)
import notify
import tg
from _env import (CHAT, MAIN, USER, WORKER, FakeApi, FakeHerdr, agent, make_home,
                  reset_state, transcript)

OPENER = "hi I upped the max ram to 6gb again"          # the session's first message
R1 = "first turn shipped\nwith a second line"           # ...so it folds upstream
R2 = "second turn shipped\nalso two lines"
U2 = "and now for something else"
U3 = "one more question after the last answer"

QUOTE = re.compile(r"<blockquote[^>]*>(.*?)</blockquote>", re.S)


def blockquotes(text: str) -> list:
    return QUOTE.findall(text)


class FramingCase(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.base = tempfile.mkdtemp(prefix="fm-frame-")
        self.home = make_home(self.base)
        self.session = os.path.join(self.base, "s.jsonl")
        self.api = FakeApi()
        tg.api = self.api
        self.addCleanup(setattr, tg, "api", _env.REAL_API)
        self.herdr = None

    def fleet(self, panes):
        self.herdr = FakeHerdr(panes)
        tg.herdr_run = self.herdr.run
        tg.herdr_json = self.herdr.json
        self.addCleanup(setattr, tg, "herdr_run", _env.REAL_HERDR_RUN)
        self.addCleanup(setattr, tg, "herdr_json", _env.REAL_HERDR_JSON)
        return self.herdr

    def main_pane(self):
        return agent(MAIN, self.home, status="done", title="π - cai",
                     session={"kind": "path", "value": self.session})

    def cfg(self, firstmate=True, **extra):
        cfg = {"chat_id": str(CHAT), "bot_token": "T0K:EN", "replies": False,
               "allowed_user_ids": [USER], "settle_ms": 0, "quiet_seconds": 0,
               "statuses": ["done", "blocked"]}
        if firstmate:
            cfg["firstmate"] = {"enabled": True, "main_cwd": self.home}
        cfg.update(extra)
        return cfg

    def push(self, cfg=None, pane=None, cli="pi", title="π - cai"):
        cfg = cfg or self.cfg()
        pane = pane or self.main_pane()
        self.fleet([pane])
        return notify.deliver(cfg, pane["pane_id"], "done", "working",
                              agent=cli, cli=cli, title=title)

    def turn(self, reply: str, mid: str, user_line: str = U2):
        """Grow the session transcript one turn: a user line, then an assistant reply."""
        with open(self.session, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "message", "message": {"role": "user",
                    "content": [{"type": "text", "text": user_line}]}}) + "\n")
            f.write(json.dumps({"type": "message",
                    "message": {"role": "assistant", "id": mid,
                                "content": [{"type": "text", "text": reply}]}}) + "\n")


class TestStandaloneFraming(FramingCase):
    """Firstmate mode: the screenshot scenario, turn by turn."""

    def setUp(self):
        super().setUp()
        transcript(self.session, OPENER, [R1], ids=["m1"])   # opened with OPENER

    def test_push_carries_no_session_opener_and_no_fold(self):
        self.push()
        self.assertEqual(len(self.api.sent), 1)
        text = self.api.sent[0]["text"]
        self.assertIn(R1.split("\n")[0], text)          # the pane's latest reply
        self.assertNotIn(OPENER, text)                  # never the session opener
        self.assertEqual(blockquotes(text), [])         # no reply-quote-styled fold

    def test_second_push_is_fresh_not_a_reply_to_the_first(self):
        """No part of a later push — header or body — may carry text from a
        prior notification or the session opener: each push stands alone."""
        self.push()
        self.turn(R2, "m2")
        self.assertTrue(self.push())
        self.assertEqual(len(self.api.sent), 2)
        text = self.api.sent[1]["text"]
        self.assertIn(R2.split("\n")[0], text)
        for stale in (OPENER, R1, U2):
            self.assertNotIn(stale, text)
        self.assertEqual(blockquotes(text), [])

    def test_explicit_fold_auto_keeps_quote_but_only_this_pushs_content(self):
        """The 'no blockquote contains text from a prior notification' invariant,
        where blockquotes exist at all: with fold forced back on, the quote wraps
        only this notification's own rendered reply."""
        self.push(cfg=self.cfg(fold="auto"))
        self.turn(R2, "m2")
        self.push(cfg=self.cfg(fold="auto"))
        self.assertEqual(len(self.api.sent), 2)
        quotes = blockquotes(self.api.sent[1]["text"])
        self.assertTrue(quotes)                         # the explicit fold is honored
        for quote in quotes:
            self.assertIn("second turn shipped", quote)  # this push's own content
            for stale in (OPENER, R1, U2):
                self.assertNotIn(stale, quote)

    def test_worker_push_is_refused_entirely(self):
        """Framing is moot for workers under firstmate mode: outbound relay is
        main-pane only, so a worker event sends nothing at all (see
        test_notify.TestMainOnlyRelay for the full matrix)."""
        worker = os.path.join(self.base, "w.jsonl")
        transcript(worker, "please build the fork", [R2], ids=["m1"])
        home = make_home(self.base, tasks=[
            ("bridge-framing", WORKER, "ship", ["done [at=1790000100]: PR opened"])])
        pane = agent(WORKER, f"{home}-treehouse/w1", cli="pi", status="done",
                     title="OC | build the fork", session={"kind": "path", "value": worker})
        self.push(cfg=self.cfg(firstmate=True), pane=pane, cli="pi", title="OC | x")
        self.assertEqual(self.api.sent, [])
        self.assertEqual(tg.read_state(tg.MSG_MAP, {}), {})

    def test_reply_to_a_push_still_routes_to_the_pane(self):
        """Inbound routing and the message-id map are untouched by framing: a
        reply to the pushed notification still reaches the main pane, unprefixed."""
        import bot
        self.push()
        entries = tg.read_state(tg.MSG_MAP, {})
        self.assertEqual(len(entries), 1)
        msg_id, entry = next(iter(entries.items()))
        self.assertEqual(entry["pane"], MAIN)
        self.assertNotIn("worker_pane", entry)
        bot.handle_message(self.cfg(), {"chat": {"id": CHAT}, "from": {"id": USER},
                                        "message_id": 10, "text": "thanks",
                                        "reply_to_message": {"message_id": int(msg_id)}})
        self.assertEqual(self.herdr.prompts(), [(MAIN, "thanks")])


class TestMainPaneHeaderless(FramingCase):
    """Firstmate mode, main pane: the push is body-only — "✅ pi · cwd" names the
    one prompt-replyable pane, so it is prefix noise; the final reply is the whole
    message. main_header = true restores the header line (and under firstmate mode
    the main pane is the only pane that pushes at all); with firstmate disabled
    the full header always stays."""

    def setUp(self):
        super().setUp()
        transcript(self.session, OPENER, [R1], ids=["m1"])

    def fm_cfg(self, **fm):
        cfg = self.cfg()
        cfg["firstmate"].update(fm)
        return cfg

    def test_main_push_renders_body_only(self):
        self.push()
        self.assertEqual(len(self.api.sent), 1)
        text = self.api.sent[0]["text"]
        self.assertTrue(text.startswith(R1.split("\n")[0]))   # no header, no blank line
        self.assertNotIn("·", text.split("\n")[0])           # no "✅ pi · cwd · title"
        self.assertNotIn("✅", text)
        self.assertNotIn(self.home, text)                      # no folder segment
        self.assertNotIn("π", text)                            # no banner title either
        entry = next(iter(tg.read_state(tg.MSG_MAP, {}).values()))
        self.assertEqual(entry["pane"], MAIN)                  # routing needs no header

    def test_main_header_true_restores_the_header_line(self):
        self.push(cfg=self.fm_cfg(main_header=True))
        text = self.api.sent[0]["text"]
        first = text.split("\n")[0]
        self.assertIn("✅", first)
        self.assertIn("·", first)                              # the "✅ agent · folder" shape
        self.assertIn(R1.split("\n")[0], text)                 # body intact below it

    def test_worker_push_is_never_sent(self):
        # under firstmate mode a worker pane has no push to frame: relay is
        # main-pane only (full matrix in test_notify.TestMainOnlyRelay)
        worker = os.path.join(self.base, "w.jsonl")
        transcript(worker, "please build the fork", [R2], ids=["m1"])
        home = make_home(self.base, tasks=[
            ("bridge-framing", WORKER, "ship", ["done [at=1790000100]: PR opened"])])
        pane = agent(WORKER, f"{home}-treehouse/w1", cli="pi", status="done",
                     title="OC | build the fork", session={"kind": "path", "value": worker})
        self.push(cfg=self.cfg(firstmate=True), pane=pane, cli="pi", title="OC | x")
        self.assertEqual(self.api.sent, [])

    def test_upstream_keeps_the_full_header(self):
        self.push(cfg=self.cfg(firstmate=False))
        first = self.api.sent[0]["text"].split("\n")[0]
        self.assertIn("✅", first)
        self.assertIn("·", first)

    def test_bodiless_main_push_still_sends_a_status_line(self):
        # no transcript resolves: reply "" — an empty text would not send, so the
        # header line stays and the push still reaches the phone
        pane = agent(MAIN, self.home, status="done", title="π - cai")
        self.push(pane=pane)
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("✅", self.api.sent[0]["text"])

    def test_chunked_body_only_push_has_no_leading_blank_lines(self):
        long_reply = R1 + "\n" + ("word " * 1200)            # > CHUNK_CHARS: splits
        session = os.path.join(self.base, "long.jsonl")
        transcript(session, OPENER, [long_reply], ids=["m1"])
        pane = agent(MAIN, self.home, status="done", title="π - cai",
                     session={"kind": "path", "value": session})
        self.push(pane=pane)
        self.assertGreater(len(self.api.sent), 1)
        self.assertTrue(self.api.sent[0]["text"].startswith(R1.split("\n")[0]))
        for params in self.api.sent:
            self.assertFalse(params["text"].startswith("\n"))
        self.assertTrue(self.api.sent[1]["text"].startswith("⋯ 2/"))  # pagination stays


class TestUpstreamFramingUnchanged(FramingCase):
    """enabled = false must behave exactly like upstream: the opening-prompt
    fallback and the auto fold both stay — the divergence guard for this seam."""

    def test_opener_fallback_and_auto_fold_survive_upstream(self):
        transcript(self.session, OPENER, [R1], ids=["m1"])
        self.push(cfg=self.cfg(firstmate=False))
        text = self.api.sent[0]["text"]
        self.assertIn(OPENER, text)                     # upstream still shows it
        quotes = blockquotes(text)
        self.assertEqual(len(quotes), 1)                # upstream still folds
        self.assertIn(R1.split("\n")[0], quotes[0])


class TestExcerptResolution(FramingCase):
    """The body's excerpt source is pinned: always the pane's LATEST final
    assistant message — never a user line, never an earlier assistant reply."""

    def seed(self):
        with open(self.session, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "message", "message": {"role": "user",
                    "content": [{"type": "text", "text": OPENER}]}}) + "\n")
            f.write(json.dumps({"type": "message", "message": {"role": "assistant",
                    "id": "m1", "content": [{"type": "text", "text": R1}]}}) + "\n")
            f.write(json.dumps({"type": "message", "message": {"role": "user",
                    "content": [{"type": "text", "text": U2}]}}) + "\n")
            f.write(json.dumps({"type": "message", "message": {"role": "assistant",
                    "id": "m2", "content": [{"type": "text", "text": R2}]}}) + "\n")
            f.write(json.dumps({"type": "message", "message": {"role": "user",
                    "content": [{"type": "text", "text": U3}]}}) + "\n")
            # a textless assistant entry (a tool-use block) must not win either
            f.write(json.dumps({"type": "message", "message": {"role": "assistant",
                    "id": "m3", "content": [{"type": "tool_use", "name": "bash"}]}}) + "\n")

    def test_reply_identity_picks_the_latest_assistant_message(self):
        self.seed()
        identity, text = notify.reply_identity(
            "pi", {"kind": "path", "value": self.session}, self.home, MAIN, "π - cai")
        self.assertEqual(identity, "m2")                # m3 carries no text: skipped
        self.assertEqual(text, R2)
        self.assertNotEqual(text, R1)
        self.assertNotIn(U3, text)                      # never a user line

    def test_deliver_body_carries_the_latest_assistant_message(self):
        self.seed()
        self.push()
        text = self.api.sent[0]["text"]
        self.assertIn(R2.split("\n")[0], text)
        for stale in (R1.split("\n")[0], U2, U3):
            self.assertNotIn(stale, text)


class TestFoldMode(unittest.TestCase):

    def test_upstream_default_is_auto(self):
        self.assertEqual(notify.fold_mode({}), "auto")
        self.assertEqual(notify.fold_mode({"firstmate": {"enabled": False}}), "auto")
        self.assertEqual(notify.fold_mode({"fold": "auto"}), "auto")

    def test_firstmate_default_is_never(self):
        self.assertEqual(notify.fold_mode({"firstmate": {"enabled": True}}), "never")

    def test_explicit_top_level_key_wins_over_the_mode_default(self):
        fm = {"firstmate": {"enabled": True}}
        self.assertEqual(notify.fold_mode({**fm, "fold": "auto"}), "auto")
        self.assertEqual(notify.fold_mode({"fold": "never"}), "never")

    def test_unknown_value_falls_to_the_mode_default(self):
        self.assertEqual(notify.fold_mode({"fold": "sometimes"}), "auto")
        self.assertEqual(notify.fold_mode({"fold": "sometimes",
                                           "firstmate": {"enabled": True}}), "never")
        self.assertEqual(notify.fold_mode({"fold": "AUTO"}), "auto")


class TestDisplayTitle(unittest.TestCase):

    def info(self, title, cwd="/srv/proj"):
        return {"terminal_title": title, "terminal_title_stripped": title, "cwd": cwd}

    def test_banner_title_is_dropped_under_firstmate_mode(self):
        info = self.info("π - cai")
        # a session with an opener exists, but firstmate mode must not quote it
        path = transcript(os.path.join(tempfile.mkdtemp(prefix="fm-t-"), "s.jsonl"),
                          OPENER, [R1])
        info["agent_session"] = {"kind": "path", "value": path}
        self.assertEqual(notify.display_title(info, "pi", MAIN, firstmate_mode=True), "")

    def test_banner_title_falls_back_to_the_opener_upstream(self):
        path = transcript(os.path.join(tempfile.mkdtemp(prefix="fm-t-"), "s.jsonl"),
                          OPENER, [R1])
        info = self.info("π - cai", cwd=os.path.dirname(path))
        info["agent_session"] = {"kind": "path", "value": path}
        self.assertEqual(notify.display_title(info, "pi", MAIN), OPENER)
        self.assertEqual(notify.display_title(info, "pi", MAIN, firstmate_mode=False),
                         OPENER)

    def test_informative_title_passes_through_in_both_modes(self):
        info = self.info("OC | build the fork")
        for mode in (True, False):
            self.assertEqual(notify.display_title(info, "opencode", WORKER,
                                                  firstmate_mode=mode),
                             "OC | build the fork")


if __name__ == "__main__":
    unittest.main()
