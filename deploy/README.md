# Deploying as a persistent systemd --user service

The "comes back after every reboot" deployment for a single home. The bridge
needs no Herdr plugin install for this: it honors `HERDR_PLUGIN_CONFIG_DIR` /
`HERDR_PLUGIN_STATE_DIR`, so config and state live in ordinary XDG-ish paths and
Herdr itself never grows a hook into it.

## One-time setup

1. **Checkout** the fork somewhere stable (not a disposable worktree):

       git clone https://github.com/kirkins/firstmate-telegram-bridge.git \
         ~/.local/share/herdr-telegram-bridge

2. **Config** — copy `config.example.toml` to
   `~/.config/herdr-telegram-bridge/config.toml`, set `chat_id`,
   `allowed_user_ids`, and `[firstmate].main_cwd` for your Firstmate main pane.
   Fleet mode is enabled by default. Set `[firstmate] enabled = false` for
   generic per-pane Herdr behavior. Do NOT put the bot token in the file.

3. **Token** — store it in the Secret Service and name it in the config:

       printf %s 'THE-TOKEN' | secret-tool store \
         --label='herdr-telegram-bridge token' \
         service herdr-telegram-bridge account bot_token

   then in `config.toml`:

       bot_token_secret = { service = "herdr-telegram-bridge", account = "bot_token" }

   The token is piped from its source into the keyring, fetched via
   `secret-tool lookup` at startup (the secret travels on stdout, never on a
   command line), and never appears in `config.toml`, the journal, or
   `bridge.log`. Verify with a masked read:

       secret-tool lookup service herdr-telegram-bridge account bot_token | head -c 8

4. **Unit** — install and reload:

       mkdir -p ~/.config/systemd/user
       cp deploy/herdr-telegram-bridge.service ~/.config/systemd/user/
       systemctl --user daemon-reload
   systemctl --user enable herdr-telegram-bridge

5. **Validate** the code you are about to run: `sh tests/run.sh` (offline,
   stdlib-only).

## Day to day

    systemctl --user start herdr-telegram-bridge   # switch on
    systemctl --user stop herdr-telegram-bridge    # switch off

Once enabled, the bridge also autostarts at login: the unit ships an
`[Install]` section (`WantedBy=default.target`), so it comes back after every
reboot. Because the bot token comes from the Secret Service (via the
keyring), startup can fail early in a login session while the keyring is
still locked or the network is not up yet — the unit handles that itself:
`StartLimitIntervalSec=0` plus `Restart=on-failure`/`RestartSec=15` means it
keeps retrying every 15s indefinitely instead of systemd giving up, so the
retry loop bridges the keyring-unlock-at-login gap. If you do not enable the
unit, it stays manual-only. Telegram retains updates ~24h server-side, so
messages sent while the bridge is off arrive (in order) when it is switched
back on.

## Codex main-session binding

When firstmate mode is enabled and Herdr omits the main pane's Codex session
reference, the primary Codex agent runs this command through its own tool shell:

```sh
HERDR_PLUGIN_CONFIG_DIR=~/.config/herdr-telegram-bridge \
  HERDR_PLUGIN_STATE_DIR=~/.local/state/herdr-telegram-bridge \
  python3 ~/.local/share/herdr-telegram-bridge/codex_binding.py
```

The tool shell supplies native `CODEX_THREAD_ID`. For a pane-owned tool shell,
its nearest Codex ancestor must be the resolved main pane's foreground process.
Managed app-server tool shells have a separate ancestry, so that command refuses
until the primary owner supplies the current primary conversation's verified
native UUID explicitly:

```sh
HERDR_PLUGIN_CONFIG_DIR=~/.config/herdr-telegram-bridge \
  HERDR_PLUGIN_STATE_DIR=~/.local/state/herdr-telegram-bridge \
  python3 ~/.local/share/herdr-telegram-bridge/codex_binding.py \
    --primary-session "$CODEX_THREAD_ID"
```

Run this only in a tool shell from the current primary conversation, where
`CODEX_THREAD_ID` is supplied by that native context. Do not copy a worker's
UUID or let a worker choose the reference. The helper checks that the supplied
value equals its native caller thread, then validates the caller and target
processes plus the exact rollout metadata; the value alone grants no authority.
The command requires equality with the caller's native thread, the nearest Codex
ancestor's installed app-server executable, `app-server` mode, user ownership
and unchanged process lifetime. Supported executable locations are
`~/.codex/packages/app-server-daemon/releases/<release>/bin/codex` and
`~/.local/share/mise/installs/codex/<version>/bin/codex`, where the mise version
directory is a direct semantic-version release. It also requires one live
foreground Codex process for the resolved main pane. A renamed process, ordinary
shell, nearer worker Codex, or mismatched worker/old thread is refused.

Both paths check the exact rollout's native id/cwd metadata and recheck process
lifetimes after scanning before atomically writing `codex_main.json` in the
bridge state directory. The managed binding records both the app-server and
foreground lifetimes; a change to either invalidates it. No credentials are
fetched and no messages are sent.

