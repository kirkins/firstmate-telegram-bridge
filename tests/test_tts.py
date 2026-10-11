"""Offline voice-output/TTS tests: every subprocess (curl, ffmpeg, ffprobe) is
stubbed at the tts._run seam, herdr and the Telegram API at the tg module
boundary as everywhere else in this suite. The contract under test: /voice
toggles a state file and reports unavailability instead of a silent no-op, the
synthesis request carries the verified provider shapes with the key out of
argv, markdown is stripped into speakable prose, every reply is spoken in full
(chunked on sentence boundaries into provider-sized requests and assembled
into ONE Ogg/Opus note), non-Ogg audio converts through ffmpeg, the voice note
rides AFTER the text push without ever gating it, and every failure degrades
to the text-only reply with a rate-limited chat warning. No network, no real
key, no ffmpeg."""

import json
import os
import tempfile
import unittest

import _env  # noqa: F401  (env must be set before tg/bot imports)
import bot
import notify
import tg
import tts
from _env import (CHAT, MAIN, USER, FakeApi, FakeHerdr, agent, reset_state,
                  transcript)

TTS_KEY = "sk-test_0123456789"
CF_KEY = "cf_test_9876543210"
CF_ACCOUNT = "acct0123456789abcdef0123456789abcd"
OPENAI_URL = "https://api.openai.com/v1/audio/speech"
CF_URL = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}"
          "/ai/run/@cf/myshell-ai/melotts")

REAL_RUN = tts._run
REAL_SEND_VOICE = tg.send_voice

OGG = (b"OggS" + b"\x00" * 60)  # magic-prefixed stand-in for real Opus audio


def run_result(meta=b"200 audio/ogg\n", err=b"", failure="", rc=0):
    return rc, meta, err, failure


class FakeRun:
    """Stands in for tts._run (tg.run_bounded): dispatches on argv[0] —
    curl writes the audio body to its -o file and puts http+content-type on
    stdout, ffmpeg writes an ogg to argv[-1], ffprobe prints a duration."""

    def __init__(self, body=OGG, meta=b"200 audio/ogg", rc=0, stderr=b"",
                 ffmpeg_ok=True, duration=b"6.0\n", curl_exc_failure="",
                 curl_fail_on=None):
        self.body, self.meta, self.rc = body, meta, rc
        self.stderr, self.ffmpeg_ok = stderr, ffmpeg_ok
        self.duration, self.curl_exc_failure = duration, ffmpeg_ok and "" or ""
        self.argvs = []
        self.headers = []
        self.header_modes = []
        self.bodies = []
        self.failures = curl_exc_failure
        self.curl_fail_on = curl_fail_on   # 0-based curl-call index to fail
        self._curl_calls = 0

    def __call__(self, cmd, timeout):
        self.argvs.append(list(cmd))
        if cmd[0] == "curl":
            n, self._curl_calls = self._curl_calls, self._curl_calls + 1
            path = cmd[cmd.index("-o") + 1]
            with open(path, "wb") as f:
                f.write(self.body)
            hdr = cmd[cmd.index("-H") + 1]
            assert hdr.startswith("@"), "auth header must come from a file, not argv"
            with open(hdr[1:], encoding="utf-8") as f:
                self.headers.append(f.read())
            self.header_modes.append(os.stat(hdr[1:]).st_mode & 0o777)
            bodyf = cmd[cmd.index("-d") + 1]
            with open(bodyf[1:], encoding="utf-8") as f:
                self.bodies.append(json.load(f))
            if self.failures:
                return -1, b"", b"", self.failures
            if self.curl_fail_on is not None and n == self.curl_fail_on:
                return -1, b"", b"", f"timeout after 45s (curl call {n})"
            return self.rc, self.meta, self.stderr, ""
        if cmd[0] == "ffmpeg":
            if not self.ffmpeg_ok:
                return 1, b"", b"boom: no libopus", ""
            with open(cmd[-1], "wb") as f:
                f.write(OGG)
            return 0, b"", b"", ""
        if cmd[0] == "ffprobe":
            return 0, self.duration, b"", ""
        raise AssertionError(f"unexpected subprocess {cmd[0]!r}")


class FakeVoice:
    """Stands in for tg.send_voice at the module boundary."""

    def __init__(self, err=""):
        self.calls = []
        self.err = err

    def __call__(self, cfg, path, duration=0):
        self.calls.append({"path": path, "duration": duration})
        return ({"message_id": 777}, "") if not self.err else (None, self.err)


