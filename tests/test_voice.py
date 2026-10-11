"""Offline voice/STT tests: the provider transcription call is stubbed at the curl
seam (stt._curl), herdr and the Telegram API at the tg module boundary as
everywhere else in this suite. The contract under test: a voice note rides the
exact same routing as typed text (main-pane-only under firstmate mode, worker
context prefix, caption semantics), the transcription request carries the verified
OpenAI multipart shape with the key out of argv for every supported provider, and
every failure mode reaches the operator instead of dropping the note silently. No
network, no real key."""

import os
import subprocess
import tempfile
import unittest

import _env  # noqa: F401  (env must be set before tg/bot imports)
import bot
import firstmate
import stt
import tg
from _env import (CHAT, MAIN, USER, WORKER, FakeApi, FakeHerdr, agent, make_home,
                  reset_state)

TASK = ("bridge-voice-groq", WORKER, "ship", ["working [at=1790000000]: implementing"])
WORKER_CTX = f"[opencode/bridge-voice-groq pane {WORKER}]"
GROQ_KEY = "gsk_test_0123456789"
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
REAL_CURL = stt._curl


def voice_msg(msg_id=20, reply_to=None, caption=None, size=2048, duration=12,
              chat=CHAT, user=USER):
    m = {"chat": {"id": chat}, "from": {"id": user}, "message_id": msg_id,
         "voice": {"file_id": "vfid", "duration": duration, "file_size": size,
                   "mime_type": "audio/ogg"}}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    if caption is not None:
        m["caption"] = caption
    return m


