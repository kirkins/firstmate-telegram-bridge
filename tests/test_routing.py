"""Offline routing tests: a fake herdr_run captures argv; a fake Telegram API
captures sends. The invariant under test is the operator's: under firstmate mode no
code path can prompt a pane other than the main pane — worker context rides along
as a prefix, forged or stale reply ids hit the error path, and a plain message
always reaches firstmate, even on brand-new state.
"""

import json
import os
import tempfile
import unittest

import _env  # noqa: F401  (env must be set before tg/bot imports)
import bot
import firstmate
import tg
from _env import (CHAT, FOREIGN, MAIN, USER, WORKER, FakeApi, FakeHerdr, agent,
                  make_home, reset_state)

TASK = ("bridge-fork-build", WORKER, "ship", ["done [at=1790000100]: PR opened"])

WORKER_CTX = f"[opencode/bridge-fork-build pane {WORKER}]"


def msg(text, msg_id=10, reply_to=None, user=USER, chat=CHAT):
    m = {"chat": {"id": chat}, "from": {"id": user}, "message_id": msg_id, "text": text}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return m


def doc_msg(reply_to, msg_id=11):
    return {"chat": {"id": CHAT}, "from": {"id": USER}, "message_id": msg_id,
            "document": {"file_id": "fid", "file_name": "shot.png", "file_size": 100},
            "caption": "the error",
            "reply_to_message": {"message_id": reply_to}}