class TtsCase(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.runner = FakeRun()
        self.voice = FakeVoice()
        tts._run = self.runner
        tg.send_voice = self.voice
        self.addCleanup(setattr, tts, "_run", REAL_RUN)
        self.addCleanup(setattr, tg, "send_voice", REAL_SEND_VOICE)

    def cfg(self, provider="openai", **opts):
        section = {"provider": provider, "api_key": TTS_KEY, **opts}
        if provider == "cloudflare":
            section.setdefault("account_id", CF_ACCOUNT)
            section.setdefault("api_key", CF_KEY)
        return {"chat_id": str(CHAT), "bot_token": "T0K:EN", "tts": section}


class TestConfig(TtsCase):
    def test_absent_section_is_unconfigured(self):
        cfg = {"chat_id": str(CHAT)}
        self.assertFalse(tts.configured(cfg))
        self.assertIn("[tts] section", tts.unavailability(cfg))

    def test_disabled_section_is_unconfigured(self):
        self.assertFalse(tts.configured(self.cfg(enabled=False)))

    def test_openai_needs_only_a_key(self):
        self.assertEqual(tts.config_error(self.cfg()), "")

    def test_cloudflare_needs_account_id(self):
        cfg = self.cfg("cloudflare")
        cfg["tts"].pop("account_id")
        self.assertEqual(tts.config_error(cfg),
                         "tts.account_id is not set (cloudflare)")
        cfg["tts"]["account_id"] = "REPLACE_ME"
        self.assertEqual(tts.config_error(cfg),
                         "tts.account_id is not set (cloudflare)")
        self.assertEqual(tts.synthesize(cfg, "x")[2],
                         "tts.account_id is not set (cloudflare)")
        self.assertEqual(self.runner.argvs, [])

    def test_unknown_provider_refused(self):
        self.assertIn('unknown tts provider "azure"',
                      tts.config_error(self.cfg(provider="azure")))

    def test_placeholder_key_counts_as_unset(self):
        self.assertIn("api_key is not set", tts.config_error(self.cfg(api_key="REPLACE_ME:sk")))

    def test_api_key_secret_resolves_and_caches(self):
        seen = []

        def lookup(spec):
            seen.append(spec)
            return "sekrit-from-keyring", ""

        real = tg.secret_tool_lookup
        tg.secret_tool_lookup = lookup
        try:
            cfg = self.cfg()
            del cfg["tts"]["api_key"]
            cfg["tts"]["api_key_secret"] = {"service": "bridge", "account": "tts"}
            self.assertEqual(tts.api_key(cfg), "sekrit-from-keyring")
            self.assertEqual(tts.api_key(cfg), "sekrit-from-keyring")
            self.assertEqual(len(seen), 1)  # one secret-tool run, then cached
        finally:
            tg.secret_tool_lookup = real

    def test_startup_warning_only_when_mode_on_and_broken(self):
        self.assertEqual(tts.startup_warnings(self.cfg()), [])
        tts.set_voice_mode(True)
        self.assertEqual(len(tts.startup_warnings(self.cfg())), 0)
        broken = {"chat_id": str(CHAT), "tts": {"enabled": True}}
        warns = tts.startup_warnings(broken)
        self.assertEqual(len(warns), 1)
        self.assertIn("api_key is not set", warns[0])
        self.assertIn("text-only", warns[0])


class TestVoiceCommand(TtsCase):
    def setUp(self):
        super().setUp()
        self.api = FakeApi()
        self.herdr = FakeHerdr([])
        tg.api = self.api
        tg.herdr_run = self.herdr.run
        tg.herdr_json = self.herdr.json
        self.addCleanup(setattr, tg, "api", _env.REAL_API)
        self.addCleanup(setattr, tg, "herdr_run", _env.REAL_HERDR_RUN)
        self.addCleanup(setattr, tg, "herdr_json", _env.REAL_HERDR_JSON)

    def command(self, cfg, text):
        bot.handle_message(cfg, {"chat": {"id": CHAT}, "from": {"id": USER},
                                 "message_id": 9, "text": text})
        return self.api.texts[-1]

    def test_toggle_on_reports_and_persists(self):
        reply = self.command(self.cfg(), "/voice")
        self.assertIn("🔊", reply)
        self.assertTrue(tts.voice_mode())

    def test_toggle_again_turns_off(self):
        self.command(self.cfg(), "/voice")
        reply = self.command(self.cfg(), "/voice")
        self.assertIn("🔇", reply)
        self.assertFalse(tts.voice_mode())

    def test_explicit_on_off_status(self):
        self.command(self.cfg(), "/voice off")
        self.assertFalse(tts.voice_mode())
        self.assertIn("off", self.command(self.cfg(), "/voice status"))
        self.assertIn("🔊", self.command(self.cfg(), "/voice on"))

    def test_turning_on_without_config_reports_why(self):
        reply = self.command({"chat_id": str(CHAT), "bot_token": "T0K:EN"}, "/voice on")
        self.assertIn("unavailable", reply)
        self.assertIn("[tts]", reply)
        self.assertFalse(tts.voice_mode())

    def test_turning_off_always_works_without_config(self):
        reply = self.command({"chat_id": str(CHAT)}, "/voice off")
        self.assertIn("🔇", reply)

    def test_unknown_argument_is_usage_not_state_change(self):
        reply = self.command(self.cfg(), "/voice loudest")
        self.assertIn("/voice on | off | status", reply)
        self.assertFalse(tts.voice_mode())

    def test_command_published_in_menu(self):
        self.assertIn("voice", [c["command"] for c in bot.COMMANDS])
        self.assertIn("/voice", bot.HELP)


class TestSynthesize(TtsCase):
    def test_openai_request_shape(self):
        cfg = self.cfg()
        data, fmt, err = tts.synthesize(cfg, "hello there")
        self.assertEqual((err, fmt), ("", "opus"))
        self.assertEqual(data, OGG)
        cmd = self.runner.argvs[0]
        self.assertIn(OPENAI_URL, cmd)
        body = self.runner.bodies[0]
        self.assertEqual(body, {"model": "gpt-4o-mini-tts", "input": "hello there",
                                "voice": "alloy", "response_format": "opus"})

    def test_openai_overrides_forwarded(self):
        cfg = self.cfg(model="tts-1-hd", voice="shimmer",
                       response_format="mp3", base_url="https://gw.example/v1/")
        tts.synthesize(cfg, "x")
        cmd = self.runner.argvs[0]
        self.assertIn("https://gw.example/v1/audio/speech", cmd)
        self.assertEqual(self.runner.bodies[0]["model"], "tts-1-hd")
        self.assertEqual(self.runner.bodies[0]["voice"], "shimmer")
        self.assertEqual(self.runner.bodies[0]["response_format"], "mp3")

    def test_cloudflare_native_shape_with_both_input_fields(self):
        cfg = self.cfg("cloudflare")
        data, fmt, err = tts.synthesize(cfg, "cloudflare hello")
        self.assertEqual((err, fmt), ("", "opus"))
        cmd = self.runner.argvs[0]
        self.assertIn(CF_URL, cmd)
        body = self.runner.bodies[0]
        self.assertEqual(body, {"text": "cloudflare hello", "prompt": "cloudflare hello"})

    def test_cloudflare_voice_sent_only_when_configured(self):
        self.cfg("cloudflare")
        cfg = self.cfg("cloudflare", voice="thalia")
        tts.synthesize(cfg, "x")
        self.assertEqual(self.runner.bodies[0].get("voice"), "thalia")

    def test_key_rides_in_a_0600_header_file_never_argv(self):
        tts.synthesize(self.cfg(), "x")
        self.assertEqual(self.runner.headers, [f"Authorization: Bearer {TTS_KEY}\n"])
        self.assertEqual(self.runner.header_modes, [0o600])
        self.assertNotIn(TTS_KEY, " ".join(self.runner.argvs[0]))
        for argv in self.runner.argvs:
            for a in argv:
                self.assertNotIn(TTS_KEY, a)

    def test_cloudflare_json_envelope_decodes_to_wav(self):
        wav = b"RIFF" + b"\x00" * 20
        import base64
        self.runner.meta = b"200 application/json"
        self.runner.body = json.dumps({"result": {"audio": base64.b64encode(wav).decode()},
                                       "success": True}).encode()
        data, fmt, err = tts.synthesize(self.cfg("cloudflare"), "x")
        self.assertEqual((err, fmt), ("", "wav"))
        self.assertEqual(data, wav)

    def test_mp3_content_type_comes_back_as_mp3(self):
        self.runner.meta = b"200 audio/mpeg"
        self.runner.body = b"\xff\xf3" + b"\x00" * 30
        data, fmt, err = tts.synthesize(self.cfg("cloudflare"), "x")
        self.assertEqual((err, fmt), ("", "mpeg"))
        self.assertEqual(data[:2], b"\xff\xf3")

    def test_http_error_message_openai_shape(self):
        self.runner.meta = b"401 application/json"
        self.runner.body = b'{"error":{"message":"Invalid API key"}}'
        _, _, err = tts.synthesize(self.cfg(), "x")
        self.assertIn("http 401", err)
        self.assertIn("Invalid API key", err)
        self.assertNotIn(TTS_KEY, err)

    def test_http_error_message_cloudflare_shape(self):
        self.runner.meta = b"400 application/json"
        self.runner.body = b'{"errors":[{"code":7000,"message":"No route for that URI"}]}'
        _, _, err = tts.synthesize(self.cfg("cloudflare"), "x")
        self.assertIn("No route for that URI", err)

    def test_error_body_key_material_redacted(self):
        self.runner.meta = b"403 application/json"
        self.runner.body = f'{{"error":{{"message":"key {TTS_KEY} rejected"}}}}'.encode()
        _, _, err = tts.synthesize(self.cfg(), "x")
        self.assertNotIn(TTS_KEY, err)
        self.assertIn("<tts-key>", err)

    def test_curl_exit_and_failure_strings(self):
        self.runner.rc, self.runner.stderr = 7, b"Failed to connect"
        _, _, err = tts.synthesize(self.cfg(), "x")
        self.assertTrue(err.startswith("curl exit=7"))
        self.runner.rc, self.runner.stderr = 0, b""
        self.runner.failures = "timeout after 45s (a descendant held the pipes)"
        _, _, err = tts.synthesize(self.cfg(), "x")
        self.assertIn("timeout after 45s", err)
        self.assertEqual(tts.error_class(err), "network timeout")

    def test_json_without_audio_is_an_error(self):
        self.runner.meta = b"200 application/json"
        self.runner.body = b'{"result":{}}'
        _, _, err = tts.synthesize(self.cfg("cloudflare"), "x")
        self.assertIn("no audio", err)

    def test_error_classes(self):
        cases = {"tts.api_key is not set": "no tts api_key configured",
                 "tts.account_id is not set (cloudflare)": "no cloudflare account_id configured",
                 'unknown tts provider "x"': "unsupported tts provider",
                 "ffmpeg failed: boom": "audio conversion failed (is ffmpeg installed?)",
                 "http 401: nope": "the TTS API rejected the key",
                 "http 429: slow down": "TTS rate limit",
                 "http 400: bad model": "the TTS API rejected the request",
                 "http 500: boom": "TTS server error",
                 "curl exit=7 x": "network error",
                 "telegram: bad voice": "Telegram refused the voice note",
                 "something else": "speech failed"}
        for err, want in cases.items():
            self.assertEqual(tts.error_class(err), want)


class TestSpeechText(TtsCase):
    def test_markdown_stripped_to_prose(self):
        md = ("## Status report\n\n"
              "The **bridge** is _fine_. See [the docs](https://example.com) "
              "and run `bot.py --status`.\n"
              "- first point\n"
              "- second point\n"
              "> a quoted line\n")
        out = tts.speech_text(md)
        self.assertNotIn("#", out)
        self.assertNotIn("**", out)
        self.assertNotIn("[", out)
        self.assertNotIn("`", out)
        self.assertNotIn("- ", out)
        self.assertNotIn(">", out)
        self.assertIn("Status report", out)
        self.assertIn("the docs", out)
        self.assertIn("bot.py --status", out)
        self.assertIn("first point", out)

    def test_code_and_tables_become_spoken_placeholders(self):
        md = "Here is the plan.\n\n```python\nprint('hi')\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nDone."
        out = tts.speech_text(md)
        self.assertIn("Here is the plan.", out)
        self.assertIn("Code omitted.", out)
        self.assertIn("Table omitted.", out)
        self.assertIn("Done.", out)
        self.assertNotIn("print", out)
        self.assertNotIn("|", out)

    def test_code_only_reply_has_nothing_to_say(self):
        self.assertEqual(tts.speech_text("```bash\nls -la\n```"), "")

    def test_image_alt_survives(self):
        self.assertIn("a chart", tts.speech_text("![a chart](x.png)"))
        self.assertIn("image", tts.speech_text("![](x.png)"))

    def test_word_internal_marks_survive(self):
        self.assertIn("5*3", tts.speech_text("compute 5*3 please"))
        self.assertIn("snake_case", tts.speech_text("use snake_case names"))

    def test_long_text_capped_on_sentence_boundary(self):
        text = "Sentence one is here. " * 100  # 2200 chars
        out = tts.speech_text(text, 500)
        self.assertLessEqual(len(out), 500)
        self.assertTrue(out.endswith((".", "!", "?")), out[-20:])

    def test_cap_without_boundary_hard_cuts(self):
        out = tts.speech_text("word " * 400, 500)
        self.assertLessEqual(len(out), 500)

    def test_whitespace_collapses(self):
        self.assertEqual(tts.speech_text("a\n\n  b\t c  ."), "a b c .")

    def test_default_is_uncapped_no_matter_the_length(self):
        text = "Sentence one is here. " * 400    # 8800 chars
        self.assertEqual(len(tts.speech_text(text)), len(" ".join(text.split())))
        self.assertEqual(tts.speech_text(text), tts.speech_text(text, 0))


class TestSpeechChunks(TtsCase):
    """speech_chunks: the whole reply is always spoken — chunking only decides
    where one synthesis request ends and the next begins, never what is said."""

    def test_short_text_is_one_chunk(self):
        self.assertEqual(tts.speech_chunks("Hello there. Short.", 100),
                         ["Hello there. Short."])

    def test_empty_text_has_no_chunks(self):
        self.assertEqual(tts.speech_chunks("", 100), [])
        self.assertEqual(tts.speech_chunks("   \n  ", 100), [])

    def test_chunks_respect_the_limit_and_reconstruct(self):
        text = " ".join(f"Sentence number {i} says something worth hearing."
                        for i in range(60))
        chunks = tts.speech_chunks(text, 300)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 300)
        self.assertEqual(" ".join(chunks), text)             # nothing dropped
        for c in chunks[:-1]:
            self.assertTrue(c.endswith((".", "!", "?")), c[-20:])

    def test_sentences_pack_greedily_into_full_chunks(self):
        sentence = "This is a moderately long sentence for packing."
        text = " ".join(sentence for _ in range(20))
        chunks = tts.speech_chunks(text, 250)
        self.assertEqual(len(chunks), 4)          # five 47-char sentences each
        for c in chunks:
            self.assertLessEqual(len(c), 250)
            self.assertGreaterEqual(len(c), 200)  # packed full, not one per

    def test_long_sentence_splits_at_a_word_boundary(self):
        text = " ".join("word" for _ in range(120))          # no sentence marks
        chunks = tts.speech_chunks(text, 200)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 200)
        self.assertEqual(" ".join(chunks), text)
        self.assertTrue(all(c.split()[0] == "word" for c in chunks))

    def test_unbroken_token_hard_cut_at_the_budget(self):
        self.assertEqual(tts.speech_chunks("x" * 1200, 500),
                         ["x" * 500, "x" * 500, "x" * 200])

    def test_no_limit_is_one_chunk(self):
        text = "word " * 500
        self.assertEqual(tts.speech_chunks(text, 0), [" ".join(text.split())])

    def test_provider_budgets_and_override(self):
        cfg = {"chat_id": str(CHAT), "bot_token": "T0K:EN", "tts": {"api_key": "k"}}
        self.assertEqual(tts.chunk_chars(cfg), 4096)          # openai documented
        cfg["tts"]["provider"] = "cloudflare"
        cfg["tts"]["account_id"] = "a"
        self.assertEqual(tts.chunk_chars(cfg), 1000)          # CF: no published limit
        cfg["tts"]["chunk_chars"] = 777
        self.assertEqual(tts.chunk_chars(cfg), 777)
        cfg["tts"]["chunk_chars"] = "junk"
        self.assertEqual(tts.chunk_chars(cfg), 1000)
        cfg["tts"]["provider"] = "openai"
        cfg["tts"]["chunk_chars"] = 0
        self.assertEqual(tts.chunk_chars(cfg), 4096)