class Completed:
    """The slice of subprocess.CompletedProcess transcribe looks at."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout.encode() if isinstance(stdout, str) else stdout
        self.stderr = stderr.encode() if isinstance(stderr, str) else stderr
        self.returncode = returncode


class FakeCurl:
    """Stands in for the stt curl subprocess: snapshots argv and the `-H @file`
    header contents *during* the call (transcribe unlinks the file afterwards),
    then answers with a canned response."""

    def __init__(self, body='{"text": "hello from the phone"}', http=200,
                 rc=0, stderr="", raise_exc=None):
        self.body, self.http, self.rc = body, http, rc
        self.stderr, self.raise_exc = stderr, raise_exc
        self.argvs = []
        self.headers = []
        self.header_paths = []
        self.header_modes = []

    def __call__(self, cmd, timeout):
        self.argvs.append(list(cmd))
        path = cmd[cmd.index("-H") + 1]
        assert path.startswith("@"), "auth header must come from a file, not argv"
        with open(path[1:], encoding="utf-8") as f:
            self.headers.append(f.read())
        self.header_paths.append(path[1:])
        self.header_modes.append(os.stat(path[1:]).st_mode & 0o777)
        if self.raise_exc is not None:
            raise self.raise_exc
        return Completed(f"{self.body}\n{self.http}", self.stderr, self.rc)


class VoiceCase(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.base = tempfile.mkdtemp(prefix="fm-voice-")
        self.home = make_home(self.base, tasks=[TASK])
        self.agents = [agent(MAIN, self.home, status="working"),
                       agent(WORKER, f"{self.home}-treehouse/w1", cli="opencode",
                             status="working", title="OC | voice port")]
        self.herdr = FakeHerdr(self.agents)
        self.api = FakeApi()
        tg.herdr_run = self.herdr.run
        tg.herdr_json = self.herdr.json
        tg.api = self.api
        tg.download = lambda cfg, remote, dest: (open(dest, "wb").write(b"OggS"), "")[1]
        self.curl = FakeCurl()
        stt._curl = self.curl
        self.addCleanup(setattr, tg, "herdr_run", _env.REAL_HERDR_RUN)
        self.addCleanup(setattr, tg, "herdr_json", _env.REAL_HERDR_JSON)
        self.addCleanup(setattr, tg, "api", _env.REAL_API)
        self.addCleanup(setattr, tg, "download", _env.REAL_DOWNLOAD)
        self.addCleanup(setattr, stt, "_curl", REAL_CURL)

    def cfg(self, firstmate=True, **stt_opts):
        config = {"chat_id": str(CHAT), "bot_token": "T0K:EN",
                  "allowed_user_ids": [USER],
                  "stt": {"provider": "groq", "api_key": GROQ_KEY, **stt_opts}}
        if firstmate:
            config["firstmate"] = {"enabled": True, "main_cwd": self.home}
        return config

    def seed_map(self, message_id, entry):
        seen = tg.read_state(tg.MSG_MAP, {})
        seen[str(message_id)] = {**entry, "ts": 1}
        tg.write_state(tg.MSG_MAP, seen)

    def worker_notification(self, message_id=501):
        self.seed_map(message_id, {"pane": MAIN, "agent": "firstmate",
                                   "folder": self.home, "context": WORKER_CTX,
                                   "worker_pane": WORKER})


class TestGroqRequest(VoiceCase):
    """The verified endpoint shape: one multipart POST, key out of argv."""

    def setUp(self):
        super().setUp()
        self.config = self.cfg()
        self.audio = os.path.join(tempfile.mkdtemp(prefix="fm-voice-ogg-"), "note.ogg")
        with open(self.audio, "wb") as f:
            f.write(b"OggS")

    def test_request_shape(self):
        text, err = stt.transcribe(self.config, self.audio)
        self.assertEqual((text, err), ("hello from the phone", ""))
        self.assertEqual(len(self.curl.argvs), 1)
        cmd = self.curl.argvs[0]
        self.assertIn(GROQ_URL, cmd)
        self.assertIn("model=whisper-large-v3-turbo", cmd)
        self.assertIn("response_format=json", cmd)
        self.assertIn(f"file=@{self.audio}", cmd)
        self.assertNotIn("language=", " ".join(cmd))  # unset hint is not sent

    def test_language_and_model_overrides_forwarded(self):
        config = self.cfg(language="en", model="whisper-large-v3")
        stt.transcribe(config, self.audio)
        cmd = self.curl.argvs[0]
        self.assertIn("language=en", cmd)
        self.assertIn("model=whisper-large-v3", cmd)
        self.assertNotIn("model=whisper-large-v3-turbo", cmd)

    def test_key_rides_in_a_0600_header_file_never_argv(self):
        stt.transcribe(self.config, self.audio)
        self.assertEqual(self.curl.headers, [f"Authorization: Bearer {GROQ_KEY}\n"])
        self.assertEqual(self.curl.header_modes, [0o600])
        self.assertNotIn(GROQ_KEY, " ".join(self.curl.argvs[0]))
        for path in self.curl.header_paths:  # unlinked once the call is done
            self.assertFalse(os.path.exists(path))

    def test_http_error_surfaces_redacted(self):
        self.curl.body, self.curl.http = '{"error":{"message":"Invalid API key"}}', 401
        text, err = stt.transcribe(self.config, self.audio)
        self.assertEqual(text, "")
        self.assertIn("http 401", err)
        self.assertIn("Invalid API key", err)
        self.assertNotIn(GROQ_KEY, err)

    def test_error_body_key_material_redacted(self):
        self.curl.body = f'{{"error":{{"message":"key {GROQ_KEY} rejected"}}}}'
        self.curl.http = 400
        _, err = stt.transcribe(self.config, self.audio)
        self.assertNotIn(GROQ_KEY, err)
        self.assertIn("<stt-key>", err)

    def test_timeout_repr_redacted(self):
        # the F2 shape: an exception repr embeds argv — even a poisoned one loses the key
        self.curl.raise_exc = subprocess.TimeoutExpired(
            ["curl", "-H", f"Authorization: Bearer {GROQ_KEY}"], 1)
        _, err = stt.transcribe(self.config, self.audio)
        self.assertNotIn(GROQ_KEY, err)
        self.assertIn("<stt-key>", err)

    def test_curl_exit_and_bad_json(self):
        self.curl.rc, self.curl.stderr, self.curl.http, self.curl.body = 7, "Failed to connect", 0, ""
        _, err = stt.transcribe(self.config, self.audio)
        self.assertTrue(err.startswith("curl exit=7"))
        self.curl.rc, self.curl.stderr, self.curl.http, self.curl.body = 0, "", 200, "not json"
        _, err = stt.transcribe(self.config, self.audio)
        self.assertIn("bad json", err)

    def test_keyless_never_calls_out(self):
        config = self.cfg()
        config["stt"].pop("api_key")
        text, err = stt.transcribe(config, self.audio)
        self.assertEqual((text, err), ("", "stt.api_key is not set"))
        self.assertEqual(self.curl.argvs, [])

    def test_placeholder_key_counts_as_unset(self):
        config = self.cfg(api_key="REPLACE_ME:gsk")
        _, err = stt.transcribe(config, self.audio)
        self.assertEqual(err, "stt.api_key is not set")
        self.assertEqual(self.curl.argvs, [])


class TestProviderDispatch(VoiceCase):
    """Provider dispatch: every supported provider speaks the same OpenAI multipart
    shape — only the per-provider defaults differ, and base_url/model win."""

    def setUp(self):
        super().setUp()
        self.audio = os.path.join(tempfile.mkdtemp(prefix="fm-voice-ogg-"), "note.ogg")
        with open(self.audio, "wb") as f:
            f.write(b"OggS")

    def test_openai_default_shape(self):
        """provider="openai" speaks the standard OpenAI audio-transcriptions shape."""
        text, err = stt.transcribe(self.cfg(provider="openai"), self.audio)
        self.assertEqual((text, err), ("hello from the phone", ""))
        cmd = self.curl.argvs[0]
        self.assertIn(OPENAI_URL, cmd)
        self.assertIn("model=whisper-1", cmd)
        self.assertIn("response_format=json", cmd)
        self.assertIn(f"file=@{self.audio}", cmd)

    def test_groq_remains_the_default_provider(self):
        stt.transcribe(self.cfg(), self.audio)
        cmd = self.curl.argvs[0]
        self.assertIn(GROQ_URL, cmd)
        self.assertIn("model=whisper-large-v3-turbo", cmd)

    def test_base_url_override_honored_for_both_providers(self):
        stt.transcribe(self.cfg(provider="openai", base_url="https://gw.example/v1/"),
                       self.audio)
        self.assertIn("https://gw.example/v1/audio/transcriptions", self.curl.argvs[0])
        stt.transcribe(self.cfg(base_url="https://gw.example/v1"), self.audio)
        self.assertIn("https://gw.example/v1/audio/transcriptions", self.curl.argvs[1])

    def test_openai_model_override_forwarded(self):
        stt.transcribe(self.cfg(provider="openai", model="gpt-4o-transcribe"), self.audio)
        cmd = self.curl.argvs[0]
        self.assertIn("model=gpt-4o-transcribe", cmd)
        self.assertNotIn("model=whisper-1", cmd)

    def test_provider_value_normalized(self):
        stt.transcribe(self.cfg(provider="  OpenAI "), self.audio)
        self.assertIn(OPENAI_URL, self.curl.argvs[0])
        self.assertEqual(stt.startup_warnings(self.cfg(provider="OpenAI")), [])

    def test_openai_config_has_no_startup_warnings(self):
        self.assertEqual(stt.startup_warnings(self.cfg(provider="openai")), [])

    def test_unsupported_provider_refused_with_supported_list(self):
        err = stt.config_error(self.cfg(provider="deepgram"))
        self.assertIn('unknown stt provider "deepgram"', err)
        self.assertIn('"groq"', err)
        self.assertIn('"openai"', err)


class TestPromptSemantics(VoiceCase):
    """hermes gateway semantics for what a transcript becomes."""

    def test_transcript_is_a_bare_quoted_line(self):
        self.assertEqual(stt.prompt_for("  check the tests  "), '"check the tests"')

    def test_empty_transcript_becomes_the_sentinel(self):
        prompt = stt.prompt_for("   ")
        self.assertIn("empty or inaudible", prompt)
        self.assertIn("Do not guess", prompt)
        self.assertTrue(prompt.startswith("["))

    def test_error_classes(self):
        cases = {"stt.api_key is not set": "no stt api_key configured",
                 "http 401: Invalid API key": "Groq rejected the API key",
                 "http 429: rate limit": "Groq rate limit",
                 "http 500: boom": "Groq server error",
                 "curl exit=7 x": "network error",
                 "TimeoutExpired(cmd, 1)": "network timeout",
                 "something else": "transcription failed"}
        for err, want in cases.items():
            self.assertEqual(stt.error_class(err), want)


class TestVoiceRouting(VoiceCase):
    """A transcript must be routed exactly like typed text on every posture path."""

    def setUp(self):
        super().setUp()
        self.config = self.cfg()

    def test_plain_voice_reaches_main_as_a_quoted_line(self):
        bot.handle_message(self.config, voice_msg())
        self.assertEqual(self.herdr.prompts(), [(MAIN, '"hello from the phone"')])
        self.assertTrue(any("🎙️" in t and "hello from the phone" in t
                            for t in self.api.texts))

    def test_reply_to_worker_notification_routes_to_main_with_prefix(self):
        self.worker_notification()
        bot.handle_message(self.config, voice_msg(reply_to=501))
        self.assertEqual(self.herdr.prompts(),
                         [(MAIN, f"{WORKER_CTX}\n\"hello from the phone\"")])

    def test_caption_is_prepended_like_attachment_captions(self):
        bot.handle_message(self.config, voice_msg(caption="quick note"))
        pane, text = self.herdr.prompts()[0]
        self.assertEqual(text, 'quick note\n"hello from the phone"')

    def test_every_voice_path_lands_on_main_only(self):
        """The posture invariant, voice edition: across every flavor of incoming
        voice message, no `herdr agent prompt` ever names a non-main pane."""
        self.worker_notification()
        self.seed_map(502, {"pane": MAIN, "agent": "pi", "folder": self.home})
        self.seed_map(503, {"pane": WORKER, "agent": "opencode", "folder": "~/w"})
        for m in (voice_msg(msg_id=21), voice_msg(msg_id=22, reply_to=501),
                  voice_msg(msg_id=23, reply_to=502), voice_msg(msg_id=24, reply_to=503),
                  voice_msg(msg_id=25, caption="with caption")):
            bot.handle_message(self.config, m)
        panes = {pane for pane, _ in self.herdr.prompts()}
        self.assertEqual(panes, {MAIN})

    def test_mode_off_routes_to_current_target_verbatim(self):
        config = self.cfg(firstmate=False)
        tg.set_current_target({"pane": WORKER, "agent": "opencode", "folder": "~/w"})
        bot.handle_message(config, voice_msg())
        self.assertEqual(self.herdr.prompts(), [(WORKER, '"hello from the phone"')])

    def test_stt_absent_voice_is_a_plain_attachment(self):
        config = self.cfg()
        del config["stt"]
        bot.handle_message(config, voice_msg())
        pane, text = self.herdr.prompts()[0]
        self.assertEqual(pane, MAIN)
        self.assertIn("/inbox/", text)          # the path, not a quoted transcript
        self.assertNotIn('"hello', text)
        self.assertEqual(self.curl.argvs, [])      # nothing transcribed
        self.assertTrue(any(t.startswith("📎") for t in self.api.texts))

    def test_stt_disabled_voice_is_a_plain_attachment(self):
        config = self.cfg(enabled=False)
        bot.handle_message(config, voice_msg())
        _, text = self.herdr.prompts()[0]
        self.assertIn("/inbox/", text)
        self.assertEqual(self.curl.argvs, [])

    def test_music_audio_file_is_never_transcribed(self):
        """The format gate: only voice bubbles are STT input; a music file sent as
        `audio` rides the plain attachment path even with [stt] on."""
        music = {"chat": {"id": CHAT}, "from": {"id": USER}, "message_id": 30,
                 "audio": {"file_id": "afid", "file_name": "song.mp3", "file_size": 100}}
        bot.handle_message(self.cfg(), music)
        self.assertEqual(self.curl.argvs, [])
        _, text = self.herdr.prompts()[0]
        self.assertIn("song.mp3", text)


class TestVoiceFailures(VoiceCase):
    """Every failure reaches the operator with a why-class; nothing drops silently."""

    def setUp(self):
        super().setUp()
        self.config = self.cfg()

    def test_transcribe_failure_notifies_and_never_prompts(self):
        self.curl.body, self.curl.http = '{"error":{"message":"internal"}}', 500
        bot.handle_message(self.config, voice_msg())
        self.assertEqual(self.herdr.prompts(), [])
        notes = [t for t in self.api.texts if "transcription failed" in t]
        self.assertEqual(len(notes), 1)
        self.assertIn("Groq server error", notes[0])
        self.assertIn("kept at", notes[0])   # the audio survives in the inbox
        self.assertIn("voice.ogg", notes[0])

    def test_missing_key_refuses_before_anything(self):
        config = self.cfg()
        config["stt"].pop("api_key")
        bot.handle_message(config, voice_msg())
        self.assertEqual(self.herdr.prompts(), [])
        self.assertEqual(self.curl.argvs, [])
        self.assertTrue(any("stt.api_key is not set" in t for t in self.api.texts))

    def test_unknown_provider_refuses_loudly(self):
        config = self.cfg(provider="deepgram")
        warnings = stt.startup_warnings(config)
        self.assertEqual(len(warnings), 1)
        self.assertIn('unknown stt provider "deepgram"', warnings[0])
        bot.handle_message(config, voice_msg())
        self.assertEqual(self.herdr.prompts(), [])
        self.assertEqual(self.curl.argvs, [])
        self.assertTrue(any("unknown stt provider" in t for t in self.api.texts))

    def test_good_config_has_no_startup_warnings(self):
        self.assertEqual(stt.startup_warnings(self.cfg()), [])
        self.assertEqual(stt.startup_warnings({"chat_id": "1"}), [])

    def test_oversized_voice_rejected_before_download(self):
        big = voice_msg(size=int(25 * 1024 * 1024))
        bot.handle_message(self.config, big)
        self.assertEqual(self.herdr.prompts(), [])
        self.assertEqual(self.curl.argvs, [])
        self.assertTrue(any("over the 20 MB limit" in t for t in self.api.texts))

    def test_empty_transcript_submits_sentinel_not_blank(self):
        self.curl.body = '{"text": "   "}'
        bot.handle_message(self.config, voice_msg())
        prompts = self.herdr.prompts()
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0][0], MAIN)
        self.assertIn("empty or inaudible", prompts[0][1])
        self.assertTrue(any("no intelligible speech" in t for t in self.api.texts))

    def test_echo_off_confirms_without_the_transcript(self):
        config = self.cfg(echo_transcript=False)
        bot.handle_message(config, voice_msg())
        self.assertEqual(self.herdr.prompts(), [(MAIN, '"hello from the phone"')])
        self.assertTrue(any(t.startswith("🎙️ voice →") for t in self.api.texts))
        self.assertFalse(any("hello from the phone" in t for t in self.api.texts))

    def test_long_transcript_echo_is_truncated_but_prompt_is_full(self):
        self.curl.body = '{"text": "%s"}' % ("word " * 200)
        bot.handle_message(self.config, voice_msg())
        pane, text = self.herdr.prompts()[0]
        self.assertGreater(len(text), 900)          # the agent gets all of it
        echoed = [t for t in self.api.texts if "🎙️" in t][0]
        self.assertIn("…", echoed)                  # the phone gets a tail cut
        self.assertLessEqual(len(echoed), 500)


if __name__ == "__main__":
    unittest.main()
