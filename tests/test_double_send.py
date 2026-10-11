"""Cross-process double-send regression (issue #15): the event hook
(notify.main, its own herdr plugin process) and the watcher (bot.py's
status-watcher thread) both deliver completions through notify.deliver, and
the sent-marker used to be read, then sent under, then stamped — an
unserialized check-then-act window spanning the whole Telegram HTTP send, so
both processes could pass the check for one message identity and the phone
heard the completion twice (voice mode spoke it twice).

Two real subprocesses race for the same pane and identity, the second held at
the gate until the first is provably inside its fake send — the check/send
boundary, interleaved deterministically. Exactly one Telegram send may come
out of the pair, and a failed first send must not suppress the waiting second:
it delivers once the lock falls, which is the retry semantics preserved.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import _env  # noqa: F401  (env must be set before tg imports)
import tg

DRIVER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "_concurrent_deliver.py")
PANE = "w3:p1"
IDENTITY = "race-msg-1"


class TestConcurrentDeliver(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="fm-race-")
        self.state = os.path.join(self.base, "state")   # shared by both roles
        os.makedirs(self.state)
        self.record = os.path.join(self.base, "record")  # who sent to Telegram
        self.gate = os.path.join(self.base, "gate")      # first is in-send

    def run_pair(self, first_outcome):
        """Launch both roles at once -> {role: delivered?}, in role order."""
        env = {**os.environ, "HERDR_PLUGIN_STATE_DIR": self.state}
        procs = {role: subprocess.Popen(
                     [sys.executable, DRIVER, role, self.state, self.record,
                      self.gate, first_outcome],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                     cwd=_env.ROOT, env=env)
                 for role in ("first", "second")}
        results = {}
        for role, proc in procs.items():
            out, err = proc.communicate(timeout=90)
            self.assertEqual(proc.returncode, 0, f"{role}: {err or out}")
            results[role] = out.strip().rsplit("delivered=", 1)[1]
        return results

    def senders(self):
        with open(self.record, encoding="utf-8") as f:
            return f.read().split()

    def sent_marker(self):
        with open(os.path.join(self.state, "content_sent.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def test_both_processes_deliver_same_identity_exactly_once(self):
        results = self.run_pair("ok")
        self.assertEqual(self.senders(), ["first"])
        self.assertEqual(results, {"first": "True", "second": "False"})
        self.assertIn(f"{PANE}:{IDENTITY}", self.sent_marker())

    def test_failed_send_does_not_suppress_the_waiting_delivery(self):
        results = self.run_pair("fail")
        self.assertEqual(self.senders(), ["second"])
        self.assertEqual(results, {"first": "False", "second": "True"})
        self.assertIn(f"{PANE}:{IDENTITY}", self.sent_marker())


class TestLockMechanics(unittest.TestCase):
    """The flock plumbing the delivery lock rides on: pruning may only remove
    lock files that are old and currently unlocked — never one a live delivery
    holds, and never one so fresh its delivery may still be starting."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fm-locks-")

    def age(self, name, secs):
        path = os.path.join(self.dir, name)
        old = time.time() - secs
        os.utime(path, (old, old))

    def test_prune_removes_only_old_unlocked_lock_files(self):
        stale = os.path.join(self.dir, "stale.lock")   # old, unlocked: removed
        fresh = os.path.join(self.dir, "fresh.lock")   # just created: kept
        held = os.path.join(self.dir, "held.lock")     # old but locked: kept
        for path in (stale, fresh, held):
            with open(path, "w", encoding="utf-8"):
                pass
        self.age("stale.lock", 7200)
        self.age("held.lock", 7200)
        fd = tg.lock_ex(held)
        try:
            tg.prune_lock_dir(self.dir, max_files=2, idle_secs=3600)
            self.assertFalse(os.path.exists(stale))
            self.assertTrue(os.path.exists(fresh))
            self.assertTrue(os.path.exists(held))
        finally:
            tg.unlock(fd)


if __name__ == "__main__":
    unittest.main()
