"""Omitted native Codex references: exact association and fake-transport delivery."""

import json
import io
import os
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import _env  # noqa: F401  (env must be set before tg/notify/watch imports)
import codex_binding
import notify
import tg
import watch
from _env import (CHAT, MAIN, WORKER, FakeApi, FakeHerdr, agent, make_home,
                  reset_state, transcript, write_config)

CURRENT = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
NEXT = "33333333-3333-3333-3333-333333333333"
BROKEN = "44444444-4444-4444-4444-444444444444"
CRASHED = "55555555-5555-5555-5555-555555555555"
ANSWER = "The synthetic repair is ready."


class ProcessHerdr(FakeHerdr):
    pid = 200

    def json(self, *args, timeout=30):
        if args[:2] == ("pane", "process-info"):
            self.calls.append(args)
            return {"process_info": {"pane_id": args[3], "foreground_processes": [
                {"pid": self.pid, "name": "codex"}]}}, ""
        return super().json(*args, timeout=timeout)


class TestCodexBinding(unittest.TestCase):
    def setUp(self):
        reset_state()
        watch._NOTES.clear()
        temp = tempfile.TemporaryDirectory(prefix="fm-codex-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.home = make_home(str(self.base), tasks=[("worker", WORKER, "ship", [])])
        self.sessions = self.base / ".codex" / "sessions"
        self.proc = self.base / "proc"
        self.releases = self.base / ".codex/packages/app-server-daemon/releases"
        self.mise_installs = self.base / ".local/share/mise/installs/codex"
        for pid, parent, ticks in [(300, 200, "30"), (200, 100, "20"), (100, 1, "10")]:
            self.process(pid, parent, ticks)
        self.pane = agent(MAIN, self.home, cli="codex", title="Old session banner", status="idle")
        self.herdr = ProcessHerdr([self.pane])
        self.api = FakeApi()
        self.cfg = {"chat_id": str(CHAT), "statuses": ["done", "blocked"],
                    "settle_ms": 0, "quiet_seconds": 5,
                    "firstmate": {"enabled": True, "main_cwd": self.home}}
        for target, name, value in [(codex_binding, "SESSIONS", str(self.sessions)),
                                    (codex_binding, "PROC", str(self.proc)),
                                    (codex_binding, "MANAGED_RELEASES", str(self.releases)),
                                    (codex_binding, "MISE_CODEX_INSTALLS", str(self.mise_installs)),
                                    (tg, "herdr_json", self.herdr.json),
                                    (tg, "herdr_run", self.herdr.run), (tg, "api", self.api)]:
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.rollout(CURRENT, ANSWER)
        self.clock = 1800000000.0
        p = patch.object(notify.time, "time", side_effect=lambda: self.clock)
        p.start()
        self.addCleanup(p.stop)

    def process(self, pid, parent, ticks, name=None):
        directory = self.proc / str(pid)
        directory.mkdir(parents=True, exist_ok=True)
        fields = ["S", str(parent)] + ["0"] * 17 + [ticks]
        (directory / "stat").write_text(f"{pid} (codex tool) " + " ".join(fields))
        (directory / "comm").write_text(name or ("codex" if pid in (200, 201) else "python"))

    def rollout(self, sid, text, cwd=None):
        path = self.sessions / "2026" / "01" / "02" / f"rollout-2026-01-02T00-00-00-{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"type": "session_meta", "payload": {"id": sid, "cwd": cwd or self.home}},
            {"type": "response_item", "timestamp": "2026-01-02T00:01:00Z",
             "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                         "content": [{"type": "output_text", "text": text}]}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return str(path)

    def corrupt(self, sid, text):
        """A rollout that exists but cannot be parsed: crash at creation, torn write."""
        path = self.sessions / "2026" / "01" / "04" / f"rollout-2026-01-04T00-00-00-{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return str(path)

    def log_text(self):
        with open(tg.LOG_PATH, encoding="utf-8") as f:
            return f.read()

    def bind(self, sid=CURRENT, pane=None):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": sid}), patch.object(codex_binding.os, "getpid", return_value=300):
            return codex_binding.bind(self.cfg, MAIN, pane or self.pane)

    def managed_chain(self):
        """Real operator topology: tool -> bash -> app-server -> app-server -> init.

        The Herdr foreground Codex (200) is absent from this entire chain.
        Executable, mode and start ticks are synthetic kernel metadata.
        """
        executable = self.releases / "0.162.0-test/bin/codex"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("synthetic executable fixture\n")
        executable.chmod(0o700)
        for pid, parent, name in [(300, 350, "python"), (350, 400, "bash"),
                                  (400, 450, "codex"), (450, 100, "codex")]:
            self.process(pid, parent, str(pid), name=name)
            if name == "codex":
                (self.proc / str(pid) / "exe").symlink_to(executable)
                (self.proc / str(pid) / "cmdline").write_bytes(
                    os.fsencode(executable) + b"\0app-server\0")

    def mise_chain(self):
        """The deployed mise layout, with the same managed app-server topology."""
        self.managed_chain()
        executable = self.mise_installs / "0.162.1/bin/codex"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("synthetic mise Codex fixture\n")
        executable.chmod(0o700)
        for pid in (400, 450):
            path = self.proc / str(pid) / "exe"
            path.unlink()
            path.symlink_to(executable)

    def bind_managed(self, sid=CURRENT, primary=CURRENT):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": sid}), \
                patch.object(codex_binding.os, "getpid", return_value=300):
            return codex_binding.bind(self.cfg, MAIN, self.pane, primary)

    def identity(self):
        return notify.reply_identity("codex", self.pane.get("agent_session", {}), self.home,
                                     MAIN, cfg=self.cfg, info=self.pane)

    def deliver(self, pane=MAIN, **kwargs):
        return notify.deliver(self.cfg, pane, "idle", "working", cli="codex", agent="codex", **kwargs)

    def begin_updates(self):
        self.bind()
        self.pane["agent_status"] = "working"
        identity = self.identity()[0]
        notify.mark_content_sent(MAIN, identity)
        tg.write_state(watch.CONTENT_LAST, {MAIN: identity})
        return identity

    def native_rows(self, mid="update-1", text="A complete public update.",
                    phase="commentary", stamp="2026-01-02T00:02:00Z", sid=CURRENT):
        # The actual supported native shapes, with entirely synthetic content.
        complete = {"type": "event_msg", "timestamp": stamp, "payload": {
            "type": "item_completed", "thread_id": sid, "turn_id": "synthetic-turn",
            "started_at_ms": 1, "completed_at_ms": 2,
            "item": {"type": "AgentMessage", "id": mid, "phase": phase,
                     "content": [{"type": "Text", "text": text}]}}}
        response = {"type": "response_item", "timestamp": stamp, "payload": {
            "type": "message", "role": "assistant", "id": mid, "phase": phase,
            "content": [{"type": "output_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {"turn_id": "synthetic-turn"}}}
        return complete, response

    def append_rows(self, *rows, sid=CURRENT):
        path = next(self.sessions.glob(f"*/*/*/rollout-*-{sid}.jsonl"))
        with path.open("a") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def poll_updates(self, prime=False):
        watch.poll_once(self.cfg, prime=prime)

    def test_complete_commentary_delivers_while_primary_remains_working(self):
        self.begin_updates()
        self.append_rows(*self.native_rows())
        self.poll_updates()
        self.assertEqual(self.pane["agent_status"], "working")
        self.assertEqual(self.api.texts, ["A complete public update."])
        self.assertEqual(len(tg.read_state(notify.CONTENT_SENT, {})), 2)
        self.assertEqual(next(iter(tg.read_state(tg.MSG_MAP, {}).values()))["pane"], MAIN)
        self.poll_updates()
        self.assertEqual(len(self.api.sent), 1)

    def test_multiple_complete_updates_and_final_keep_order_and_share_dedup(self):
        self.begin_updates()
        for n, phase in [(2, "commentary"), (3, "partial_answer"), (4, "final_answer")]:
            self.append_rows(*self.native_rows(f"update-{n}", f"Complete message {n}.", phase,
                                              f"2026-01-02T00:0{n}:00Z"))
        self.poll_updates()
        self.assertEqual(self.api.texts, [f"Complete message {n}." for n in (2, 3, 4)])
        self.pane["agent_status"] = "idle"
        tg.write_state("last_status.json", {MAIN: "working"})
        self.poll_updates()
        self.clock += 20
        self.assertFalse(self.deliver())
        self.assertEqual(len(self.api.sent), 3)

    def assert_overlapping_completion_delivers_once(self, outer_origin, inner_origin):
        self.begin_updates()
        self.append_rows(*self.native_rows("overlap-final", "One complete final answer.", "final_answer"))
        self.pane["agent_status"] = "idle"
        update = codex_binding.completed_updates(self.cfg, MAIN, self.pane)[0]
        api = tg.api
        nested = []

        def send(origin):
            return notify.deliver(self.cfg, MAIN, "done", "working", cli="codex", agent="codex",
                                  origin=origin, content_id=update.identity if origin != "status" else "",
                                  completed_update=update if origin == "completed" else None)

        def completion_during_send(cfg, method, params):
            if method == "sendMessage" and not nested:
                nested.append(None)
                nested[0] = send(inner_origin)
            return api(cfg, method, params)

        with patch.object(tg, "api", side_effect=completion_during_send):
            self.assertTrue(send(outer_origin))
        self.assertEqual(nested, [False])
        self.assertEqual(self.api.texts, ["One complete final answer."])
        self.assertEqual(len(tg.read_state(notify.CONTENT_SENT, {})), 2)

    def test_status_completion_during_completed_send_cannot_duplicate_final(self):
        self.assert_overlapping_completion_delivers_once("completed", "status")

    def test_completed_message_during_status_send_cannot_duplicate_final(self):
        self.assert_overlapping_completion_delivers_once("status", "completed")

    def test_completed_message_during_content_send_cannot_duplicate_final(self):
        self.assert_overlapping_completion_delivers_once("content", "completed")

    def test_completion_poll_before_status_diff_preserves_message_order(self):
        self.begin_updates()
        self.append_rows(*self.native_rows())
        self.append_rows(*self.native_rows("final", "The complete final answer.", "final_answer",
                                          "2026-01-02T00:03:00Z"))
        self.pane["agent_status"] = "idle"
        tg.write_state("last_status.json", {MAIN: "working"})
        self.poll_updates()
        self.assertEqual(self.api.texts, ["A complete public update.", "The complete final answer."])
        self.assertEqual(len(tg.read_state(notify.CONTENT_SENT, {})), 3)

    def test_started_delta_and_unpaired_response_never_deliver_while_working(self):
        baseline = self.begin_updates()
        complete, response = self.native_rows()
        started = json.loads(json.dumps(complete))
        started["payload"]["type"] = "item_started"
        self.append_rows(started, {"type": "event_msg", "payload": {
            "type": "agent_message_delta", "delta": "Unfinished synthetic fragment"}}, response)
        self.poll_updates()
        self.assertEqual(self.api.sent, [])
        self.assertEqual(tg.read_state(watch.CONTENT_LAST, {})[MAIN], baseline)
        self.append_rows(complete)
        self.poll_updates()
        self.assertEqual(self.api.texts, ["A complete public update."])

    def test_unterminated_or_torn_jsonl_response_waits_for_the_sealed_record(self):
        self.begin_updates()
        complete, response = self.native_rows()
        self.append_rows(complete)
        path = next(self.sessions.glob(f"*/*/*/rollout-*-{CURRENT}.jsonl"))
        encoded = json.dumps(response)
        with path.open("a") as stream:
            stream.write(encoded[:-10])
        self.poll_updates()
        self.assertEqual(self.api.sent, [])
        with path.open("a") as stream:
            stream.write(encoded[-10:])
        self.poll_updates()
        self.assertEqual(self.api.sent, [])
        with path.open("a") as stream:
            stream.write("\n")
        self.poll_updates()
        self.assertEqual(self.api.texts, ["A complete public update."])

    def test_completed_item_waits_for_matching_persisted_assistant_response(self):
        self.begin_updates()
        complete, response = self.native_rows()
        self.append_rows(complete)
        self.poll_updates()
        self.assertEqual(self.api.sent, [])
        self.append_rows(response)
        self.poll_updates()
        self.assertEqual(self.api.texts, ["A complete public update."])

    def test_other_thread_or_turn_completion_cannot_authorize_public_response(self):
        self.begin_updates()
        for n, mismatch in enumerate(("thread", "turn")):
            complete, response = self.native_rows(f"mismatch-{n}")
            if mismatch == "thread":
                complete["payload"]["thread_id"] = OTHER
            else:
                response["payload"]["internal_chat_message_metadata_passthrough"]["turn_id"] = "other-turn"
            self.append_rows(complete, response)
        self.poll_updates()
        self.assertEqual(self.api.sent, [])

    def test_complete_reasoning_tool_private_chat_and_nonpublic_messages_are_excluded(self):
        self.begin_updates()
        for n, kind in enumerate(("Reasoning", "DynamicToolCall", "SubAgentActivity", "CommandExecution")):
            complete, response = self.native_rows(f"nonchat-{n}")
            complete["payload"]["item"]["type"] = kind
            self.append_rows(complete, response)
        for n, change in enumerate(({"type": "agent_message", "author": "primary", "recipient": "worker"},
                                    {"recipient": "worker"}, {"role": "tool"}, {"visibility": "private"},
                                    {"channel": "analysis"}, {"phase": "analysis"}, {"status": "in_progress"})):
            complete, response = self.native_rows(f"private-{n}")
            response["payload"].update(change)
            self.append_rows(complete, response)
        self.poll_updates()
        self.assertEqual(self.api.sent, [])

    def test_mixed_unsupported_content_or_different_complete_text_never_leaks_a_fragment(self):
        self.begin_updates()
        for n, change in enumerate(("event-private", "response-private", "text")):
            complete, response = self.native_rows(f"mixed-{n}")
            if change == "event-private":
                complete["payload"]["item"]["content"].append({"type": "Reasoning", "text": "private"})
            elif change == "response-private":
                response["payload"]["content"].append({"type": "reasoning_text", "text": "private"})
            else:
                complete["payload"]["item"]["content"][0]["text"] = "Different completed text."
            self.append_rows(complete, response)
        self.poll_updates()
        self.assertEqual(self.api.sent, [])

    def test_unknown_phase_is_excluded_but_native_optional_phase_is_supported(self):
        self.begin_updates()
        self.append_rows(*self.native_rows("unsupported", phase="unknown-private-phase"))
        self.append_rows(*self.native_rows("legacy-phase", "Completed legacy public text.", phase=None))
        self.poll_updates()
        self.assertEqual(self.api.texts, ["Completed legacy public text."])

    def test_duplicate_completion_records_send_once_and_conflicting_ids_fail_closed(self):
        self.begin_updates()
        pair = self.native_rows()
        self.append_rows(*pair, *pair)
        self.poll_updates()
        self.assertEqual(len(self.api.sent), 1)
        self.append_rows(*self.native_rows("conflict", "One text.", stamp="2026-01-02T00:03:00Z"))
        self.append_rows(*self.native_rows("conflict", "Another text.", stamp="2026-01-02T00:04:00Z"))
        self.poll_updates()
        self.assertEqual(len(self.api.sent), 1)

    def test_failed_complete_update_retains_order_and_retries_without_stamping(self):
        baseline = self.begin_updates()
        self.append_rows(*self.native_rows())
        self.append_rows(*self.native_rows("update-2", "Second complete update.", stamp="2026-01-02T00:03:00Z"))
        self.api.fail_send = "synthetic failure"
        self.poll_updates()
        self.assertEqual(self.api.sent, [])
        self.assertEqual(len(tg.read_state(notify.CONTENT_SENT, {})), 1)
        self.assertEqual(tg.read_state(watch.CONTENT_LAST, {})[MAIN], baseline)
        self.api.fail_send = ""
        self.clock += 20
        self.poll_updates()
        self.poll_updates()
        self.assertEqual(self.api.texts, ["A complete public update.", "Second complete update."])
        self.assertEqual(len(tg.read_state(notify.CONTENT_SENT, {})), 3)

    def test_idle_status_cannot_overtake_a_failed_completed_update(self):
        baseline = self.begin_updates()
        self.append_rows(*self.native_rows())
        self.append_rows(*self.native_rows("final", "The complete final answer.", "final_answer",
                                          "2026-01-02T00:03:00Z"))
        self.pane["agent_status"] = "idle"
        tg.write_state("last_status.json", {MAIN: "working"})
        api = tg.api
        attempts = []
        def fail_first(cfg, method, params):
            if method == "sendMessage":
                attempts.append(params["text"])
                if len(attempts) == 1:
                    return None, "synthetic first send failure"
            return api(cfg, method, params)
        with patch.object(tg, "api", side_effect=fail_first):
            self.poll_updates()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.api.sent, [])
        self.assertEqual(tg.read_state(watch.CONTENT_LAST, {})[MAIN], baseline)
        self.clock += 20
        self.poll_updates()
        self.poll_updates()
        self.assertEqual(self.api.texts, ["A complete public update.", "The complete final answer."])

    def test_restart_delivers_undelivered_complete_update_while_working_once(self):
        self.begin_updates()
        tg.write_state(watch.CONTENT_LAST, {})
        self.append_rows(*self.native_rows())
        self.poll_updates(prime=True)
        self.poll_updates(prime=True)
        self.assertEqual(self.api.texts, ["A complete public update."])

    def test_fresh_install_primes_history_and_empty_completed_baseline_ignores_partial(self):
        self.bind()
        self.pane["agent_status"] = "working"
        complete, response = self.native_rows()
        self.append_rows(response)
        self.poll_updates(prime=True)
        self.assertEqual(tg.read_state(watch.CONTENT_LAST, {})[MAIN], "")
        self.append_rows(complete)
        self.poll_updates()
        self.assertEqual(self.api.texts, ["A complete public update."])

    def test_fresh_install_with_already_completed_history_does_not_reannounce(self):
        self.bind()
        self.pane["agent_status"] = "working"
        self.append_rows(*self.native_rows())
        self.poll_updates(prime=True)
        self.assertEqual(self.api.sent, [])
        self.append_rows(*self.native_rows("new", "New complete update.", stamp="2026-01-02T00:03:00Z"))
        self.poll_updates()
        self.assertEqual(self.api.texts, ["New complete update."])

    def test_complete_updates_preserve_worker_exclusion_and_default_off_gate(self):
        self.begin_updates()
        self.append_rows(*self.native_rows())
        worker = agent(WORKER, self.home, cli="codex", status="working",
                       session={"kind": "path", "value": next(self.sessions.glob(f"*/*/*/*{CURRENT}.jsonl")).as_posix()})
        self.herdr.agents.append(worker)
        watch.content_pass(self.cfg, worker, main=MAIN)
        update = codex_binding.completed_updates(self.cfg, MAIN, self.pane)[0]
        self.assertFalse(notify.deliver(self.cfg, WORKER, "done", "working", cli="codex",
                                       origin="completed", content_id=update.identity, completed_update=update))
        cfg = {**self.cfg, "firstmate": {"enabled": False}}
        self.assertFalse(notify.deliver(cfg, MAIN, "done", "working", cli="codex",
                                       origin="completed", content_id=update.identity, completed_update=update))
        self.assertEqual(self.api.sent, [])

    def test_complete_updates_respect_agent_status_and_watch_filters(self):
        self.begin_updates()
        self.append_rows(*self.native_rows())
        for cfg in ({**self.cfg, "agents": ["pi"]}, {**self.cfg, "statuses": ["blocked"]},
                    {**self.cfg, "watch": {"content_delivery": False}}):
            watch.poll_once(cfg)
        self.assertEqual(self.api.sent, [])
        self.poll_updates()
        self.assertEqual(self.api.texts, ["A complete public update."])

    def test_completion_authority_does_not_weaken_replaced_or_ambiguous_binding(self):
        self.begin_updates()
        self.append_rows(*self.native_rows())
        self.process(200, 100, "replacement", name="codex")
        self.poll_updates()
        self.assertEqual(self.api.sent, [])
        self.bind()
        self.rollout(OTHER, "Same-cwd worker activity")
        self.poll_updates()
        self.assertEqual(self.api.sent, [])

    def test_session_replacement_between_completion_check_and_send_cannot_borrow_identity(self):
        self.begin_updates()
        self.append_rows(*self.native_rows())
        check = codex_binding.current_update
        calls = []
        def replace(*args):
            result = check(*args)
            calls.append(result)
            if len(calls) == 1:
                self.rollout(NEXT, "Replacement session")
                self.append_rows(*self.native_rows(sid=NEXT), sid=NEXT)
                self.bind(NEXT)
            return result
        with patch.object(codex_binding, "current_update", side_effect=replace):
            self.poll_updates()
        self.assertEqual(self.api.sent, [])
        self.assertEqual(calls, [True, False])
        self.clock += 20
        self.poll_updates()
        self.assertEqual(self.api.texts, ["A complete public update."])

    def test_explicit_native_reference_keeps_priority_for_completed_updates(self):
        self.begin_updates()
        path = self.rollout(NEXT, "Explicit reference")
        self.append_rows(*self.native_rows(sid=NEXT), sid=NEXT)
        self.pane["agent_session"] = {"kind": "path", "value": path}
        self.poll_updates()
        self.assertEqual(self.api.texts, ["A complete public update."])

    def test_explicit_native_id_is_exact_and_has_priority_for_completed_updates(self):
        self.begin_updates()
        self.rollout(NEXT, "Explicit id reference")
        self.append_rows(*self.native_rows(sid=NEXT), sid=NEXT)
        self.pane["agent_session"] = {"kind": "id", "value": NEXT}
        self.poll_updates()
        self.assertEqual(self.api.texts, ["A complete public update."])

    def test_ambiguous_id_or_glob_like_reference_cannot_select_complete_updates(self):
        self.begin_updates()
        self.append_rows(*self.native_rows())
        path = next(self.sessions.glob(f"*/*/*/*{CURRENT}.jsonl"))
        duplicate = self.sessions / "2026/01/03" / path.name
        duplicate.parent.mkdir(); duplicate.write_bytes(path.read_bytes())
        for value in (CURRENT, "*"):
            self.pane["agent_session"] = {"kind": "id", "value": value}
            self.poll_updates()
        self.assertEqual(self.api.sent, [])

    def test_delivered_boundary_outside_bounded_tail_uses_native_message_timestamp(self):
        self.begin_updates()
        self.append_rows({"type": "response_item", "payload": {"type": "reasoning", "synthetic": "x" * 2_100_000}})
        self.append_rows(*self.native_rows())
        self.poll_updates()
        self.assertEqual(self.api.texts, ["A complete public update."])

    def test_complete_event_outside_tail_cannot_be_inferred_from_a_response(self):
        self.begin_updates()
        complete, response = self.native_rows()
        self.append_rows(complete)
        self.append_rows({"type": "response_item", "payload": {"type": "reasoning", "synthetic": "x" * 2_100_000}})
        self.append_rows(response)
        self.poll_updates()
        self.assertEqual(self.api.sent, [])

    def test_omitted_ref_native_answer_delivers_once_across_all_paths(self):
        self.rollout(OTHER, "A newer same-cwd conversation must not win.")
        self.bind()
        tg.write_state("last_status.json", {MAIN: "working"})
        watch.poll_once(self.cfg)
        watch.content_pass(self.cfg, self.pane, main=MAIN)
        self.clock += 20
        again = self.deliver()
        self.assertEqual(self.api.texts, [ANSWER])
        self.assertFalse(again)
        self.assertEqual(self.api.sent[0]["chat_id"], str(CHAT))
        self.assertEqual(len(tg.read_state(notify.CONTENT_SENT, {})), 1)
        self.assertEqual(next(iter(tg.read_state(tg.MSG_MAP, {}).values()))["pane"], MAIN)

    def test_same_cwd_ambiguity_without_binding_never_guesses_or_sends_banner(self):
        self.rollout(OTHER, "Wrong same-cwd answer")
        self.assertEqual(self.identity(), ("", ""))
        self.assertFalse(self.deliver())
        self.clock += 20
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.sent, [])
        self.assertEqual(tg.read_state(notify.CONTENT_SENT, {}), {})

    def test_explicit_path_has_priority_over_binding(self):
        self.bind()
        path = self.rollout(OTHER, "Explicit reference answer")
        self.pane["agent_session"] = {"kind": "path", "value": path}
        self.assertTrue(self.deliver())
        self.assertEqual(self.api.texts, ["Explicit reference answer"])

    def test_explicit_id_has_priority_over_binding(self):
        self.bind()
        self.rollout(OTHER, "Explicit id answer")
        self.pane["agent_session"] = {"kind": "id", "value": OTHER}
        with patch.object(notify.os.path, "expanduser", return_value=str(self.base)):
            self.assertTrue(self.deliver())
        self.assertEqual(self.api.texts, ["Explicit id answer"])

    def test_stale_explicit_path_does_not_select_sibling_or_bound_session(self):
        self.bind()
        self.pane["agent_session"] = {"kind": "path", "value": str(self.sessions / "2026/01/02/gone.jsonl")}
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.sent, [])

    def test_process_replacement_refuses_old_binding_then_rebinds(self):
        self.bind()
        self.herdr.pid = 201
        self.process(201, 100, "21")
        self.process(300, 201, "30")
        self.rollout(NEXT, "Replacement answer")
        self.assertFalse(self.deliver())
        self.bind(NEXT)
        self.assertTrue(self.deliver())
        self.assertEqual(self.api.texts, ["Replacement answer"])

    def test_pid_reuse_cannot_reuse_binding(self):
        self.bind()
        self.process(200, 100, "2000")
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.sent, [])

    def test_replacement_during_settle_cannot_send_old_reply(self):
        self.bind()
        def replace(*args):
            self.rollout(NEXT, "Replacement during settle")
            self.bind(NEXT)
            return True
        with patch.object(notify, "settled", side_effect=replace):
            self.assertFalse(self.deliver())
        self.assertEqual(self.api.sent, [])
        self.assertEqual(tg.read_state(notify.CONTENT_SENT, {}), {})
        self.assertTrue(self.deliver())
        self.assertEqual(self.api.texts, ["Replacement during settle"])

    def test_duplicate_exact_id_files_refuse_association(self):
        self.bind()
        source = Path(self.rollout(CURRENT, ANSWER))
        duplicate = self.sessions / "2026/01/03" / source.name
        duplicate.parent.mkdir()
        duplicate.write_bytes(source.read_bytes())
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.sent, [])

    def test_corrupt_native_metadata_refuses_association(self):
        self.bind()
        bound = self.rollout(CURRENT, ANSWER)
        Path(bound).write_text("[]\n")
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.sent, [])
        self.assertIn(bound, self.log_text())

    def test_stable_corrupt_foreign_rollouts_never_block_binding_or_reads(self):
        zero = self.corrupt(BROKEN, "")
        torn = self.corrupt(CRASHED, '{"type": "sessio')
        self.bind()
        binding = tg.read_state(codex_binding.BINDING, {})
        self.assertEqual(binding["other_rollouts"], {})
        self.assertEqual(sorted(binding["opaque_rollouts"]), sorted([zero, torn]))
        tg.write_state("last_status.json", {MAIN: "working"})
        watch.poll_once(self.cfg)
        watch.content_pass(self.cfg, self.pane, main=MAIN)
        self.assertEqual(self.api.texts, [ANSWER])
        self.clock += 20
        self.assertFalse(self.deliver())
        self.assertEqual(len(self.api.sent), 1)

    def test_changed_or_new_opaque_rollout_invalidates_until_native_rebind(self):
        broken = self.corrupt(BROKEN, "")
        self.bind()
        self.assertTrue(self.deliver())
        self.assertEqual(self.api.texts, [ANSWER])
        self.corrupt(BROKEN, '{"type": "session_meta", "payl')
        self.assertEqual(codex_binding.resolve(self.cfg, MAIN, self.pane), "")
        self.assertIn(broken, self.log_text())
        crashed = self.corrupt(CRASHED, "")
        self.assertEqual(codex_binding.resolve(self.cfg, MAIN, self.pane), "")
        self.assertIn(crashed, self.log_text())
        self.bind()
        self.rollout(CURRENT, "Second turn answer")
        self.clock += 20
        self.assertTrue(self.deliver())
        self.assertEqual(self.api.texts, [ANSWER, "Second turn answer"])

    def test_known_same_cwd_competitor_turning_unreadable_still_invalidates(self):
        competitor = self.rollout(OTHER, "Inactive same-cwd competitor")
        self.bind()
        os.chmod(competitor, 0)
        self.addCleanup(os.chmod, competitor, 0o644)
        self.assertEqual(codex_binding.resolve(self.cfg, MAIN, self.pane), "")
        self.assertIn(competitor, self.log_text())
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.sent, [])

    def test_same_process_new_session_refuses_until_native_rebind(self):
        self.bind()
        self.rollout(NEXT, "New conversation answer")
        self.assertFalse(self.deliver())
        self.bind(NEXT)
        self.assertTrue(self.deliver())
        self.assertEqual(self.api.texts, ["New conversation answer"])

    def test_same_process_resume_of_existing_session_invalidates_binding(self):
        self.rollout(OTHER, "An old inactive session")
        self.bind()
        self.rollout(OTHER, "Resumed session final answer")
        self.assertFalse(self.deliver())
        self.bind(OTHER)
        self.assertTrue(self.deliver())
        self.assertEqual(self.api.texts, ["Resumed session final answer"])

    def test_foreign_worker_rollout_activity_does_not_invalidate_main(self):
        self.bind()
        self.rollout(OTHER, "Worker answer", cwd=self.home + "/worktree")
        self.assertTrue(self.deliver())
        self.assertEqual(self.api.texts, [ANSWER])

    def test_changed_same_cwd_worker_fails_closed_instead_of_retargeting(self):
        self.rollout(OTHER, "Inactive same-cwd worker")
        self.bind()
        self.rollout(OTHER, "Active same-cwd worker")
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.sent, [])

    def test_worker_never_relays_even_with_main_cwd_and_explicit_reference(self):
        self.bind()
        worker = agent(WORKER, self.home, cli="codex", status="done",
                       session={"kind": "path", "value": self.rollout(OTHER, "Worker answer")})
        self.herdr.agents.append(worker)
        self.assertFalse(self.deliver(WORKER))
        watch.content_pass(self.cfg, worker, main=MAIN)
        self.assertEqual(self.api.sent, [])

    def test_send_failure_leaves_marker_unstamped_and_content_retries(self):
        self.bind()
        tg.write_state(watch.CONTENT_LAST, {MAIN: "old-message"})
        self.api.fail_send = "synthetic failure"
        self.assertFalse(self.deliver())
        self.assertEqual(tg.read_state(notify.CONTENT_SENT, {}), {})
        self.api.fail_send = ""
        self.clock += 20
        watch.content_pass(self.cfg, self.pane, main=MAIN)
        self.clock += 20
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.texts, [ANSWER])
        self.assertEqual(len(tg.read_state(notify.CONTENT_SENT, {})), 1)

    def test_rebinding_between_content_read_and_send_does_not_stamp_wrong_identity(self):
        self.bind()
        old_identity = self.identity()[0]
        self.rollout(NEXT, "Replacement answer")
        self.bind(NEXT)
        self.assertFalse(self.deliver(origin="content", content_id=old_identity))
        self.assertEqual(tg.read_state(notify.CONTENT_SENT, {}), {})
        self.assertTrue(self.deliver())
        self.assertEqual(self.api.texts, ["Replacement answer"])

    def test_binding_requires_primary_process_ancestry(self):
        self.process(300, 100, "30")
        with self.assertRaises(codex_binding.BindingError):
            self.bind()
        self.assertEqual(tg.read_state(codex_binding.BINDING, {}), {})

    def test_managed_cli_with_owner_reference_delivers_real_text_once(self):
        self.managed_chain()
        self.rollout(OTHER, "Same-cwd worker answer")
        write_config(self.cfg)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": CURRENT}), \
                patch.object(codex_binding.os, "getpid", return_value=300), \
                patch.object(codex_binding.sys, "argv", ["codex_binding.py", "--primary-session", CURRENT]), \
                patch.object(tg, "resolve_token", side_effect=AssertionError("credential lookup")), \
                redirect_stdout(io.StringIO()):
            codex_binding.main()
        binding = tg.read_state(codex_binding.BINDING, {})
        self.assertEqual((binding["pid"], binding["start_ticks"]), (200, "20"))
        self.assertEqual((binding["managed_pid"], binding["managed_start_ticks"]), (400, "400"))
        self.assertEqual(binding["session_id"], CURRENT)
        tg.write_state("last_status.json", {MAIN: "working"})
        watch.poll_once(self.cfg)
        watch.content_pass(self.cfg, self.pane, main=MAIN)
        self.clock += 20
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.texts, [ANSWER])

    def test_mise_managed_cli_prepares_without_writing_then_delivers_once(self):
        self.mise_chain()
        self.rollout(OTHER, "Same-cwd worker answer")
        write_config(self.cfg)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": CURRENT}), \
                patch.object(codex_binding.os, "getpid", return_value=300), \
                patch.object(tg, "resolve_token", side_effect=AssertionError("credential lookup")), \
                redirect_stdout(io.StringIO()):
            candidate = codex_binding.prepare_binding(self.cfg, MAIN, self.pane, CURRENT)
            self.assertEqual((candidate["managed_pid"], candidate["managed_start_ticks"]),
                             (400, "400"))
            self.assertEqual(candidate["session_id"], CURRENT)
            self.assertEqual(tg.read_state(codex_binding.BINDING, {}), {})
            with patch.object(codex_binding.sys, "argv",
                              ["codex_binding.py", "--primary-session", CURRENT]):
                codex_binding.main()
        binding = tg.read_state(codex_binding.BINDING, {})
        self.assertEqual((binding["pid"], binding["start_ticks"]), (200, "20"))
        self.assertEqual((binding["managed_pid"], binding["managed_start_ticks"]), (400, "400"))
        self.assertEqual(binding["session_id"], CURRENT)
        tg.write_state("last_status.json", {MAIN: "working"})
        watch.poll_once(self.cfg)
        watch.content_pass(self.cfg, self.pane, main=MAIN)
        self.clock += 20
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.texts, [ANSWER])

    def test_mise_managed_cli_still_requires_exact_owner_and_app_server_mode(self):
        self.mise_chain()
        self.rollout(OTHER, "Worker text")
        self.rollout(NEXT, "Old primary text")
        for native, primary in [(OTHER, CURRENT), (NEXT, CURRENT), (CURRENT, NEXT),
                                (CURRENT, ""), (CURRENT, "../../guess")]:
            with self.subTest(native=native, primary=primary), \
                    self.assertRaises(codex_binding.BindingError):
                self.bind_managed(native, primary)
        self.assertEqual(tg.read_state(codex_binding.BINDING, {}), {})

        cmdline = self.proc / "400/cmdline"
        cmdline.write_bytes(b"codex\0exec\0")
        with self.assertRaises(codex_binding.BindingError):
            self.bind_managed()
        self.assertEqual(tg.read_state(codex_binding.BINDING, {}), {})

    def test_managed_without_owner_reference_reproduces_operator_refusal(self):
        self.managed_chain()
        with self.assertRaises(codex_binding.BindingError):
            self.bind()
        self.assertEqual(tg.read_state(codex_binding.BINDING, {}), {})

    def test_managed_worker_old_or_invalid_owner_reference_cannot_bind(self):
        self.managed_chain()
        self.rollout(OTHER, "Worker text")
        self.rollout(NEXT, "Old text")
        for native, primary in [(OTHER, CURRENT), (NEXT, CURRENT), (CURRENT, NEXT),
                                (CURRENT, ""), (CURRENT, "../../guess")]:
            with self.subTest(native=native, primary=primary), self.assertRaises(codex_binding.BindingError):
                self.bind_managed(native, primary)
        self.assertEqual(tg.read_state(codex_binding.BINDING, {}), {})

    def test_owner_reference_does_not_authorize_an_ordinary_shell(self):
        self.process(300, 100, "30")
        with self.assertRaises(codex_binding.BindingError):
            self.bind_managed()
        self.assertEqual(tg.read_state(codex_binding.BINDING, {}), {})

    def test_managed_requires_installed_executable_and_app_server_mode(self):
        self.managed_chain()
        cmdline = self.proc / "400/cmdline"
        cmdline.write_bytes(b"codex\0exec\0")
        with self.assertRaises(codex_binding.BindingError):
            self.bind_managed()
        cmdline.write_bytes(b"codex\0app-server\0")
        (self.proc / "400/exe").unlink()
        (self.proc / "400/exe").symlink_to("/usr/bin/python3")
        with self.assertRaises(codex_binding.BindingError):
            self.bind_managed()
        self.assertEqual(tg.read_state(codex_binding.BINDING, {}), {})

    def test_managed_caller_must_be_owned_by_current_user(self):
        self.managed_chain()
        with patch.object(codex_binding.os, "getuid", return_value=os.getuid() + 1), \
                self.assertRaises(codex_binding.BindingError):
            self.bind_managed()
        self.assertEqual(tg.read_state(codex_binding.BINDING, {}), {})

    def test_managed_parent_cannot_bypass_nearer_worker_codex(self):
        self.managed_chain()
        self.process(500, 400, "500", name="codex")
        self.process(350, 500, "350", name="bash")
        (self.proc / "500/exe").symlink_to("/usr/bin/python3")
        with self.assertRaises(codex_binding.BindingError):
            self.bind_managed()
        self.assertEqual(tg.read_state(codex_binding.BINDING, {}), {})

    def test_managed_foreground_ambiguity_cannot_select_a_process(self):
        self.managed_chain()
        with patch.object(codex_binding, "_foreground", return_value=[200, 201]), \
                self.assertRaises(codex_binding.BindingError):
            self.bind_managed()
        self.assertEqual(tg.read_state(codex_binding.BINDING, {}), {})

    def test_managed_process_replacement_invalidates_reads_until_rebind(self):
        self.managed_chain()
        self.bind_managed()
        self.process(400, 450, "401", name="codex")
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.sent, [])
        self.bind_managed()
        self.assertTrue(self.deliver())
        self.assertEqual(self.api.texts, [ANSWER])

    def test_managed_session_replacement_needs_new_primary_owner_reference(self):
        self.managed_chain()
        self.bind_managed()
        self.rollout(NEXT, "Replacement managed answer")
        self.assertFalse(self.deliver())
        with self.assertRaises(codex_binding.BindingError):
            self.bind_managed(NEXT, CURRENT)
        self.bind_managed(NEXT, NEXT)
        self.assertTrue(self.deliver())
        self.assertEqual(self.api.texts, ["Replacement managed answer"])

    def test_replacement_during_managed_binding_never_writes_stale_snapshot(self):
        self.managed_chain()
        scan = codex_binding._others
        for target in ["frontend", "managed"]:
            with self.subTest(target=target):
                def replace(*args):
                    result = scan(*args)
                    pid = 200 if target == "frontend" else 400
                    self.process(pid, 100 if target == "frontend" else 450,
                                 "replaced", name="codex")
                    return result
                with patch.object(codex_binding, "_others", side_effect=replace), \
                        self.assertRaises(codex_binding.BindingError):
                    self.bind_managed()
                self.assertEqual(tg.read_state(codex_binding.BINDING, {}), {})

    def test_direct_pane_reference_mismatch_is_also_refused(self):
        with self.assertRaises(codex_binding.BindingError):
            self.bind_managed(CURRENT, OTHER)
        self.assertEqual(tg.read_state(codex_binding.BINDING, {}), {})

    def test_binding_cli_uses_native_context_without_credentials_or_transport(self):
        write_config(self.cfg)
        output = io.StringIO()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": CURRENT}), \
                patch.object(codex_binding.os, "getpid", return_value=300), \
                patch.object(codex_binding.sys, "argv", ["codex_binding.py"]), \
                patch.object(tg, "resolve_token", side_effect=AssertionError("credential lookup")), \
                patch.object(tg, "api", side_effect=AssertionError("live send")), redirect_stdout(output):
            codex_binding.main()
            with patch.object(codex_binding.sys, "argv", ["codex_binding.py", "--check"]):
                codex_binding.main()
        self.assertEqual(tg.read_state(codex_binding.BINDING, {})["session_id"], CURRENT)
        self.assertEqual(output.getvalue().count("binding validated"), 2)

    def test_worker_cannot_install_main_binding(self):
        worker = agent(WORKER, self.home, cli="codex")
        self.herdr.agents.append(worker)
        with self.assertRaises(codex_binding.BindingError):
            codex_binding.bind(self.cfg, WORKER, worker)
        self.assertEqual(tg.read_state(codex_binding.BINDING, {}), {})

    def test_same_cwd_worker_descended_from_main_cannot_bind_main(self):
        self.process(250, 200, "25", name="codex")
        self.process(300, 250, "30")
        self.rollout(OTHER, "Same-cwd worker reply")
        with self.assertRaises(codex_binding.BindingError):
            self.bind(OTHER)
        self.assertEqual(tg.read_state(codex_binding.BINDING, {}), {})

    def test_binding_requires_native_thread_id_and_matching_metadata(self):
        for sid in ["", "../../wrong", OTHER]:
            with self.subTest(sid=sid), self.assertRaises(codex_binding.BindingError):
                self.bind(sid)
        self.rollout(OTHER, "Wrong cwd", cwd=self.home + "/worker")
        with self.assertRaises(codex_binding.BindingError):
            self.bind(OTHER)

    def test_pi_no_reference_delivery_is_unchanged(self):
        self.bind()
        self.pane["agent"] = "pi"
        pi_root = self.base / "pi-sessions"
        directory = pi_root / ("--" + self.home[1:].replace("/", "-") + "--")
        directory.mkdir(parents=True)
        transcript(str(directory / "pi.jsonl"), "Synthetic request", ["Pi final answer"])
        with patch.object(notify, "PI_SESSIONS", str(pi_root)):
            self.assertTrue(notify.deliver(self.cfg, MAIN, "idle", "working", cli="pi", agent="pi"))
        self.assertEqual(self.api.texts, ["Pi final answer"])

    def test_default_off_keeps_upstream_header_only_behavior(self):
        self.bind()
        self.cfg["firstmate"]["enabled"] = False
        with patch.object(codex_binding, "resolve", side_effect=AssertionError("default-off binding read")):
            self.assertTrue(self.deliver())
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("Old session banner", self.api.texts[0])
        self.assertNotIn(ANSWER, self.api.texts[0])


if __name__ == "__main__":
    unittest.main()