Run the appropriate command from the current primary conversation after `/new`,
`/resume`, a Codex process replacement, or a main-pane replacement. Reads fail
closed on a changed PID/start time, a missing or duplicate exact rollout, or any
change to another same-cwd rollout since binding. This last check detects
in-process replacements and conservatively refuses active same-cwd workers too;
it never retargets to those conversations. A rollout that cannot be read at all
(e.g. a zero-byte file left by a crashed session) is fingerprinted as an opaque
candidate instead of failing the scan: a stable one never blocks binding, while
a new or changed one still refuses delivery like any competitor. Bind only
after the intended primary session is active. For the managed path, obtain its
new owner-verified reference each time; an old UUID cannot authorize a new native
thread. The snapshot is metadata, not a portable installation file.

An ordinary shell can check reply health without changing the binding by
adding `--check` to the command. It uses a current explicit Herdr reference when
present, otherwise the existing binding, without fetching credentials or sending
a message. It reports `handoff_required` for `missing_binding`,
`primary_replaced`, `process_lifetime_changed`, or `conversation_ambiguous`,
with the owner-registration instructions above. `source_unavailable` reports
`retrying`: restore access to the same source and let polling retry it. An
unavailable explicit reference never falls back to the old binding or a sibling
rollout.

The watcher sends a Telegram recovery notice for a handoff-required condition,
once per configured home, pane/process lifetime, and refusal reason until
recovery. Successful notices are remembered in `bridge_health.json` across
poller restarts; failed sends retry. Diagnostic retention is bounded (seven days,
32 notice identities per scope, 500 scopes), independently of reply cursors and
delivery markers. A valid source or a switch to another current harness clears
that home's recovery state. Transient agent-list/process-info outages do not
request a new registration.

The current primary owner must still register its verified session after a
replacement when Herdr omits the reference. The bridge does not discover or
manufacture a replacement identity from newer files, cwd, pane index, title,
shared PID, or an old hook UUID. A self-consistent UUID is insufficient authority
for asynchronous handoff; trusted selected-conversation/generation production
remains a separate requirement. The helper is an explicit owner workflow.

Normal polling reloads the binding, clears health/notice state when the source
becomes valid, and resumes ordered delivery without a service restart or cursor
reset. If a Codex completion has no displayable reply, firstmate mode logs it
rather than sending a title banner. Fresh-install priming and bounded transcript
history still apply; recovery does not promise unlimited historical replay.

If a native delivery cursor is absent from the bounded rollout tail and has no
native timestamp, the watcher checks the shared content-sent marker and drains
only unmarked, sealed public completions in transcript order, at most eight per
poll. It advances after each successful delivery, so a failed send and newer
completions remain ordered for retry rather than being primed away.

After an authorized code installation, verify through a normal reply in the
primary conversation: its actual final text should arrive once, without the old
session title, and a later poll/completion should not repeat it. Check
`bridge.log` and the successful-send marker in `content_sent.json`. Incoming
Telegram routing remains on the main pane. Do not use `--test`/`--announce` as a
substitute for verifying normal reply delivery.

## Spoken replies (voice output)

`/voice` toggles speaking agent replies as Telegram voice notes; it needs a
`[tts]` section in the config (see `config.example.toml` for the full set) and
`ffmpeg` on PATH whenever the provider does not return Ogg/Opus itself
(Cloudflare's models return MP3 or WAV; an OpenAI-shaped provider with
`response_format = "opus"` does not).

For the Cloudflare provider, copy your account id from the Cloudflare
Dashboard and create an API token with the Workers AI permission required by
your selected model. Prefer keeping the token out of the file by piping it into
the keyring once and naming the entry via `api_key_secret`:

    printf %s "$CLOUDFLARE_API_TOKEN" | secret-tool store \
      --label='herdr-telegram-bridge tts' \
      service herdr-telegram-bridge account tts_api_key

The token then travels the same path as the bot token: fetched via
`secret-tool lookup` at use, never in the config file, a command line, or a
log. Read values only when configuring or redeploying; never print them.

## Live smoke test

The unit sets `HERDR_PLUGIN_CONFIG_DIR`/`HERDR_PLUGIN_STATE_DIR` only for the
daemon — a plain shell has them unset, and without them the bridge looks for
`config.toml` next to the code. Carry the same paths the unit uses (run from
the checkout):

    HERDR_PLUGIN_CONFIG_DIR=~/.config/herdr-telegram-bridge \
      HERDR_PLUGIN_STATE_DIR=~/.local/state/herdr-telegram-bridge \
      python3 bot.py --announce 'bridge connected'

sends one message to the configured chat through the same send plumbing the
notifications use — proving config, token resolution and the Telegram path
without typing into any pane.

## Future option: the Herdr plugin route

Installing as a Herdr plugin instead (`herdr plugin install <path>`) buys
auto-start with Herdr and self-heal via the `[[startup]]`/`[[events]]` hooks in
`herdr-plugin.toml`, at the cost of "always on" becoming install/uninstall.
That route stays available; this deployment deliberately does not use it.
