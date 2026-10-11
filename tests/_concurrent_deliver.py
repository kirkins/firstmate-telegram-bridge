"""Test driver, not a test: one half of a two-process notify.deliver collision.

Launched twice concurrently by test_double_send.py with one shared
HERDR_PLUGIN_STATE_DIR — the event-hook/watcher arrangement in miniature, two
operating-system processes feeding notify.deliver for the same pane and
message identity. The Telegram transport and herdr are stubbed at the tg
module boundary (the offline suite's contract); the "first" role parks inside
its fake send to hold the check->stamp window wide open and drops a gate file,
so the "second" role provably starts delivering while the first sits between
its marker check and its stamp. Which roles actually reached sendMessage land
in the record file; the parent asserts exactly one line.
"""
import os
import sys
import time

ROLE, STATE, RECORD, GATE, OUTCOME = sys.argv[1:6]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HERDR_PLUGIN_STATE_DIR"] = STATE

import tg
import notify

PANE = "w3:p1"
IDENTITY = "race-msg-1"
TEXT = "One concurrent completion."


def fake_api(cfg, method, params, timeout=20, retries=3):
    if method != "sendMessage":
        return {}, ""
    if ROLE == "first":
        with open(GATE, "w", encoding="utf-8") as g:
            g.write("in-send")          # signal to the parent and the second role
        time.sleep(1.5)                 # hold the check->stamp window wide open
        if OUTCOME == "fail":
            return None, "synthetic send failure"
    with open(RECORD, "a", encoding="utf-8") as r:
        r.write(ROLE + "\n")
    return {"message_id": 4242}, ""


tg.api = fake_api
tg.herdr_json = lambda *args, timeout=30: ({}, "")
tg.herdr_run = lambda *args, timeout=30: ("", "")

CFG = {"chat_id": "100", "statuses": ["done"], "settle_ms": 0,
       "quiet_seconds": 0, "replies": False, "markdown": False}

if ROLE == "second":
    while not os.path.exists(GATE):
        time.sleep(0.01)

sent = notify.deliver(CFG, PANE, "done", "working", agent="pi", cli="pi",
                      origin="content", content_id=IDENTITY, content_text=TEXT)
print(f"RESULT role={ROLE} delivered={sent}")