class TestConvert(TtsCase):
    def test_ogg_passes_through_without_ffmpeg(self):
        path, err = tts.to_ogg(self.cfg(), OGG, "opus")
        self.assertEqual(err, "")
        self.assertTrue(path.endswith(".ogg"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), OGG)
        os.unlink(path)
        self.assertEqual([a[0] for a in self.runner.argvs], [])

    def test_mp3_converts_through_ffmpeg(self):
        self.runner.meta = b"200 audio/mpeg"
        self.runner.body = b"\xff\xf3data"
        _, fmt, err = tts.synthesize(self.cfg("cloudflare"), "x")
        self.assertEqual((fmt, err), ("mpeg", ""))
        path, err = tts.to_ogg(self.cfg("cloudflare"), b"\xff\xf3data", "mpeg")
        self.assertEqual(err, "")
        cmd = next(a for a in self.runner.argvs if a[0] == "ffmpeg")
        self.assertIn("libopus", cmd)
        with open(path, "rb") as f:
            self.assertEqual(f.read()[:4], b"OggS")
        os.unlink(path)

    def test_ffmpeg_failure_is_an_error(self):
        self.runner.ffmpeg_ok = False
        _, err = tts.to_ogg(self.cfg(), b"\xff\xf3data", "mpeg")
        self.assertIn("ffmpeg failed", err)
        self.assertEqual(tts.error_class(err),
                         "audio conversion failed (is ffmpeg installed?)")

    def test_duration_from_ffprobe(self):
        self.assertEqual(tts.voice_duration("/whatever"), 6)
        self.runner.duration = b"not-a-number"
        self.assertEqual(tts.voice_duration("/whatever"), 0)

    def test_multi_part_assembly_joins_into_one_note(self):
        path, err = tts.assemble_ogg(self.cfg(),
                                     [(OGG, "opus"), (b"\xff\xf3data", "mpeg")])
        self.assertEqual(err, "")
        cmd = next(a for a in self.runner.argvs if a[0] == "ffmpeg")
        self.assertEqual(cmd.count("-i"), 2)
        self.assertIn("concat=n=2:v=0:a=1", cmd[cmd.index("-filter_complex") + 1])
        self.assertIn("libopus", cmd)
        with open(path, "rb") as f:
            self.assertEqual(f.read()[:4], b"OggS")
        os.unlink(path)
        self.assertFalse(os.path.exists(cmd[2]))   # part files cleaned up
        self.assertFalse(os.path.exists(cmd[4]))

    def test_assembly_ffmpeg_failure_is_an_error(self):
        self.runner.ffmpeg_ok = False
        _, err = tts.assemble_ogg(self.cfg(), [(OGG, "opus"), (OGG, "opus")])
        self.assertIn("ffmpeg failed", err)

    def test_assembly_refuses_no_parts(self):
        _, err = tts.assemble_ogg(self.cfg(), [])
        self.assertIn("no audio parts", err)


