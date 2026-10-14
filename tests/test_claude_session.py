"""Synthetic native Claude registry: no live Herdr, Telegram or private state."""

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import _env  # noqa: F401  (isolate state/config before importing the bridge)
import claude_session
import notify
import tg
import watch
from _env import MAIN, WORKER, FakeApi, FakeHerdr, agent, make_home, reset_state, transcript

CURRENT = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
ANSWER = "The synthetic Claude repair is ready."


class ProcessHerdr(FakeHerdr):
    def __init__(self, agents, cwd):
        super().__init__(agents)
        self.foreground = [{"pid": 200, "name": "claude", "cwd": cwd}]
        self.process_pane = MAIN
        self.process_error = ""

    def json(self, *args, timeout=30):
        if args[:2] == ("pane", "process-info"):
            self.calls.append(args)
            return {"process_info": {"pane_id": self.process_pane,
                                      "foreground_processes": self.foreground}}, self.process_error
        return super().json(*args, timeout=timeout)


class TestClaudeSession(unittest.TestCase):
    def setUp(self):
        reset_state()
        watch._NOTES.clear()
        temp = tempfile.TemporaryDirectory(prefix="fm-claude-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.home = make_home(str(self.base), tasks=[("worker", WORKER, "ship", [])])
        self.sessions = self.base / ".claude/sessions"
        self.projects = self.base / ".claude/projects"
        self.proc = self.base / "proc"
        self.sessions.mkdir(parents=True)
        self.process()
        self.pane = agent(MAIN, self.home, cli="claude", title="Claude Code")
        self.herdr = ProcessHerdr([self.pane], self.home)
        self.api = FakeApi()
        self.cfg = {"chat_id": "100", "statuses": ["done", "blocked"],
                    "settle_ms": 0, "quiet_seconds": 0, "replies": False,
                    "firstmate": {"enabled": True, "main_cwd": self.home}}
        for target, name, value in [(claude_session, "SESSIONS", str(self.sessions)),
                                    (claude_session, "PROJECTS", str(self.projects)),
                                    (claude_session, "PROC", str(self.proc)),
                                    (tg, "herdr_json", self.herdr.json),
                                    (tg, "herdr_run", self.herdr.run), (tg, "api", self.api),
                                    (notify.tts, "maybe_speak", lambda *a, **kw: None)]:
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.native = {"pid": 200, "sessionId": CURRENT, "cwd": self.home,
                       "startedAt": 1791946134866, "procStart": "20",
                       "kind": "interactive", "entrypoint": "cli", "version": "2.1.296"}
        self.registry()
        self.path = self.rollout(CURRENT, [("old", "A prior synthetic reply.")])

    def process(self, ticks="20", name="claude", cwd=None):
        directory = self.proc / "200"
        directory.mkdir(parents=True, exist_ok=True)
        fields = ["S", "100"] + ["0"] * 17 + [ticks]
        (directory / "stat").write_text("200 (claude process) " + " ".join(fields))
        (directory / "comm").write_text(name)
        link = directory / "cwd"
        link.unlink(missing_ok=True)
        link.symlink_to(cwd or self.home)

    def registry(self):
        (self.sessions / "200.json").write_text(json.dumps(self.native))

    def rollout(self, sid, messages, cwd=None, folder="owner", request="Synthetic request."):
        path = self.projects / folder / (sid + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"type": "user", "sessionId": sid, "cwd": cwd or self.home,
                 "message": {"role": "user", "content": request}}]
        for mid, text in messages:
            rows.append({"type": "assistant", "sessionId": sid, "cwd": cwd or self.home,
                         "isSidechain": False, "timestamp": "2026-10-14T03:19:22.920Z",
                         "message": {"id": mid, "role": "assistant", "stop_reason": "end_turn",
                                     "content": [{"type": "text", "text": text}]}})
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path

    def source(self):
        return claude_session.resolve(self.cfg, MAIN, self.pane)

    def deliver(self):
        return notify.deliver(self.cfg, MAIN, "idle", "working", agent="claude", cli="claude")

    def final(self):
        self.rollout(CURRENT, [("old", "A prior synthetic reply."), ("final", ANSWER)])

    def assert_no_body(self):
        self.assertFalse(any(ANSWER in text for text in self.api.texts))
        self.assertEqual(tg.read_state("content_sent.json", {}), {})

    def test_missing_reference_notification_delivers_once_ignores_newer_background(self):
        self.final()
        background = self.rollout(OTHER, [("background", "Background synthetic reply.")])
        os.utime(background, (2000000000, 2000000000))
        self.assertTrue(self.deliver())
        self.assertEqual(self.api.texts, [ANSWER])
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.texts, [ANSWER])
        self.assertEqual(notify.reply_identity("claude", {}, self.home, MAIN,
                                             cfg=self.cfg, info=self.pane)[1], ANSWER)

    def test_notification_event_entry_point(self):
        self.final()
        tg.write_state("last_status.json", {MAIN: "working"})
        event = json.dumps({"data": {"pane_id": MAIN, "agent": "claude", "agent_status": "idle"}})
        with patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": event}), \
                patch.object(tg, "load_config", return_value=self.cfg), \
                patch.object(notify, "ensure_bot"):
            notify.main()
            notify.main()
        self.assertEqual(self.api.texts, [ANSWER])

    def test_notification_oversized_opening_record_delivers_once(self):
        for size in (700_000, 4_100_000):
            with self.subTest(size=size):
                reset_state()
                self.api.sent.clear()
                self.rollout(CURRENT, [("final", ANSWER)], request="x" * size)
                self.assertTrue(self.deliver())
                self.assertFalse(self.deliver())
                self.assertEqual(self.api.texts, [ANSWER])
                identity, text = notify.reply_identity("claude", {}, self.home, MAIN,
                                                     cfg=self.cfg, info=self.pane)
                self.assertEqual(text, ANSWER)
                self.assertTrue(notify.content_already_sent(MAIN, identity))

    def test_watcher_oversized_opening_record_orders_and_dedupes_replies(self):
        intermediate = "A complete synthetic intermediate reply."
        for size in (700_000, 4_100_000):
            with self.subTest(size=size):
                reset_state()
                self.api.sent.clear()
                self.pane["agent_status"] = "working"
                self.rollout(CURRENT, [("old", "A prior synthetic reply.")], request="x" * size)
                watch.poll_once(self.cfg, prime=True)
                self.assertEqual(self.api.sent, [])
                self.assertTrue(watch._content_last(MAIN).startswith("old@"))
                with self.path.open("a") as f:
                    for mid, text in (("interior", intermediate), ("final", ANSWER)):
                        f.write(json.dumps({"type": "assistant", "sessionId": CURRENT,
                                            "cwd": self.home, "timestamp": "2026-10-14T03:19:22.920Z",
                                            "message": {"id": mid, "role": "assistant",
                                                        "content": [{"type": "text", "text": text}]}}) + "\n")
                watch.poll_once(self.cfg)
                self.assertEqual(self.api.texts, [intermediate])
                self.pane["agent_status"] = "idle"
                watch.poll_once(self.cfg)
                watch.poll_once(self.cfg)
                self.assertFalse(self.deliver())
                self.assertEqual(self.api.texts, [intermediate, ANSWER])

    def test_oversized_transcript_rejects_mismatched_native_metadata(self):
        for size in (700_000, 4_100_000):
            for key, value in (("sessionId", OTHER), ("cwd", "/other")):
                for location in ("head", "tail"):
                    with self.subTest(size=size, key=key, location=location):
                        reset_state()
                        self.api.sent.clear()
                        self.rollout(CURRENT, [("final", ANSWER)], request="x" * size)
                        wrong = {"type": "user", "sessionId": CURRENT, "cwd": self.home,
                                 "message": {"role": "user", "content": "A conflicting record."},
                                 key: value}
                        record = json.dumps(wrong) + "\n"
                        if location == "head":
                            self.path.write_text(record + self.path.read_text())
                        else:
                            with self.path.open("a") as f:
                                f.write(record)
                        self.assertIsNone(self.source())
                        self.deliver()
                        watch.midrun_pass(self.cfg, self.pane)
                        self.assert_no_body()

    def test_oversized_transcript_incomplete_tail_is_not_identity_evidence(self):
        self.rollout(CURRENT, [], request="x" * 4_100_000)
        row = {"type": "assistant", "sessionId": CURRENT, "cwd": self.home,
               "message": {"id": "final", "role": "assistant", "content": ANSWER}}
        with self.path.open("a") as f:
            f.write(json.dumps(row)[:100])
        self.assertIsNone(self.source())
        self.deliver()
        watch.midrun_pass(self.cfg, self.pane)
        self.assert_no_body()

    def test_oversized_transcript_validation_keeps_reads_bounded(self):
        self.rollout(CURRENT, [("final", ANSWER)], request="x" * 4_100_000)
        reads = []
        def bounded_open(path, *args, **kwargs):
            f = open(path, *args, **kwargs)
            if str(path) == str(self.path):
                read = f.read
                total = 0
                def bounded_read(size=-1):
                    nonlocal total
                    self.assertGreater(size, 0)
                    data = read(size)
                    total += len(data)
                    reads.append(len(data))
                    self.assertLessEqual(total, 2_600_001)
                    return data
                f.read = bounded_read
            return f
        with patch("claude_session.open", side_effect=bounded_open):
            self.assertIsNotNone(self.source())
        self.assertTrue(reads)

    def test_watcher_entry_point_final_once_and_status_dedupes(self):
        with patch.object(watch, "content_pass", side_effect=AssertionError("Claude uses midrun")):
            watch.poll_once(self.cfg, prime=True)
            self.final()
            watch.poll_once(self.cfg)
            watch.poll_once(self.cfg)
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.texts, [ANSWER])

    def test_midrun_order_working_tail_hold_then_final_once(self):
        self.pane["agent_status"] = "working"
        watch.midrun_pass(self.cfg, self.pane, prime=True)
        self.rollout(CURRENT, [("old", "A prior synthetic reply."),
                               ("interior", "A complete synthetic intermediate reply."), ("final", ANSWER)])
        watch.midrun_pass(self.cfg, self.pane)
        self.assertEqual(self.api.texts, ["A complete synthetic intermediate reply."])
        self.pane["agent_status"] = "idle"
        watch.midrun_pass(self.cfg, self.pane)
        watch.midrun_pass(self.cfg, self.pane)
        self.assertEqual(self.api.texts, ["A complete synthetic intermediate reply.", ANSWER])
        self.assertFalse(self.deliver())

    def test_missing_unreadable_or_unsupported_registry_refuses_both_paths(self):
        self.final()
        registry = self.sessions / "200.json"
        for contents in (None, "{unfinished", "[]", "{}"):
            with self.subTest(contents=contents):
                reset_state()
                self.api.sent.clear()
                if contents is None:
                    registry.unlink(missing_ok=True)
                else:
                    registry.write_text(contents)
                self.assertIsNone(self.source())
                self.deliver()
                watch.midrun_pass(self.cfg, self.pane)
                self.assert_no_body()
        self.registry()
        with patch("claude_session.open", side_effect=PermissionError):
            self.assertIsNone(self.source())
            self.assertEqual(notify.relay_messages("claude", {}, self.home, MAIN,
                                                  cfg=self.cfg, info=self.pane), [])

    def test_wrong_native_pid_lifetime_cwd_session_schema_refuses_both_paths(self):
        self.final()
        original = self.native.copy()
        for key, value in (("pid", 201), ("pid", True), ("procStart", "21"),
                           ("procStart", None), ("cwd", "/other"), ("sessionId", "../unsafe*"),
                           ("sessionId", OTHER), ("startedAt", 0), ("kind", "background"),
                           ("entrypoint", "sdk")):
            with self.subTest(key=key, value=value):
                reset_state()
                self.api.sent.clear()
                self.native = {**original, key: value}
                self.registry()
                self.assertIsNone(self.source())
                self.deliver()
                watch.midrun_pass(self.cfg, self.pane)
                self.assert_no_body()

    def test_foreground_ambiguity_wrong_pane_pid_name_cwd_and_errors(self):
        good = self.herdr.foreground[0].copy()
        for foreground in ([], [good, good], [{**good, "pid": True}],
                           [{**good, "name": "pi"}], [{**good, "cwd": "/other"}]):
            with self.subTest(foreground=foreground):
                self.herdr.foreground = foreground
                self.assertIsNone(self.source())
        self.herdr.foreground = [good]
        self.herdr.process_pane = WORKER
        self.assertIsNone(self.source())
        self.herdr.process_pane = MAIN
        self.herdr.process_error = "unavailable"
        self.assertIsNone(self.source())

    def test_proc_reuse_wrong_identity_cwd_unavailable_and_foreign_uid(self):
        for args in ({"ticks": "21"}, {"name": "pi"}, {"cwd": "/other"}):
            with self.subTest(args=args):
                self.process(**args)
                self.assertIsNone(self.source())
        self.process()
        with patch.object(claude_session.os, "getuid", return_value=os.getuid() + 1):
            self.assertIsNone(self.source())
        shutil.rmtree(self.proc)
        self.assertIsNone(self.source())

    def test_duplicate_exact_transcripts_and_wrong_native_metadata(self):
        self.rollout(CURRENT, [("copy", ANSWER)], folder="duplicate")
        self.assertIsNone(self.source())
        shutil.rmtree(self.projects / "duplicate")
        self.rollout(CURRENT, [("wrong", ANSWER)], cwd="/other")
        self.assertIsNone(self.source())
        self.path.write_text(json.dumps({"type": "assistant", "sessionId": OTHER,
                                        "cwd": self.home}) + "\n")
        self.assertIsNone(self.source())
        self.path.write_text(json.dumps({"type": "summary"}) + "\n")
        self.assertIsNone(self.source())

    def test_worker_and_ambiguous_main_never_resolve_or_deliver(self):
        worker = agent(WORKER, self.home, cli="claude")
        self.herdr.agents.append(worker)
        self.assertIsNotNone(self.source())  # claimed worker cannot displace main
        self.assertIsNone(claude_session.resolve(self.cfg, WORKER, worker))
        self.assertFalse(notify.deliver(self.cfg, WORKER, "done", "working", cli="claude"))
        self.herdr.agents.append(agent("other:p1", self.home, cli="claude"))
        self.assertIsNone(self.source())
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.sent, [])

    def test_explicit_path_and_id_are_authoritative_even_without_registry(self):
        self.final()
        (self.sessions / "200.json").unlink()
        self.pane["agent_session"] = {"kind": "path", "value": str(self.path)}
        with patch.object(claude_session, "resolve", side_effect=AssertionError("must not discover")):
            self.assertTrue(self.deliver())
            self.assertEqual(self.api.texts, [ANSWER])
            with patch.object(notify.os.path, "expanduser", return_value=str(self.base)):
                identity, text = notify.reply_identity("claude", {"kind": "id", "value": CURRENT}, self.home)
                self.assertTrue(identity)
                self.assertEqual(text, ANSWER)
        self.pane["agent_session"] = {"kind": "id", "value": OTHER}
        self.assertIsNone(self.source())

    def test_registry_generation_changes_during_resolution(self):
        read = claude_session._transcript
        def changed(*args):
            result = read(*args)
            self.native["startedAt"] += 1
            self.registry()
            return result
        with patch.object(claude_session, "_transcript", side_effect=changed):
            self.assertIsNone(self.source())

    def test_current_rejects_registry_changes_during_transcript_validation(self):
        source = self.source()
        self.assertIsNotNone(source)
        original = self.native.copy()
        read = claude_session._transcript
        for key, value in (("pid", 201), ("cwd", "/other"), ("sessionId", OTHER),
                           ("procStart", "21"), ("procStart", 20),
                           ("startedAt", original["startedAt"] + 1),
                           ("kind", "background"), ("entrypoint", "sdk")):
            with self.subTest(key=key, value=value):
                self.native = original.copy()
                self.registry()
                def changed(*args):
                    result = read(*args)
                    self.native[key] = value
                    self.registry()
                    return result
                with patch.object(claude_session, "_transcript", side_effect=changed):
                    self.assertFalse(claude_session.current(self.cfg, source))

    def test_current_rejects_foreground_changes_during_transcript_validation(self):
        source = self.source()
        self.assertIsNotNone(source)
        good = self.herdr.foreground[0].copy()
        read = claude_session._transcript
        for foreground in ([], [good, good], [{**good, "pid": 201}],
                           [{**good, "name": "pi"}], [{**good, "cwd": "/other"}]):
            with self.subTest(foreground=foreground):
                self.herdr.foreground = [good.copy()]
                def changed(*args):
                    result = read(*args)
                    self.herdr.foreground = foreground
                    return result
                with patch.object(claude_session, "_transcript", side_effect=changed):
                    self.assertFalse(claude_session.current(self.cfg, source))

    def test_both_readers_reject_same_pid_session_switch_during_final_validation(self):
        self.final()
        source = self.source()
        self.assertIsNotNone(source)
        self.rollout(OTHER, [("other", "Another session's reply.")])
        read = claude_session._transcript
        for reader, empty in ((notify.reply_identity, ("", "")), (notify.relay_messages, [])):
            with self.subTest(reader=reader.__name__):
                self.native["sessionId"] = CURRENT
                self.registry()
                def changed(*args):
                    result = read(*args)
                    self.native["sessionId"] = OTHER
                    self.registry()
                    return result
                with patch.object(claude_session, "_transcript", side_effect=changed):
                    self.assertEqual(reader("claude", {}, self.home, MAIN, cfg=self.cfg,
                                            info=self.pane, claude_source=source), empty)

    def test_notification_same_pid_switch_during_final_check_refuses_send(self):
        self.final()
        source = self.source()
        self.assertIsNotNone(source)
        self.rollout(OTHER, [("other", "Another session's reply.")])
        read = claude_session._transcript
        validating_send = False
        def changed(*args):
            result = read(*args)
            if validating_send:
                self.native["sessionId"] = OTHER
                self.registry()
            return result
        def format_reply(cfg, pane_id, status, text):
            nonlocal validating_send
            validating_send = True
            return text
        with patch.object(claude_session, "_transcript", side_effect=changed), \
                patch.object(notify.firstmate, "calm_excerpt", side_effect=format_reply):
            self.assertFalse(notify.deliver(self.cfg, MAIN, "idle", "working", cli="claude",
                                            claude_source=source))
        self.assertEqual(self.api.sent, [])
        self.assert_no_body()
        self.native["sessionId"] = CURRENT
        self.registry()
        self.assertTrue(self.deliver())
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.texts, [ANSWER])

    def test_watcher_same_pid_switch_during_final_check_retains_boundary(self):
        watch.midrun_pass(self.cfg, self.pane, prime=True)
        baseline = watch._content_last(MAIN)
        self.final()
        self.rollout(OTHER, [("other", "Another session's reply.")])
        read = claude_session._transcript
        validating_send = False
        def changed(*args):
            result = read(*args)
            if validating_send:
                self.native["sessionId"] = OTHER
                self.registry()
            return result
        def format_reply(cfg, pane_id, status, text):
            nonlocal validating_send
            validating_send = True
            return text
        with patch.object(claude_session, "_transcript", side_effect=changed), \
                patch.object(notify.firstmate, "calm_excerpt", side_effect=format_reply):
            self.assertTrue(watch.midrun_pass(self.cfg, self.pane))
        self.assertEqual(watch._content_last(MAIN), baseline)
        self.assertEqual(self.api.sent, [])
        self.assert_no_body()
        self.native["sessionId"] = CURRENT
        self.registry()
        watch.midrun_pass(self.cfg, self.pane)
        watch.midrun_pass(self.cfg, self.pane)
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.texts, [ANSWER])

    def test_ownership_changes_during_read_reject_both_readers(self):
        self.final()
        read = notify.assistant_messages_jsonl
        def changed(path):
            result = read(path)
            self.process(ticks="21")
            return result
        for reader in (notify.reply_identity, notify.relay_messages):
            with self.subTest(reader=reader.__name__):
                self.process()
                with patch.object(notify, "assistant_messages_jsonl", side_effect=changed):
                    result = reader("claude", {}, self.home, MAIN, cfg=self.cfg, info=self.pane)
                self.assertIn(result, [("", ""), []])

    def test_notification_owner_changes_during_settle_refuses_send_and_marker(self):
        self.final()
        def changed(*args):
            self.process(ticks="21")
            return True
        with patch.object(notify, "settled", side_effect=changed):
            self.assertFalse(self.deliver())
        self.assertEqual(self.api.sent, [])
        self.assert_no_body()

    def test_watcher_owner_changes_between_enumeration_and_delivery_retains_boundary(self):
        watch.midrun_pass(self.cfg, self.pane, prime=True)
        baseline = watch._content_last(MAIN)
        self.final()
        deliver = notify.deliver
        def changed(*args, **kwargs):
            self.native["sessionId"] = OTHER
            self.registry()
            self.rollout(OTHER, [("new-owner", "Different owner's reply.")])
            return deliver(*args, **kwargs)
        with patch.object(notify, "deliver", side_effect=changed):
            self.assertTrue(watch.midrun_pass(self.cfg, self.pane))
        self.assertEqual(watch._content_last(MAIN), baseline)
        self.assertEqual(self.api.sent, [])
        self.assert_no_body()
        self.native["sessionId"] = CURRENT
        self.registry()
        watch.midrun_pass(self.cfg, self.pane)
        self.assertEqual(self.api.texts, [ANSWER])

    def test_owner_changes_after_formatting_refuses_send(self):
        self.final()
        def changed(cfg, pane_id, status, text):
            self.pane["agent"] = "pi"
            return text
        with patch.object(notify.firstmate, "calm_excerpt", side_effect=changed):
            self.assertFalse(self.deliver())
        self.assertEqual(self.api.sent, [])
        self.assert_no_body()

    def test_snapshot_rejects_replaced_file_or_explicit_reference_added(self):
        source = self.source()
        self.assertIsNotNone(source)
        replacement = self.path.with_suffix(".replacement")
        replacement.write_bytes(self.path.read_bytes())
        replacement.replace(self.path)
        self.assertFalse(claude_session.current(self.cfg, source))
        source = self.source()
        self.pane["agent_session"] = {"kind": "path", "value": str(self.path)}
        self.assertFalse(claude_session.current(self.cfg, source))

    def test_snapshot_rejects_foreground_pid_and_main_route_changes(self):
        source = self.source()
        self.herdr.foreground[0]["pid"] = 201
        self.assertFalse(claude_session.current(self.cfg, source))
        self.herdr.foreground[0]["pid"] = 200
        self.pane["foreground_cwd"] = "/other"
        self.assertFalse(claude_session.current(self.cfg, source))
        self.pane["foreground_cwd"] = self.home
        self.herdr.agents.append(agent("other:p1", self.home, cli="claude"))
        self.assertFalse(claude_session.current(self.cfg, source))

    def test_unavailable_platform_and_pane_metadata_fail_closed(self):
        with patch.object(claude_session, "PROC", str(self.base / "absent-proc")):
            self.assertIsNone(self.source())
        for key, value in (("agent", "pi"), ("pane_id", WORKER), ("foreground_cwd", "")):
            with self.subTest(key=key):
                old = self.pane[key]
                self.pane[key] = value
                self.assertIsNone(self.source())
                self.pane[key] = old
        self.assertIsNone(claude_session.resolve({}, MAIN, self.pane))

    def test_pi_missing_reference_notification_and_watcher_remain_compatible(self):
        self.pane["agent"] = "pi"
        pi_base = self.base / "pi-sessions"
        encoded = "--" + self.home.lstrip("/").replace("/", "-") + "--"
        directory = pi_base / encoded
        directory.mkdir(parents=True)
        path = directory / "pi.jsonl"
        with patch.object(notify, "PI_SESSIONS", str(pi_base)), \
                patch.object(claude_session, "resolve", side_effect=AssertionError("Pi must not discover Claude")):
            transcript(str(path), "Synthetic request.", ["Previous Pi reply."], ids=["old-pi"])
            watch.midrun_pass(self.cfg, self.pane, prime=True)
            transcript(str(path), "Synthetic request.", ["Previous Pi reply.", "The Pi repair is ready."],
                       ids=["old-pi", "final-pi"])
            watch.midrun_pass(self.cfg, self.pane)
            self.assertFalse(notify.deliver(self.cfg, MAIN, "done", "working", agent="pi", cli="pi"))
            self.assertEqual(self.api.texts, ["The Pi repair is ready."])

    def test_no_transcript_memo_refreshes_when_harness_changes(self):
        (self.sessions / "200.json").unlink()
        with patch.object(tg, "log") as log, patch.object(notify, "PI_SESSIONS", str(self.base / "no-pi")):
            self.pane["agent"] = "pi"
            watch.midrun_pass(self.cfg, self.pane)
            self.pane["agent"] = "claude"
            watch.midrun_pass(self.cfg, self.pane)
            watch.midrun_pass(self.cfg, self.pane)
        self.assertEqual(len([c for c in log.call_args_list if "no transcript" in c.args[0]]), 2)

    def test_no_transcript_memo_refreshes_when_native_source_changes(self):
        self.rollout(CURRENT, [])
        self.rollout(OTHER, [])
        with patch.object(tg, "log") as log:
            watch.midrun_pass(self.cfg, self.pane)
            watch.midrun_pass(self.cfg, self.pane)
            self.native["sessionId"] = OTHER
            self.registry()
            watch.midrun_pass(self.cfg, self.pane)
            watch.midrun_pass(self.cfg, self.pane)
            self.native["startedAt"] += 1
            self.registry()
            watch.midrun_pass(self.cfg, self.pane)
            watch.midrun_pass(self.cfg, self.pane)
        self.assertEqual(len([c for c in log.call_args_list if "no transcript" in c.args[0]]), 3)
        self.assertEqual(self.api.sent, [])

    def test_content_pass_memo_refreshes_when_harness_changes(self):
        cfg = {**self.cfg, "firstmate": {"enabled": False}}
        with patch.object(tg, "log") as log, \
                patch.object(notify, "PI_SESSIONS", str(self.base / "no-pi")), \
                patch.object(claude_session, "resolve", side_effect=AssertionError("discovery disabled")):
            self.pane["agent"] = "pi"
            watch.content_pass(cfg, self.pane)
            watch.content_pass(cfg, self.pane)
            self.pane["agent"] = "claude"
            watch.content_pass(cfg, self.pane)
            watch.content_pass(cfg, self.pane)
        self.assertEqual(len([c for c in log.call_args_list if "no transcript" in c.args[0]]), 2)


if __name__ == "__main__":
    unittest.main()