class RoutingCase(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.base = tempfile.mkdtemp(prefix="fm-route-")
        self.home = make_home(self.base, tasks=[TASK])
        self.agents = [agent(MAIN, self.home, status="working"),
                       agent(WORKER, f"{self.home}-treehouse/w1", cli="opencode",
                             status="working", title="OC | build the fork")]
        self.herdr = FakeHerdr(self.agents)
        self.api = FakeApi()
        tg.herdr_run = self.herdr.run
        tg.herdr_json = self.herdr.json
        tg.api = self.api
        tg.download = lambda cfg, remote, dest: (open(dest, "wb").close(), "")[1]
        self.addCleanup(setattr, tg, "herdr_run", _env.REAL_HERDR_RUN)
        self.addCleanup(setattr, tg, "herdr_json", _env.REAL_HERDR_JSON)
        self.addCleanup(setattr, tg, "api", _env.REAL_API)
        self.addCleanup(setattr, tg, "download", _env.REAL_DOWNLOAD)

    def cfg(self, firstmate=True, **fm):
        config = {"chat_id": str(CHAT), "bot_token": "T0K:EN", "allowed_user_ids": [USER]}
        if firstmate:
            config["firstmate"] = {"enabled": True, "main_cwd": self.home, **fm}
        return config

    def seed_map(self, message_id, entry):
        seen = tg.read_state(tg.MSG_MAP, {})
        seen[str(message_id)] = {**entry, "ts": 1}
        tg.write_state(tg.MSG_MAP, seen)

    def worker_notification(self, message_id=501):
        """A map entry shaped like the pre-main-only worker pushes (and the
        /agents sticky context today): replyable, but only to main."""
        self.seed_map(message_id, {"pane": MAIN, "agent": "firstmate",
                                   "folder": self.home, "context": WORKER_CTX,
                                   "worker_pane": WORKER})


class TestFirstmateMode(RoutingCase):
    def setUp(self):
        super().setUp()
        self.config = self.cfg()

    def test_fresh_start_plain_message_reaches_main(self):
        """Inbox 001: brand-new poller state, no map, no current target — a plain
        message still submits to the firstmate main pane, unprefixed."""
        bot.handle_message(self.config, msg("status check please"))
        prompts = self.herdr.prompts()
        self.assertEqual(prompts, [(MAIN, "status check please")])

    def test_reply_to_worker_notification_routes_to_main_with_prefix(self):
        self.worker_notification()
        bot.handle_message(self.config, msg("rerun the tests", reply_to=501))
        self.assertEqual(self.herdr.prompts(), [(MAIN, f"{WORKER_CTX}\nrerun the tests")])

    def test_reply_to_main_notification_goes_to_main_without_prefix(self):
        self.seed_map(502, {"pane": MAIN, "agent": "pi", "folder": self.home})
        bot.handle_message(self.config, msg("hello", reply_to=502))
        self.assertEqual(self.herdr.prompts(), [(MAIN, "hello")])

    def test_forged_reply_id_hits_error_path(self):
        bot.handle_message(self.config, msg("sneaky", reply_to=987654))
        self.assertEqual(self.herdr.prompts(), [])
        self.assertTrue(any("too old" in t for t in self.api.texts))

    def test_stale_pre_fork_map_entry_still_never_prompts_a_worker(self):
        """A map entry written before firstmate mode existed points straight at the
        worker pane; retarget forces it to main with context naming the worker."""
        self.seed_map(503, {"pane": WORKER, "agent": "opencode", "folder": "~/w"})
        bot.handle_message(self.config, msg("hi", reply_to=503))
        prompts = self.herdr.prompts()
        self.assertEqual(len(prompts), 1)
        pane, text = prompts[0]
        self.assertEqual(pane, MAIN)
        self.assertTrue(text.startswith("[opencode/"))
        self.assertIn(WORKER, text)

    def test_esc_on_worker_refused(self):
        self.worker_notification()
        bot.handle_message(self.config, msg("/esc", reply_to=501))
        self.assertEqual(self.herdr.panes_touched("send-keys"), [])
        self.assertTrue(any("main pane can be interrupted" in t for t in self.api.texts))

    def test_esc_on_main_allowed(self):
        tg.set_current_target({"pane": MAIN, "agent": "pi", "folder": self.home})
        bot.handle_message(self.config, msg("/esc"))
        self.assertEqual(self.herdr.panes_touched("send-keys"), [MAIN])

    def test_esc_fresh_start_targets_main(self):
        bot.handle_message(self.config, msg("/esc"))
        self.assertEqual(self.herdr.panes_touched("send-keys"), [MAIN])

    def test_esc_worker_allowed_with_hatch(self):
        self.worker_notification()
        config = self.cfg(allow_worker_esc=True)
        bot.handle_message(config, msg("/esc", reply_to=501))
        self.assertEqual(self.herdr.panes_touched("send-keys"), [WORKER])

    def test_read_uses_worker_pane(self):
        self.worker_notification()
        bot.handle_message(self.config, msg("/read", reply_to=501))
        reads = [c for c in self.herdr.calls if c[:2] == ("agent", "read")]
        self.assertEqual([r[2] for r in reads], [WORKER])

    def test_read_fresh_start_targets_main(self):
        bot.handle_message(self.config, msg("/read"))
        reads = [c for c in self.herdr.calls if c[:2] == ("agent", "read")]
        self.assertEqual([r[2] for r in reads], [MAIN])

    def test_agents_tap_worker_sets_sticky_context_then_plain_message_carries_it(self):
        query = {"id": "q1", "from": {"id": USER},
                 "message": {"chat": {"id": CHAT}},
                 "data": WORKER}
        bot.handle_callback(self.config, query)
        current = tg.current_target()
        self.assertEqual(current["pane"], MAIN)
        self.assertEqual(current["worker_pane"], WORKER)
        self.assertEqual(current["context"], WORKER_CTX)
        bot.handle_message(self.config, msg("and add tests"))
        self.assertEqual(self.herdr.prompts(), [(MAIN, f"{WORKER_CTX}\nand add tests")])

    def test_agents_tap_main_clears_sticky_context(self):
        self.worker_notification()
        tg.set_current_target(firstmate.retarget(self.config, {"pane": WORKER, "agent": "x"}))
        query = {"id": "q2", "from": {"id": USER},
                 "message": {"chat": {"id": CHAT}},
                 "data": MAIN}
        bot.handle_callback(self.config, query)
        bot.handle_message(self.config, msg("clean message"))
        self.assertEqual(self.herdr.prompts(), [(MAIN, "clean message")])

    def test_attachment_reply_to_worker_goes_to_main_with_prefix(self):
        self.worker_notification()
        bot.handle_message(self.config, doc_msg(reply_to=501))
        prompts = self.herdr.prompts()
        self.assertEqual(len(prompts), 1)
        pane, text = prompts[0]
        self.assertEqual(pane, MAIN)
        self.assertTrue(text.startswith(f"{WORKER_CTX}\n"))
        self.assertIn("shot.png", text)

    def test_empty_allowlist_degrades_to_notify_only(self):
        config = self.cfg()
        config.pop("allowed_user_ids")
        bot.handle_message(config, msg("hello"))
        self.assertEqual(self.herdr.prompts(), [])
        self.assertTrue(any("allowed_user_ids" in t for t in self.api.texts))

    def test_unidentifiable_main_fails_closed(self):
        self.herdr.agents = [agent(WORKER, "/somewhere/else")]
        bot.handle_message(self.config, msg("hello"))
        self.worker_notification()
        bot.handle_message(self.config, msg("hello again", reply_to=501))
        self.assertEqual(self.herdr.prompts(), [])
        self.assertTrue(any("can't identify the firstmate main pane" in t
                            for t in self.api.texts))

    def test_every_prompt_path_lands_on_main_only(self):
        """The invariant, end to end: across every flavor of incoming message, no
        `herdr agent prompt` ever names a non-main pane."""
        self.worker_notification()
        self.seed_map(502, {"pane": MAIN, "agent": "pi", "folder": self.home})
        self.seed_map(503, {"pane": WORKER, "agent": "opencode", "folder": "~/w"})
        for m in (msg("plain"), msg("reply worker", reply_to=501),
                  msg("reply main", reply_to=502), msg("reply stale", reply_to=503),
                  msg("/read", reply_to=501), msg("after everything")):
            bot.handle_message(self.config, m)
        bot.handle_message(self.config, doc_msg(reply_to=501, msg_id=12))
        panes = {pane for pane, _ in self.herdr.prompts()}
        self.assertEqual(panes, {MAIN})


class TestDisabledMode(RoutingCase):
    """enabled = false (or absent) must behave exactly like upstream."""

    def setUp(self):
        super().setUp()
        self.config = self.cfg(firstmate=False)

    def test_plain_message_goes_to_current_target_worker(self):
        tg.set_current_target({"pane": WORKER, "agent": "opencode", "folder": "~/w"})
        bot.handle_message(self.config, msg("hi"))
        self.assertEqual(self.herdr.prompts(), [(WORKER, "hi")])

    def test_reply_routes_to_the_mapped_pane_verbatim(self):
        self.seed_map(501, {"pane": WORKER, "agent": "opencode", "folder": "~/w"})
        bot.handle_message(self.config, msg("hi", reply_to=501))
        self.assertEqual(self.herdr.prompts(), [(WORKER, "hi")])

    def test_esc_and_read_use_target_pane(self):
        tg.set_current_target({"pane": WORKER, "agent": "opencode", "folder": "~/w"})
        bot.handle_message(self.config, msg("/esc"))
        bot.handle_message(self.config, msg("/read"))
        self.assertEqual(self.herdr.panes_touched("send-keys"), [WORKER])
        reads = [c for c in self.herdr.calls if c[:2] == ("agent", "read")]
        self.assertEqual([r[2] for r in reads], [WORKER])

    def test_fresh_start_plain_message_is_upstreams_no_agent_error(self):
        bot.handle_message(self.config, msg("hi"))
        self.assertEqual(self.herdr.prompts(), [])
        self.assertTrue(any("no agent picked yet" in t for t in self.api.texts))

    def test_fm_map_entry_still_lands_on_main_when_mode_disabled(self):
        """A map entry written while firstmate mode was on names the main pane, so
        replies stay safe even if the mode is later switched off."""
        self.worker_notification()
        bot.handle_message(self.config, msg("hi", reply_to=501))
        self.assertEqual(self.herdr.prompts(), [(MAIN, "hi")])   # verbatim, no prefix


class TestUnknownCommandForwarding(RoutingCase):
    """Slash commands the bridge doesn't know reach the agent as ordinary text
    (firstmate default), with the leading slash intact."""

    def setUp(self):
        super().setUp()
        self.config = self.cfg()

    def test_known_commands_still_handled_locally(self):
        bot.handle_message(self.config, msg("/help"))
        bot.handle_message(self.config, msg("/agents"))
        bot.handle_message(self.config, msg("/read"))
        bot.handle_message(self.config, msg("/esc"))
        self.assertEqual(self.herdr.prompts(), [])  # nothing prompted
        self.assertTrue(any("pick an agent" in t for t in self.api.texts))

    def test_unknown_command_forwarded_to_main_untouched(self):
        bot.handle_message(self.config, msg("/afk"))
        self.assertEqual(self.herdr.prompts(), [(MAIN, "/afk")])
        self.assertFalse(any("unknown command" in t for t in self.api.texts))

    def test_forwarded_command_carries_worker_context_and_arguments(self):
        self.worker_notification()
        bot.handle_message(self.config, msg("/quiet 30m", reply_to=501))
        self.assertEqual(self.herdr.prompts(), [(MAIN, f"{WORKER_CTX}\n/quiet 30m")])

    def test_leading_space_slash_still_forwarded(self):
        """A leading space doesn't shield the command (text is stripped first)."""
        bot.handle_message(self.config, msg("  /afk  "))
        self.assertEqual(self.herdr.prompts(), [(MAIN, "/afk")])

    def test_forwarding_off_restores_the_bounce(self):
        config = self.cfg(forward_unknown_commands=False)
        bot.handle_message(config, msg("/afk"))
        self.assertEqual(self.herdr.prompts(), [])
        self.assertTrue(any("unknown command" in t for t in self.api.texts))

    def test_disabled_mode_defaults_to_upstream_bounce(self):
        config = self.cfg(firstmate=False)
        tg.set_current_target({"pane": WORKER, "agent": "opencode", "folder": "~/w"})
        bot.handle_message(config, msg("/afk"))
        self.assertEqual(self.herdr.prompts(), [])
        self.assertTrue(any("unknown command" in t for t in self.api.texts))

    def test_disabled_mode_explicit_forward_reaches_current_target(self):
        config = self.cfg(firstmate=False)
        config["firstmate"] = {"enabled": False, "forward_unknown_commands": True}
        tg.set_current_target({"pane": WORKER, "agent": "opencode", "folder": "~/w"})
        bot.handle_message(config, msg("/afk"))
        self.assertEqual(self.herdr.prompts(), [(WORKER, "/afk")])


if __name__ == "__main__":
    unittest.main()