class TestMaybeSpeak(TtsCase):
    """The deliver-path contract: voice rides after the text and after the
    sent-marker, never gates it, and never happens without a delivered text."""

    def setUp(self):
        super().setUp()
        self.api = FakeApi()
        tg.api = self.api
        self.addCleanup(setattr, tg, "api", _env.REAL_API)
        self.base = tempfile.mkdtemp(prefix="fm-tts-")
        self.session = os.path.join(self.base, "s.jsonl")
        self.herdr = FakeHerdr([])
        tg.herdr_run = self.herdr.run
        tg.herdr_json = self.herdr.json
        self.addCleanup(setattr, tg, "herdr_run", _env.REAL_HERDR_RUN)
        self.addCleanup(setattr, tg, "herdr_json", _env.REAL_HERDR_JSON)
        self.cfg = {**self.cfg(), "settle_ms": 0, "quiet_seconds": 0}
        self.seq = 0

    def deliver(self, reply="All tests pass. Ship it."):
        self.seq += 1
        transcript(self.session, "hi", [reply], ids=[f"m{self.seq}"])
        self.herdr.agents = [agent(MAIN, self.base, status="done", title="π - cai",
                                   session={"kind": "path", "value": self.session})]
        return notify.deliver(self.cfg, MAIN, "done", "working", agent="pi", cli="pi")

    def test_mode_off_sends_text_only(self):
        self.deliver()
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.voice.calls, [])
        self.assertEqual(self.runner.argvs, [])

    def test_mode_on_speaks_after_the_text(self):
        tts.set_voice_mode(True)
        self.deliver()
        self.assertEqual(len(self.api.sent), 1)         # the text push
        self.assertEqual(len(self.voice.calls), 1)      # then the voice note
        self.assertTrue(self.voice.calls[0]["path"].endswith(".ogg"))
        self.assertEqual(self.voice.calls[0]["duration"], 6)

    def test_voice_note_waits_for_the_sent_marker(self):
        tts.set_voice_mode(True)
        marker_at_send = {}
        real = tg.send_voice

        def probe(cfg, path, duration=0):
            marker_at_send["stamped"] = notify.content_already_sent(MAIN, "m1")
            return real(cfg, path, duration=duration)

        tg.send_voice = probe
        self.assertTrue(self.deliver())
        self.assertEqual(len(self.voice.calls), 1)
        self.assertTrue(marker_at_send.get("stamped"))

    def test_failed_text_send_speaks_nothing_and_stamps_no_marker(self):
        tts.set_voice_mode(True)
        self.api.fail_send = "telegram: went away"
        self.assertFalse(self.deliver())
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.voice.calls, [])
        self.assertFalse(notify.content_already_sent(MAIN, "m1"))

    def test_an_already_pushed_message_never_pushes_or_speaks_twice(self):
        tts.set_voice_mode(True)
        self.assertTrue(self.deliver())
        texts, voices = len(self.api.sent), len(self.voice.calls)
        self.assertFalse(notify.deliver(self.cfg, MAIN, "done", "done",
                                        agent="pi", cli="pi"))
        self.assertEqual(len(self.api.sent), texts)
        self.assertEqual(len(self.voice.calls), voices)

    def test_voice_message_is_routable_like_the_text(self):
        tts.set_voice_mode(True)
        self.deliver()
        entry = tg.target_for_reply(777)
        self.assertEqual(entry.get("pane"), MAIN)

    def test_speech_failure_never_fails_the_text(self):
        tts.set_voice_mode(True)
        self.runner.failures = "timeout after 45s"
        self.assertTrue(self.deliver())
        self.assertEqual(len(self.api.sent), 2)         # text + one warning
        self.assertIn("All tests pass", self.api.sent[0]["text"])
        self.assertIn("network timeout", self.api.sent[1]["text"])
        self.assertEqual(self.voice.calls, [])

    def test_failure_warning_is_rate_limited_to_one_per_hour(self):
        tts.set_voice_mode(True)
        self.runner.failures = "timeout after 45s"
        self.deliver()
        self.deliver()
        self.assertEqual(len(self.api.sent), 3)         # text, warn, text — no second warn

    def test_mode_on_without_config_warns_and_stays_text_only(self):
        tts.set_voice_mode(True)
        self.cfg["tts"].pop("api_key")
        self.assertTrue(self.deliver())
        self.assertEqual(len(self.api.sent), 2)
        self.assertIn("unavailable", self.api.sent[1]["text"])

    def test_code_only_reply_skips_voice_quietly(self):
        tts.set_voice_mode(True)
        self.deliver(reply="```bash\nls -la\n```")
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.voice.calls, [])

    def test_pure_acknowledgement_skips_voice(self):
        tts.set_voice_mode(True)
        self.deliver(reply="Captain, shipshape.")
        self.assertEqual(len(self.api.sent), 1)         # the text push goes out
        self.assertEqual(self.voice.calls, [])          # but no voice note
        self.assertEqual(self.runner.argvs, [])         # nothing synthesized

    def test_substantive_reply_still_speaks(self):
        tts.set_voice_mode(True)
        self.deliver(reply="Captain, shipshape. Two tests fail; fixing now.")
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(len(self.voice.calls), 1)

    def test_acknowledgement_inside_a_longer_message_still_speaks(self):
        tts.set_voice_mode(True)
        self.deliver(reply="All clear, Captain — three files changed, CI green.")
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(len(self.voice.calls), 1)

    def test_sendvoice_failure_warns_after_the_text(self):
        tts.set_voice_mode(True)
        self.voice = FakeVoice(err="telegram: BOT_WRITE_NOT_ALLOWED")
        tg.send_voice = self.voice
        self.assertTrue(self.deliver())
        self.assertEqual(len(self.api.sent), 2)
        self.assertIn("voice delivery failed", self.api.sent[1]["text"])

    def test_no_reply_body_no_voice(self):
        tts.set_voice_mode(True)
        self.assertTrue(self.deliver(reply=""))
        self.assertEqual(self.voice.calls, [])

    def test_empty_replied_text_never_synthesizes(self):
        tts.set_voice_mode(True)
        self.runner.body = b""
        self.deliver(reply="")
        self.assertEqual(self.runner.argvs, [])

    def test_long_reply_spoken_in_full_as_one_chunked_note(self):
        tts.set_voice_mode(True)
        reply = " ".join(f"Sentence {i} carries real content for the operator."
                         for i in range(150))            # ~7.5k chars > 4096
        spoken = " ".join(reply.split())
        self.assertTrue(self.deliver(reply=reply))
        self.assertEqual(len(self.voice.calls), 1)       # ONE voice note
        self.assertEqual(len(self.runner.bodies), 2)     # chunked at 4096
        for body in self.runner.bodies:
            self.assertLessEqual(len(body["input"]), 4096)
        joined = (self.runner.bodies[0]["input"] + " "
                  + self.runner.bodies[1]["input"])
        self.assertEqual(joined, spoken)                 # nothing truncated
        ff = [a for a in self.runner.argvs if a[0] == "ffmpeg"]
        self.assertEqual(len(ff), 1)                     # assembled in one run
        self.assertIn("concat=n=2:v=0:a=1", ff[0][ff[0].index("-filter_complex") + 1])
        with open(tg.LOG_PATH, encoding="utf-8") as f:
            log = f.read()
        self.assertIn(f"tts: voice note sent ({len(spoken)} chars spoken, 2 chunks",
                      log)

    def test_chunk_failure_fails_the_whole_note(self):
        tts.set_voice_mode(True)
        reply = " ".join(f"Sentence {i} carries real content for the operator."
                         for i in range(150))
        self.runner.curl_fail_on = 1                     # the second chunk dies
        self.assertTrue(self.deliver(reply=reply))
        self.assertEqual(self.voice.calls, [])           # no half-spoken note
        self.assertEqual(len(self.runner.bodies), 2)     # both chunks attempted
        # the long text splits (notify CHUNK_CHARS) — but exactly ONE warning
        warns = [p for p in self.api.sent if "speech failed" in p.get("text", "")]
        self.assertEqual(len(warns), 1)
        with open(tg.LOG_PATH, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("synthesize failed (chunk 2/2)", log)

    def test_max_chars_still_truncates_when_set(self):
        tts.set_voice_mode(True)
        self.cfg["tts"]["max_chars"] = 120
        self.deliver(reply="Sentence one is here. " * 100)
        self.assertEqual(len(self.voice.calls), 1)
        self.assertEqual(len(self.runner.bodies), 1)     # fits one request
        self.assertLessEqual(len(self.runner.bodies[0]["input"]), 120)
        self.assertTrue(self.runner.bodies[0]["input"].endswith("."))


class TestSendVoice(unittest.TestCase):
    """tg.send_voice: multipart upload with the token out of argv."""

    def setUp(self):
        reset_state()
        self.argvs = []
        self.urlfiles = []
        real = tg.run_bounded

        def fake_run(cmd, timeout):
            self.argvs.append(list(cmd))
            kf = cmd[cmd.index("-K") + 1]
            with open(kf, encoding="utf-8") as f:
                self.urlfiles.append(f.read())
            return 0, b'{"ok":true,"result":{"message_id":42}}', b"", ""

        tg.run_bounded = fake_run
        self.addCleanup(setattr, tg, "run_bounded", real)
        self.tmp = tempfile.mkdtemp(prefix="fm-tts-voice-")
        self.ogg = os.path.join(self.tmp, "note.ogg")
        with open(self.ogg, "wb") as f:
            f.write(OGG)

    def cfg(self):
        return {"chat_id": str(CHAT), "bot_token": "T0K:EN"}

    def test_shape_and_token_hygiene(self):
        result, err = tg.send_voice(self.cfg(), self.ogg, duration=12)
        self.assertEqual((err, result), ("", {"message_id": 42}))
        cmd = self.argvs[0]
        self.assertEqual(cmd[0], "curl")
        self.assertIn(f"voice=@{self.ogg}", cmd)
        self.assertIn(f"chat_id={CHAT}", cmd)
        self.assertIn("duration=12", cmd)
        self.assertNotIn("T0K:EN", " ".join(cmd))
        self.assertTrue(self.urlfiles[0].startswith("url = "))
        self.assertIn("/sendVoice", self.urlfiles[0])
        self.assertFalse(os.path.exists(cmd[cmd.index("-K") + 1]))  # unlinked

    def test_duration_zero_is_omitted(self):
        tg.send_voice(self.cfg(), self.ogg)
        self.assertNotIn("duration=0", " ".join(self.argvs[0]))

    def test_error_description_surfaced(self):
        real = tg.run_bounded

        def fail(cmd, timeout):
            return 0, b'{"ok":false,"description":"VOICE_OGG_INVALID"}', b"", ""

        tg.run_bounded = fail
        _, err = tg.send_voice(self.cfg(), self.ogg)
        self.assertIn("VOICE_OGG_INVALID", err)

    def test_missing_token_refuses_early(self):
        _, err = tg.send_voice({"chat_id": str(CHAT)}, self.ogg)
        self.assertEqual(err, "bot_token not set")


if __name__ == "__main__":
    unittest.main()
