#!/bin/sh
# Offline test battery: no network, no live herdr, no real firstmate state, no real
# bot token. herdr and the Telegram API are stubbed at the tg module boundary.
cd "$(dirname "$0")/.." || exit 1
exec python3 -m unittest discover -s tests -p 'test_*.py' -v
