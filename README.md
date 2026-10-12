# Firstmate Telegram Bridge

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/kirkins/firstmate-telegram-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/kirkins/firstmate-telegram-bridge/actions/workflows/ci.yml)
![herdr 0.7+](https://img.shields.io/badge/herdr-0.7%2B-8a2be2)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab)
![platforms: linux • macOS](https://img.shields.io/badge/platforms-linux%20%E2%80%A2%20macOS-informational)

**Keep your Firstmate fleet in reach from Telegram.** Walk away from your agents and answer from your phone. When an agent finishes or gets stuck, you get its complete reply; answer that notification and your message goes through the Firstmate main pane.

## Firstmate companion (default)

This bridge is built for Firstmate, which manages
an agent fleet. Fleet mode is on by default: Telegram talks to the Firstmate
main pane, while worker notifications and replies stay out of the phone
channel. Set `[firstmate].main_cwd` to the exact working directory of your
Firstmate main pane and set `allowed_user_ids` to your Telegram user id (get it
from [@userinfobot](https://t.me/userinfobot)).
Workers can still be selected as context; messages are routed through the main
pane.

To use the generic Herdr behavior instead, set `[firstmate] enabled = false` in
your `config.toml`. That restores per-pane notifications and direct replies.

```
✅ claude · sunrise · Fix the flaky integration test
   All three tests pass now. The race was in the fixture
   teardown — the DB handle outlived the transaction…

                        also add a regression test for it  ↰
➡️ sent to claude · ~/dev/sunrise

   … 40 seconds later …

✅ claude · sunrise · Fix the flaky integration test
   Added `test_teardown_ordering`. Full suite green: 41 passed.
```

No relay server, no tunnel, no port to open, no companion app. Fleet mode is enabled by default. See [Firstmate companion](#firstmate-companion-default) for setup and generic-mode opt-out. The plugin makes **outbound** long-poll requests to Telegram and nothing else — which is exactly why it works from a laptop on hotel wifi.

## Why you'd want it

- **Reply, don't command.** Every notification is addressed to the agent it came from, so replying to it routes back to that pane. Ten agents running, ten conversations — Telegram's own reply threading keeps them straight. Commands exist, but you rarely need one.
- **The whole answer, readable.** The agent's final reply is rendered into Telegram's markup — headings, bullets, links, syntax-labelled code, monospaced tables. Long answers fold into an expandable quote so a chatty agent can't flood the chat (Firstmate mode sends them unfolded by default — see [Firstmate posture](#firstmate-posture)), and anything past Telegram's 4096-character ceiling is split across messages that all remain replyable.
- **Send it a screenshot.** Photos and files land on disk and the agent is handed the path — point it at a stack trace, a design mock, or a log without getting to a keyboard.
- **Nothing to run.** No relay, no tunnel, no daemon you have to remember to start. The poller is launched by herdr itself and self-heals if it dies.
- **It only listens to you.** Only the chat id you configure is accepted; everything else is logged and dropped. `allowed_user_ids` narrows it further to specific accounts.

## What it does

### Notifications

One line of context (status, agent, directory, pane title) and then the agent's final reply. The header is deliberately short: a phone's notification preview gets only a few lines and they should go to the reply.

herdr reports `done` only for unfocused panes, so the plugin also tracks `working → idle` per pane and treats that as a completion — you get told either way, focused or not. `statuses`, an `agents` allowlist and a `quiet_seconds` debounce control what is worth interrupting you for.

### Replies

Your text is submitted with `herdr agent prompt`; the agent starts working, finishes, and the next notification arrives on its own. A message that isn't a reply goes to the most recently active agent, so a back-and-forth doesn't need long-pressing every time.

### Commands

Published with `setMyCommands`, so they show up in Telegram's menu button:

| | |
|---|---|
| `/agents` | every agent as a button, grouped under its working directory, most recently active group first — with status, agent and pane title, because agents often share a directory and the title is what tells them apart |
| `/read [lines]` | that agent's recent terminal output |
| `/esc` | send Escape to interrupt it |
| `/voice [on\|off\|status]` | toggle spoken replies: with the mode on, agent replies are additionally spoken through a configurable TTS API and sent as voice notes (needs a `[tts]` section) |
| `/help` | how replying works |

### Attachments

Photos, documents, voice notes and video are saved to `<state dir>/inbox/`, and the agent is prompted with your caption plus the absolute path. Sender-supplied filenames are flattened to a plain basename, and the inbox is swept of anything older than `inbox_days` (7) every six hours.

### Voice input

Configure `[stt]` and a voice note becomes a spoken reply: it is transcribed through an OpenAI-compatible audio-transcriptions API and the transcript is submitted exactly like typed text — same routing, same reply threading, caption prepended when there is one. The audio is still saved to the inbox either way.

- An empty or inaudible note becomes an explicit "came through empty" marker rather than a blank prompt.
- A failed transcription is reported to you with a plain-language reason instead of dropped.
- Without `[stt]`, a voice note is just an attachment.
- Music sent as an audio *file* is never transcribed — only voice bubbles are.

Configure `[stt]`: transcription speaks the standard OpenAI shape — one multipart `POST {base_url}/audio/transcriptions` with the key as a bearer header — so any OpenAI-compatible transcription gateway works:

- **`provider = "groq"`** (the default, and the tested convenience) points at Groq's Whisper endpoint (`whisper-large-v3-turbo` by default).
- **`provider = "openai"`** speaks the plain OpenAI audio API (`whisper-1` by default).

Either value honors a `[stt] base_url` override, so any other compatible endpoint works by pointing at it and setting `[stt] model` to what it serves.

### Voice output

The other direction: `/voice` toggles spoken replies for the chat. With it on, each delivered message is spoken through a text-to-speech API and sent as an Ogg/Opus voice note right after the text message. The text always arrives first, so a slow or failing speech call never loses or delays the reply, and replying to the voice bubble routes exactly like replying to the text.

Configure `[tts]` — like voice input, this runs on any OpenAI-compatible endpoint:

- **`provider = "openai"` (the default)** speaks the standard OpenAI speech API shape — `POST {base_url}/audio/speech`, raw audio back — any compatible gateway works. `base_url` points it anywhere, and `response_format = "opus"` is Ogg/Opus already.
- **`provider = "cloudflare"`** is the tested convenience for Cloudflare Workers AI, which has **no OpenAI-compatible speech route** (verified 2026-10-09: `.../ai/v1/audio/speech` answers 400 “No route for that URI” — the compat surface covers chat and embeddings only). It speaks Workers AI's native shape instead: a POST to `/client/v4/accounts/{account_id}/ai/run/{model}` whose input field differs per model family — `text` for the Deepgram Aura models, `prompt` for `@cf/myshell-ai/melotts` — so the adapter sends both. Aura returns raw MP3, melotts a base64-WAV JSON envelope; anything not already Ogg/Opus is converted with ffmpeg, which voice-output mode therefore requires for those providers.

**Every reply is spoken in full, however long.** Code blocks and tables become a spoken “code omitted” placeholder — the text message carries them in full.

**Prose longer than the provider's per-request input budget is chunked on sentence boundaries.** Each chunk synthesized separately, then assembled by ffmpeg into ONE Ogg/Opus voice note. The budget is 4096 chars for the OpenAI shape per its published spec, 1000 for cloudflare where no limit is published; `[tts] chunk_chars` overrides it, and an explicit `[tts] max_chars` still truncates if ever wanted. A failed chunk fails the whole note rather than sending a half-spoken message.

**Speech includes every Firstmate mid-run message by default.** Set `[tts] speak_final_only = true` to keep intermediate messages text-only and speak only final replies; `false` (the shipped default) speaks both.

**Pure acknowledgements stay text-only.** A message that is nothing but a trivial all-clear — “Captain, shipshape.” and its few trivially-equivalent forms (`tts.ACK_SHAPES`), matched whole-message only — is never spoken: a voice bubble for a nothing-happened signal is noise, so the skip is logged like the other refusals and the text bubble carries it. An acknowledgement embedded in a longer substantive message still speaks.

**The no-op acknowledgement can be dropped entirely.** `suppress_noop_ack = true` stops even that text push when the message is nothing but the routine no-op acknowledgement (“Captain, shipshape.”, matched with the same semantics the voice skip uses): nothing reaches Telegram — no text, no voice, no notification — so the phone notifies only when something actually happened. Each drop is one `bridge.log` line; every other message delivers unchanged. `false` (the default) keeps the quiet text bubble.

**An absent `[tts]` section or a bad key makes `/voice` report exactly why** instead of breaking replies; runtime failures log to bridge.log and warn the chat at most once an hour.

A consolidated single voice note for several messages is under consideration.

### Reply text

Reply text is read from each agent CLI's own session storage:

| Agent | Source |
|---|---|
| claude (Claude Code) | `~/.claude/projects/*/<session>.jsonl` |
| codex | `~/.codex/sessions/…/rollout-*.jsonl`, using Herdr's explicit reference; firstmate mode can bind an omitted reference to the exact native session (see below) |
| pi | session path reported by herdr, or the newest session under `~/.pi/agent/sessions/<cwd>` when it reports none — keyed on the pane's `foreground_cwd`, because herdr 0.8.2+ reports a spawned pane's `cwd` as its spawn origin and no session ref for pi, and the origin's directory would resolve somebody else's session |
| opencode | `~/.local/share/opencode/opencode.db`, session matched by directory + title when herdr reports none |
| grok | `~/.grok/sessions/<cwd>/…/chat_history.jsonl`, session pinned via the pane's process |
| kimi (Kimi Code) | `~/.kimi-code/sessions/…/wire.jsonl` |
| hermes | `~/.hermes/state.db` |

Anything else still notifies and still takes replies — just without the transcript excerpt.

### Titles

Not every CLI sets a useful terminal title: pi shows `π - <user>`, hermes shows `π - <dir>`, codex shows the directory, a fresh Claude Code shows its own name. When herdr's title says nothing, the session's opening prompt is used instead — skipping the synthetic context (`AGENTS.md` dumps, `<user_info>` blocks) that agents front-load as user messages. A pane that has not been asked anything yet legitimately has no title either way.

Firstmate mode skips this opening-prompt fallback: the opener is frozen at session start, so quoting it on every push reads as a reply to the session's first message — a banner title contributes nothing instead.

## Install

```sh
herdr plugin install kirkins/firstmate-telegram-bridge
```

Plugins hot-load, so no herdr restart is needed. Requires Python 3.11+ (`tomllib`) and `curl`.

## Configure

Create a bot with [@BotFather](https://t.me/BotFather), send it any message, then read your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.

```sh
cp config.example.toml "$(herdr plugin config-dir telegram.bridge)/config.toml"
$EDITOR "$(herdr plugin config-dir telegram.bridge)/config.toml"    # bot_token, chat_id, allowed_user_ids, firstmate.main_cwd
chmod 600 "$(herdr plugin config-dir telegram.bridge)/config.toml"  # it holds your token
```

Then send a test message — either the **Send Telegram test message** action in herdr's plugin menu, or:

```sh
herdr plugin action invoke telegram.bridge.test
```

Run `python3 bot.py --doctor` to check the local runtime and configuration. Add `--telegram` to verify the bot token with Telegram `getMe`; add `--send-test` only when you want a test message sent.

Everything else has a working default. [`config.example.toml`](config.example.toml) documents the full set:

- which `statuses` notify, an `agents` filter
- `settle_ms`, `quiet_seconds`, `snippet_chars`
- `suppress_noop_ack` to drop the routine “Captain, shipshape.” no-op acknowledgement from Telegram entirely (no text, no voice, no notification)
- `markdown` / `fold` / `max_messages`
- `replies` / `reply_to_latest` / `allowed_user_ids` / `read_lines`
- `accept_files` / `inbox_days` / `max_file_mb`
- `[watch] poll_seconds` for the poll-based watcher
- `[stt]` for transcribing voice notes via any OpenAI-compatible endpoint (groq by default)
- `[tts]` for speaking replies via an OpenAI-shaped or Cloudflare Workers AI TTS API
- `proxy` / `api_base` if you need to reach Telegram through something else

To use the bot in a group rather than a private chat, disable its privacy mode in @BotFather (`/setprivacy`) — and note that everyone in that group can then drive your agents unless you set `allowed_user_ids`.

## Running standalone (no Herdr plugin install)

The bridge also runs as a manual systemd --user service — the "switchable on/off,
no autostart" shape. It honors `HERDR_PLUGIN_CONFIG_DIR` /
`HERDR_PLUGIN_STATE_DIR`, so config and state live in your own directories and
Herdr never grows a hook into it. The bot token can live in the Secret Service
instead of the config file (`bot_token_secret`), fetched via `secret-tool` at
startup so it never sits in a file, a command line, or a log.

See [`deploy/README.md`](deploy/README.md) for the full one-time setup (checkout,
config, token, unit) and the day-to-day start/stop, plus `bot.py --announce` —
the one-message live smoke test through the bridge's own send plumbing.

## How it works

Telegram cannot push to a laptop, so `bot.py` long-polls `getUpdates` as a detached singleton. The manifest's `[[startup]]` hook launches it with herdr, the status-change hook re-checks it on every agent transition so a dead poller comes back by itself, and a `flock` keeps exactly one alive. Two actions in herdr's plugin menu — **poller status** and **restart poller** — cover the rest (`python3 bot.py --status|--restart|--stop`).

Routing is a `message id → pane` map recorded as each notification is sent, capped at `msg_map_limit` entries. Replying to something older than the cap reports an error rather than quietly retargeting whichever agent happens to be current — as does replying to an agent whose pane has since been closed. Nothing is ever redirected on your behalf.

### Poll-based watcher

The status-change event hook is the fast path, but herdr may miss events while
panes transition between working and idle. The reply poller also watches the
old-fashioned way: every
`[watch] poll_seconds` (default 20, minimum 5, `0` disables) it runs `herdr agent list`
and diffs each pane's `agent_status` against the same per-pane record the event hook
keeps, feeding transitions through the identical send path — classification, settle
re-check, quiet debounce, firstmate posture, reply mapping. Whichever side notices a
completion, the push looks the same, and the `pane:status` debounce keeps a transition
both catch from being sent twice.

The diff is deliberately focus-independent: `working → idle` counts as `done` for
**every** pane — herdr only reports `done` for panes you are not looking at, and the
point is knowing what finished while you were away with just the phone — `idle →
working` is silent, and any pane entering `blocked` or `done` pushes. The first pass
after a poller start only records the baseline, so a restart doesn't re-announce
everything that was already finished. The watcher rides the reply poller, so
`replies = false` turns it off along with the poller.

Nothing in the watcher drops silently: every status change a poll observes is one
`bridge.log` line (pane, `prev -> new`, and herdr's `state_change_seq` when it reports
one), and every refusal to push — priming, not-news, a message held while the pane
works, an unsettle flap, a debounce, a filtered status or agent, a suppressed no-op
acknowledgement, a send failure — logs its reason. "The pane finished but the phone never rang" is always answerable
from the log.

Sampling statuses can still miss a completion twice over:

- a turn that starts *and* ends between two polls never changes the sampled status (`idle → idle`), and
- a missing herdr event leaves the diff with nothing to say.

So with `[watch] content_delivery = true` (the default), each poll also runs a
**content-anchored pass**: it resolves every pane's transcript — the same per-CLI
resolution the notification snippet uses, pi's cwd-keyed fallback included — and
tracks the identity of the pane's *last assistant message* (the CLI's message id,
else a hash of the text, plus its timestamp). A new final message that has never
been pushed, on a pane observed idle or done, pushes through the same shared path;
while the pane reads working the message is held until it goes quiet.
Firstmate-mode Codex additionally delivers explicitly completed public messages while the main pane is working, as described under [Firstmate posture](#firstmate-posture).

Delivery is deduped strictly by message identity through a
shared sent-marker that the event hook, the status diff and the content pass all
check and stamp: one finished message, exactly one push, no matter which path
noticed it — or how many of them did. Panes whose transcript can't be resolved stay
on the status diff alone; `content_delivery = false` restores that behaviour
everywhere.

The content pass is still *trigger-dependent* in one way — it anchors on the
pane's last message — and under firstmate mode that was proven lossy
(as required for complete, ordered messages): firstmate's main-pane turns carry **several
user-facing text messages** (narration between tool calls), and a pipeline
that only ever delivers "the last message" silently dropped every superseded
intermediate — whether one arrived at all depended on a poll luckily catching
the pane idle while it was newest.

So under firstmate mode the main pane (non-codex CLIs) runs the **mid-run
relay**: delivery is text-block-driven.
Every sealed assistant text block in the session jsonl is a user-facing
message, complete the moment its parseable record lands — thinking and
tool-call records never qualify — and every message after the delivery
boundary relays exactly once, in transcript order, with or without any status
change.

- Interior messages push immediately, even while the pane works; the transcript *tail* keeps the hold-while-working rule (it may still be superseded; it delivers as the final reply on the first idle/done observation).
- A done/idle status push defers behind an undelivered message so the final never jumps ahead of the intermediates it follows.
- Calm's narration rule covers mid-run messages exactly as it does the live transcript, and the same shared sent-marker dedupes everything.
- A delivery boundary the transcript can no longer locate (the session rolled over, no parseable timestamp) falls back to that sent-marker and drains any never-delivered backlog oldest-first, at most 8 messages per poll — flood-safe, and nothing is dropped for being old.

Relayed mid-run messages follow the normal `[tts]`
speak path; `[tts] speak_final_only = true` keeps mid-run messages text-only.
The shipped `false` default speaks each delivered message. With `[firstmate] enabled = false` none of this
exists — the upstream single-slot behavior is exactly as before.

Restart priming never swallows a finished message: the baseline a poller start
records is bounded by the sent-marker, not by whatever the transcript happens to
hold. If the pane's current final message is newer than the last one the phone
actually received — the exact state a watcher freeze mid-message leaves behind —
the restart delivers it instead of priming it (held while the pane works unless the Firstmate Codex completion path proves the message complete,
and a message the marker has already recorded is never re-sent).

The watcher keeps itself honest about stalls: every subprocess it runs is
group-killed at a
hard timeout (a herdr helper that inherits the output pipe outlives the CLI and
turns `subprocess.run`'s own timeout into a permanent hang), and a poll cycle
that exceeds `2 × poll_seconds` logs `watch: slow cycle …` loudly — a frozen
watcher must be visible in `bridge.log` while it is happening, not only after
the restart that recovers it.

A few behaviours of the surrounding systems are worth writing down, because each one caused a bug:

- **`herdr agent prompt` writes the text and the Enter in one burst.** A busy agent queues that fine, but an *idle* Claude Code folds the trailing Enter into its paste-detection window and strands the text in the input box. So when the agent wasn't already working, the poller waits `submit_wait_ms` for it to start and presses Enter once more if it never does.
- **Telegram rejects messages over 4096 characters** rather than splitting them the way the client does when you paste a wall of text. Splitting therefore happens here, and every part maps back to the same agent.
- **Telegram won't nest a code block inside a blockquote** — the client ends the quote, emits the block, then opens a fresh one, so one reply arrives as several separately-collapsing pieces. Inside a fold, code and tables use inline monospace instead; `fold = "never"` trades folding away for syntax highlighting and copy buttons.
- **An untagged `<pre>` gets its language guessed** by the client and highlighted accordingly, which turns a plain table into confetti. Every block therefore carries an explicit tag, `plaintext` included.
- **herdr infers agent state by reading the terminal, and that flaps.** grok flashes `blocked` for about a second as a conversation starts, and a pane can blink through `idle` mid-turn. Every notification therefore waits `settle_ms` and re-reads the pane before sending, dropping the transition if it did not hold.
- **Two processes deliver completions, and “check, send, stamp” is not atomic.** The event hook and the status watcher are separate processes sharing one sent-marker file, and both can pass the marker check while the first's Telegram send is still on the wire — the window is the whole HTTP send — so one completion could reach the phone twice, spoken twice in voice mode. Each delivery therefore takes an exclusive `flock` keyed by pane and message identity, held from the marker read through the stamp: the second delivery waits, re-reads the marker, and skips. A failed send still stamps nothing, so the waiting delivery proceeds and the retry semantics are unchanged.
- **grok files sessions under a url-encoded cwd, not a session id**, and herdr reports no session reference for it — so several conversations share a directory, and a brand new one has no transcript until its first reply lands. Picking the newest by mtime hands back the *previous* conversation, so the session is instead pinned by asking the pane's grok process which `events.jsonl` it holds open.
- **`getUpdates` serves ~24h of backlog on first run.** That is skipped, so the messages you sent the bot while setting up `chat_id` never arrive as prompts.
- **A voice note is one outbound multipart POST.** Telegram voice notes are Ogg/Opus, which the OpenAI-compatible transcription endpoints take natively (Groq's Whisper among them) — no transcoding, no ffmpeg. The API key rides to curl in a 0600 header file (`-H @file`), never in argv, and every diagnostic string is blanked of it before logging — the same hygiene the bot token gets.
- **Spoken replies are one JSON POST.** Cloudflare Workers AI has no OpenAI-compatible speech route (`.../ai/v1/audio/speech` → 400), so the cloudflare provider speaks the native `/ai/run/{model}` shape with *both* input spellings (`text` for Deepgram Aura, `prompt` for melotts) — the model families each reject the other's field alone but accept the pair.
- **Spoken audio converts only when needed.** Whatever arrives that is not already Ogg/Opus converts through ffmpeg; an OpenAI-shaped provider with `response_format = "opus"` needs no conversion at all. The key gets the same `-H @file` hygiene as every other credential.

Logs are in `bridge.log` in the plugin state dir, plus `herdr plugin logs --plugin telegram.bridge`.

## Limits

- Telegram bots cannot download files larger than **20 MB**; `max_file_mb` only lowers that further.
- The injection channel is text, so an attachment reaches the agent as a *path*, not as pixels. Agents that open files themselves (Claude Code among them) handle that transparently; others just see a path.
- Interrupting is a plain Escape keypress. If an agent is sitting on a permission dialog rather than a prompt, `/esc` and the Enter fallback act on that dialog — set `submit_fallback = false` if you would rather they never did.

## Firstmate posture

Firstmate is an agent fleet manager: it coordinates a main agent and worker
agents. Fleet mode keeps the phone connected to the main pane, with worker
identity carried as context so replies always go through the main agent. The
bridge is based on `cokekitten/herdr-telegram-bridge`; see the MIT attribution
in [LICENSE](LICENSE).
Replies must route only to the firstmate main pane — never straight into worker
panes — and the phone itself carries only the main pane's messages: outbound
relay is an allowlist of one, so worker and sub-agent sessions never notify.

The entire divergence lives behind one switch in `config.toml`:

```toml
[firstmate]
enabled = true
main_cwd = "~/firstmate"   # replace with the exact Firstmate main-pane cwd
```

With `enabled = true` (the default in `config.example.toml`) the bridge uses
fleet routing. Set it to `false` for generic per-pane Herdr behavior. With fleet
mode on:

- **Main-pane identification.** The firstmate main pane is found by an exact `cwd`
match against `herdr agent list` — CLI type can't tell main from scouts (both are
pi), pane ids churn across restarts, and terminal titles are CLI-controlled, but
the firstmate home directory is stable and unique. An optional `main_pane` pin
(short-lived pane id) is revalidated live if set. If several panes match — a worker
that `cd`'d into the home — the one no `state/*.meta` claims wins; if it is still
ambiguous, **replies fail closed**: a clear Telegram error, never a silent
retarget into a worker pane. The outbound allowlist keys on the same identity:
if the main pane can't be resolved, nothing relays (fail closed).
- **Workers never notify.** Outbound relay under firstmate mode is the main pane
  only — an exact pane-id identity check on the resolved main pane
  (`firstmate.relay_denied`, consulted by the one send path), so worker, scout and
  foreign panes never reach the phone. Every refusal is one `bridge.log` line, and
  a refused pane costs nothing (not even settle/debounce work). There is no config
  key to turn worker notifications back on; an older `[firstmate] worker_notify`
  setting is ignored, and startup logs a warning naming it if present.
- **Codex without a Herdr session reference.** Run `codex_binding.py` from a tool shell **inside the primary Codex conversation**, using the bridge's config and state directories (see [deployment](deploy/README.md#codex-main-session-binding)). A managed app-server tool shell additionally needs `--primary-session UUID`, supplied by the primary owner from that conversation's verified native reference.
  - The caller's native thread must match, and its nearest Codex must be the installed managed app-server with a validated process lifetime. Workers cannot supply the authority or borrow a more distant main ancestor.
  - It records that conversation's native `CODEX_THREAD_ID` in `codex_main.json`, bound to the live main pane and Codex process start time. Explicit Herdr references still take priority. Two conversations may share a cwd: the binding selects one exact id, never the newest rollout or a matching title.
  - On each read, a changed foreground or bound app-server process, missing/duplicate rollout, or changed competing same-cwd rollout refuses delivery until the current primary conversation rebinds. This includes creating or resuming a different session in the same process; same-cwd worker activity also refuses rather than guessing. Sessions in other worktrees do not invalidate it.
  - Rebind after switching conversations; do not copy a worker's thread id or install an old binding snapshot. Unresolved Codex completions log a reason and send no empty completion banner. Pi and default-off behavior keep their existing resolution and framing.
- **Complete Codex chat updates during work.** With the content pass enabled, the trusted main Codex session can send complete commentary and answer messages while its pane is working.
  - Each update requires a sealed native `item_completed` record for a public `AgentMessage` paired with the persisted assistant `response_item`, matching message id, phase and text, the exact thread, and turn metadata when supplied. Started items, deltas, unsealed or mismatched records, reasoning, tool output, private agent-to-agent chat, and worker panes cannot qualify.
  - The watcher sends undelivered complete pairs in transcript order through `notify.deliver()`, rechecking the same session and completion evidence before sending. Message identities remain shared with final/status notifications, and only successful sends advance the sent marker and delivery boundary; failures retry without skipping the following update.
  - A fresh install primes existing history, while a restart with delivery history resumes after its recorded boundary. Reads use a bounded transcript tail; a complete pair must fit in that tail, and an unavailable native delivery boundary refuses updates with a logged reason until a normal final establishes one.
  - This provides message completion during a long turn; it does not refresh an expired pane/session binding or infer completion from inactivity.
- **Mid-run relay for every other main-pane CLI** (pi, Claude, generic jsonl shapes): the same completeness guarantee from the session formats' own records — a sealed text block is a finished user-facing message, thinking/tool-call records never qualify. Every message after the delivery boundary relays exactly once, in transcript order; interiors push immediately while the pane works, the transcript tail holds until the pane reads idle/done (it may still be superseded), and a status completion defers behind an undelivered message so the phone reads in order. `[tts] speak_final_only = true` keeps mid-run relays text-only while finals still speak. The shipped `false` default speaks every delivered message. With firstmate mode off this path does not exist (upstream behavior).
- **Workers are addressable only as context.** A plain (non-reply) message goes to
  the main pane — always, including the very first message on a brand-new install
  before any notification has ever been sent. `/agents` can still name a worker as
  *sticky context*: the next plain message submits to firstmate with a prefix like
  `[opencode/bridge-fork-scout pane wA:p2]`, so firstmate itself sees which worker
  you meant. Attachments route identically (the path lands in the main pane,
  prefixed the same way).
- **Commands.** `/agents` buttons still show every pane, but tapping a worker
 makes it *sticky context* for the next plain message (which goes to main with the
 prefix); tapping the main pane clears the context. `/read` stays read-only and —
 alone — may name a worker pane (the one the sticky context carries). `/esc`
 interrupts the **main pane only**: interrupting a worker is firstmate's job, not
 the phone's. `allow_worker_esc = true` in `[firstmate]` restores `/esc` on a
 worker for operators who want the hatch.
- **`allowed_user_ids` becomes mandatory.** Firstmate mode without it refuses the
 control channel entirely: notifications still arrive, but replies and `/esc` get
 a clear error (“notify-only degrade”). This keeps a group chat from turning into
 a fleet remote control. Note the key stays at the *top level* of `config.toml` —
 it is not read from inside `[firstmate]`.
- **Standalone notification framing.** Every push reads as a fresh standalone message, never as a reply to old session content. Three firstmate-mode defaults make that so:
  - the header never carries the session's opening prompt (it is the session's *first* message and stays frozen there — quoting it on every push reads as a reply to it; a CLI banner title contributes nothing instead);
  - long replies are **not** folded — Telegram renders an expandable blockquote like a reply quote, so the body goes out as plain rendered text with syntax-highlighted code;
  - the **main pane's push carries no header line at all**: `✅ pi · /cwd` names the one pane you can already reply to, so the final reply is the whole message.

  Under firstmate mode the main pane is also the only pane that pushes at all. An explicit top-level `fold` key still wins if an operator wants folding back, and `[firstmate] main_header = true` restores the main pane's header; with `[firstmate] enabled = false` all of these are exactly upstream's (opener fallback, `fold = "auto"`, full headers).
- **Calm-aware excerpts.** If the firstmate home's Calm preference
 (`config/calm` in the home, values `on`/`off`, absent = off) is on, notification
 bodies for firstmate-home panes obey Calm's preservation rule: mid-turn
 narration — a settled single-line block under 240 trimmed characters — is
 dropped from the excerpt, substantive blocks (a newline, or ≥240 trimmed
 characters) are kept, and the genuine final reply that ends a turn is always
 kept. `/read` output is raw terminal by design and is never filtered.

**Divergence policy:** every fork change is either a firstmate helper file, a
new config section, or a guarded one-to-three-line hook in `bot.py`/`notify.py`
of the form `if firstmate.enabled(cfg):`, with config passed through the shared
watcher — plus two small upstream-safe fixes that
ship unconditionally (token redaction in Telegram API error strings, and
quote-escaping of link URLs in the markdown renderer). Pulling from upstream should
stay cheap; conflict resolution is confined to those anchor points.

## License

MIT — see [LICENSE](LICENSE).
