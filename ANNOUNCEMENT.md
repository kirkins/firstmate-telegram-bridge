# Discord announcement

**Firstmate Telegram Bridge** lets you walk away from your agent fleet and answer from your phone. It sends complete agent replies to Telegram, routes replies through the Firstmate main pane, supports voice notes in (Groq transcription) and spoken replies out, and can run as a Herdr plugin or a standalone systemd service. MIT licensed; based on cokekitten’s MIT-licensed herdr-telegram-bridge.

**Fleet mode is on by default:** worker sessions do not message your phone or receive prompts directly. They can be selected as context, with your reply still going through the main pane.

Quick start: create a bot with @BotFather, set `bot_token`, `chat_id`, `allowed_user_ids`, and `[firstmate].main_cwd` in `config.toml`, then install: `herdr plugin install kirkins/firstmate-telegram-bridge`. For speech, configure `[tts]` and turn it on with `/voice`; voice notes in use a Groq API key under `[stt]`. See the README for the full setup and generic Herdr opt-out.

Repo: https://github.com/kirkins/firstmate-telegram-bridge
