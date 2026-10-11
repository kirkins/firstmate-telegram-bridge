"""Regression tests for the two audit fixes that ship unconditionally:

F2 — a forced curl timeout (subprocess.TimeoutExpired's repr embeds the full argv,
     token-bearing URL included) must never reach the returned error string or
     bridge.log: api() redacts at the source, so every caller is covered.
F3 — a markdown link URL containing a double quote must not break out of the href
     attribute; it is entity-escaped instead.
"""

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import _env  # noqa: F401  (env must be set before tg imports)
import bot
import tg
from _env import CHAT, STATE_DIR, FakeApi, reset_state, write_config

TOKEN = "1234567890:AAEecret_TOKEN-value"
CFG = {"bot_token": TOKEN, "chat_id": "100"}


def log_text():
    try:
        with open(os.path.join(STATE_DIR, "bridge.log"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def local_black_hole():
    """A loopback base URL to a port nothing listens on: curl and urllib both
    fail instantly with connection refused — offline, deterministic, no real
    network request ever leaves the machine."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


class TestTokenRedaction(unittest.TestCase):
    def setUp(self):
        try:
            os.remove(os.path.join(STATE_DIR, "bridge.log"))
        except FileNotFoundError:
            pass

    def test_redact_helper(self):
        self.assertEqual(tg.redact(f"boom {TOKEN} boom", CFG), "boom <token> boom")
        self.assertEqual(tg.redact("no token here", {"bot_token": ""}), "no token here")

    def test_forced_curl_timeout_leaves_error_and_log_token_free(self):
        """The F2 reproduction: TimeoutExpired's repr embeds the full curl argv."""
        real_run = subprocess.run

        def hang_then_raise(cmd, **kwargs):
            if cmd and cmd[0] == "curl":
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 20))
            return real_run(cmd, **kwargs)

        with mock.patch("subprocess.run", side_effect=hang_then_raise), \
             mock.patch("urllib.request.urlopen",
                        side_effect=OSError("connection refused")):
            result, err = tg.api(CFG, "sendMessage",
                                 {"chat_id": 100, "text": "hello"}, timeout=5, retries=2)
        tg.log(f"bot: send failed: {err}")   # exactly what the bot.py caller does

        self.assertIsNone(result)
        self.assertNotIn(TOKEN, err)
        # the token no longer rides curl's argv at all (it lives in a 0600 -K
        # config file), so TimeoutExpired's repr has nothing sensitive left to
        # redact — token-free outright, which is the stronger F2 outcome
        self.assertNotIn(TOKEN, log_text())
        self.assertIn("curl", err)                     # the failure is still reported

    def test_real_curl_failure_also_stays_token_free(self):
        """A genuine curl attempt against a local black hole (accepts, never
        answers) exits on its own -m deadline; the error string and log stay clean."""
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def swallow():
            try:
                conn, _ = server.accept()
                conn.recv(1024)          # accept the request, never respond
                conn.close()
            except OSError:
                pass
            finally:
                server.close()

        thread = threading.Thread(target=swallow, daemon=True)
        thread.start()
        cfg = {**CFG, "api_base": f"http://127.0.0.1:{port}"}
        try:
            _, err = tg.api(cfg, "sendMessage", {"chat_id": 100, "text": "x"},
                            timeout=1, retries=1)
        finally:
            thread.join(timeout=5)
        self.assertNotIn(TOKEN, err)
        self.assertNotIn(TOKEN, log_text())

    def test_timeoutexpired_repr_really_does_embed_the_token(self):
        """Guard the guard: if a future Python stops embedding argv in the repr,
        the redaction above is still wanted (urllib errors embed the URL too)."""
        argv = ["curl", "-sm", "20", f"https://api.telegram.org/bot{TOKEN}/sendMessage"]
        self.assertIn(TOKEN, repr(subprocess.TimeoutExpired(argv, 20)))


class TestRunBounded(unittest.TestCase):
    """The stall fix: a subprocess call in the watch path must always come back.
    subprocess.run(timeout=...) kills only the direct child and then waits for the
    output pipes to drain — a herdr helper that inherited them keeps the write end
    open, so the \"timed out\" call never returns and the watch thread freezes with
    the process alive. run_bounded group-kills on expiry and abandons pipes held
    by an escaped descendant instead of waiting on them."""

    def test_plain_run_returns_output(self):
        rc, out, err, failure = tg.run_bounded(["echo", "hi"], 10)
        self.assertEqual((rc, out, failure), (0, b"hi\n", ""))

    def test_timeout_kills_and_returns(self):
        started = time.monotonic()
        rc, out, err, failure = tg.run_bounded(["sleep", "30"], 0.3, grace=5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("timeout", failure)
        self.assertNotEqual(rc, 0)

    def test_herdr_run_cannot_hang_past_its_timeout(self):
        # the herdr CLI itself is the freeze suspect; pin the seam without one
        with mock.patch.dict(os.environ, {"HERDR_BIN_PATH": "/bin/sleep"}):
            started = time.monotonic()
            out, err = tg.herdr_run("30", timeout=0.3)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(out, "")
        self.assertIn("timeout", err)

    @unittest.skipUnless(shutil.which("bash") and shutil.which("setsid"),
                         "needs bash + setsid to build an escaped pipe holder")
    def test_descendant_holding_the_pipes_cannot_extend_the_wait(self):
        # the direct child dies on schedule, but a setsid'd grandchild keeps the
        # inherited stdout write end open: plain subprocess.run would block on it
        # forever despite its timeout — run_bounded gives up at the grace bound
        cmd = ["bash", "-c", "setsid sh -c 'sleep 3' & exec sleep 3"]
        started = time.monotonic()
        rc, out, err, failure = tg.run_bounded(cmd, 0.3, grace=0.5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("timeout", failure)
        self.assertIn("pipes", failure)


class TestHrefQuoteEscaping(unittest.TestCase):
    def test_quote_in_link_url_is_entity_escaped(self):
        html = tg.md_to_html('[x](https://e/"onload="y)')
        self.assertIn("https://e/&quot;onload=&quot;y", html)
        self.assertNotIn('"onload', html)

    def test_ordinary_links_unchanged(self):
        self.assertEqual(tg.md_to_html("[a](https://ok.io/path)"),
                         '<a href="https://ok.io/path">a</a>')

    def test_ampersand_url_still_escapes(self):
        html = tg.md_to_html("[q](https://e/?a=1&b=2)")
        self.assertIn("https://e/?a=1&amp;b=2", html)


class TestTokenFreeArgv(unittest.TestCase):
    """The token never rides a command line — not into secret-tool, and not
    into curl either: the token-bearing URL travels in a 0600 -K config file
    (created and deleted around the call), so a same-user ps/proc snoop sees
    only a throwaway path."""

    def setUp(self):
        self.seen_cmds = []
        self.seen_files = {}
        real_run = subprocess.run

        def spy(cmd, **kwargs):
            self.seen_cmds.append(cmd)
            if "-K" in cmd:
                path = cmd[cmd.index("-K") + 1]
                with open(path, encoding="utf-8") as f:
                    self.seen_files[path] = f.read()
                self.mode = oct(os.stat(path).st_mode & 0o777)
            return real_run(cmd, **kwargs)

        patcher = mock.patch("subprocess.run", side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.real_run = real_run

    def test_api_never_puts_the_token_in_argv(self):
        cfg = {**CFG, "api_base": local_black_hole()}
        result, err = tg.api(cfg, "sendMessage", {"chat_id": 100, "text": "x"},
                             timeout=5, retries=1)
        for cmd in self.seen_cmds:
            self.assertNotIn(TOKEN, " ".join(str(c) for c in cmd))
        urlfile = next(iter(self.seen_files))
        self.assertIn(f"bot{TOKEN}/sendMessage", self.seen_files[urlfile])
        self.assertEqual(self.mode, "0o600")
        self.assertFalse(os.path.exists(urlfile))          # swept after the call

    def test_download_never_puts_the_token_in_argv(self):
        dest = os.path.join(STATE_DIR, "dl-test.bin")
        self.addCleanup(lambda: os.path.exists(dest) and os.remove(dest))
        # curl fails (connection refused on loopback) — argv hygiene holds either way
        tg.download({**CFG, "api_base": local_black_hole()}, "docs/file.txt", dest)
        for cmd in self.seen_cmds:
            self.assertNotIn(TOKEN, " ".join(str(c) for c in cmd))
        urlfile = next(iter(self.seen_files))
        self.assertIn(f"bot{TOKEN}/docs/file.txt", self.seen_files[urlfile])
        self.assertFalse(os.path.exists(urlfile))


class TestStateDirAutoCreate(unittest.TestCase):
    """The day-one announce path: on a clean setup nothing logs before the
    first send, so the curl config file itself must ensure the state directory
    exists — a missing directory would crash the send with a traceback instead
    of a clean result or error report."""

    def test_api_creates_a_missing_state_dir_instead_of_raising(self):
        base = tempfile.mkdtemp(prefix="fm-statedir-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        missing = os.path.join(base, "state")
        cfg = {**CFG, "api_base": local_black_hole()}
        with mock.patch.object(tg, "STATE_DIR", missing):
            result, err = tg.api(cfg, "sendMessage", {"chat_id": 100, "text": "x"},
                                 timeout=2, retries=1)
        self.assertIsNone(result)
        self.assertTrue(err)                        # reported, not raised
        self.assertTrue(os.path.isdir(missing))


class TestSecretToolToken(unittest.TestCase):
    """bot_token_secret: the token lives in the Secret Service — never in
    config.toml, a command line, or a log line. load_config resolves it through
    secret-tool (attributes on argv, secret on stdout); a literal bot_token
    wins; an unresolvable secret degrades loudly, exactly like an unset one."""

    SPEC = {"service": "herdr-telegram-bridge", "account": "bot_token"}

    def setUp(self):
        try:
            os.remove(os.path.join(STATE_DIR, "bridge.log"))
        except FileNotFoundError:
            pass

    def stub(self, stdout=b"", code=0, error=""):
        seen = []

        def fake_run_bounded(cmd, timeout):
            seen.append(cmd)
            return code, stdout, b"", error

        return seen, fake_run_bounded

    def test_secret_spec_resolves_into_the_config(self):
        seen, fake = self.stub(stdout=(TOKEN + "\n").encode())
        with mock.patch.object(tg, "run_bounded", side_effect=fake):
            cfg = tg.resolve_token({"chat_id": "100", "bot_token_secret": self.SPEC})
        self.assertEqual(cfg["bot_token"], TOKEN)
        self.assertEqual(seen, [["secret-tool", "lookup",
                                 "service", "herdr-telegram-bridge",
                                 "account", "bot_token"]])

    def test_literal_bot_token_wins_over_the_secret(self):
        seen, fake = self.stub(stdout=b"SHOULD-NOT-APPEAR\n")
        with mock.patch.object(tg, "run_bounded", side_effect=fake):
            cfg = tg.resolve_token({"chat_id": "100", "bot_token": TOKEN,
                                    "bot_token_secret": self.SPEC})
        self.assertEqual(cfg["bot_token"], TOKEN)
        self.assertEqual(seen, [])                     # secret-tool never invoked

    def test_unresolvable_secret_degrades_loudly_and_token_free(self):
        _seen, fake = self.stub(stdout=b"", code=1, error="")
        with mock.patch.object(tg, "run_bounded", side_effect=fake):
            cfg = tg.resolve_token({"chat_id": "100", "bot_token_secret": self.SPEC})
        self.assertEqual(cfg.get("bot_token", ""), "")
        log = log_text()
        self.assertIn("did not resolve", log)
        self.assertNotIn(TOKEN, log)

    def test_empty_secret_stays_unset_and_logs(self):
        _seen, fake = self.stub(stdout=b"\n")
        with mock.patch.object(tg, "run_bounded", side_effect=fake):
            cfg = tg.resolve_token({"chat_id": "100", "bot_token_secret": self.SPEC})
        self.assertEqual(cfg.get("bot_token", ""), "")
        self.assertIn("did not resolve", log_text())

    def test_no_spec_no_lookup(self):
        seen, fake = self.stub(stdout=b"SHOULD-NOT-APPEAR\n")
        with mock.patch.object(tg, "run_bounded", side_effect=fake):
            cfg = tg.resolve_token({"chat_id": "100"})
        self.assertNotIn("bot_token", cfg)
        self.assertEqual(seen, [])                     # upstream shape untouched

    def test_load_config_wires_the_secret_in(self):
        from _env import write_config
        write_config({"chat_id": "100", "bot_token_secret": self.SPEC})
        _seen, fake = self.stub(stdout=(TOKEN + "\n").encode())
        with mock.patch.object(tg, "run_bounded", side_effect=fake):
            cfg = tg.load_config()
        self.assertEqual(cfg["bot_token"], TOKEN)


class TestAnnounce(unittest.TestCase):
    """bot.py --announce: one message to the configured chat through the same
    send plumbing (tg.api / bot.say), no target recorded — it is nobody's reply
    hook. The outcome is reported so the live smoke test can fail loudly."""

    def setUp(self):
        reset_state()
        write_config({"chat_id": str(CHAT), "bot_token": "T0K:EN"})

    def test_announce_sends_to_the_configured_chat(self):
        api = FakeApi()
        with mock.patch.object(tg, "api", api), \
             mock.patch("builtins.print") as printed:
            bot.announce("bridge connected — switchable via systemctl --user")
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(api.sent[0]["chat_id"], CHAT)
        self.assertIn("bridge connected", api.sent[0]["text"])
        self.assertEqual(tg.read_state(tg.MSG_MAP, {}), {})  # no reply hook
        printed.assert_called_once()
        self.assertIn("announced", printed.call_args[0][0])

    def test_announce_failure_is_reported_not_swallowed(self):
        api = FakeApi()
        api.fail_send = "telegram boom"
        with mock.patch.object(tg, "api", api), \
             mock.patch("builtins.print") as printed:
            bot.announce("will fail")
        self.assertEqual(api.sent, [])
        printed.assert_called_once()
        self.assertIn("announce failed", printed.call_args[0][0])

    def test_announce_without_chat_id_refuses(self):
        write_config({"chat_id": "REPLACE_ME", "bot_token": "T0K:EN"})
        api = FakeApi()
        with mock.patch.object(tg, "api", api), \
             mock.patch("builtins.print") as printed:
            bot.announce("nowhere to go")
        self.assertEqual(api.sent, [])
        printed.assert_called_once()
        self.assertIn("chat_id not set", printed.call_args[0][0])


class TestConfigExampleSanity(unittest.TestCase):
    """The example config must stay loadable TOML (guards the [firstmate] block)."""

    def test_example_config_parses_and_firstmate_defaults_on(self):
        import tomllib
        with open(os.path.join(_env.ROOT, "config.example.toml"), "rb") as f:
            cfg = tomllib.load(f)
        self.assertTrue(cfg["firstmate"]["enabled"])
        self.assertNotIn("worker_notify", cfg["firstmate"])  # removed: main-only is structural
        self.assertFalse(cfg["firstmate"]["allow_worker_esc"])
        self.assertIn("main_cwd", cfg["firstmate"])


if __name__ == "__main__":
    unittest.main()
