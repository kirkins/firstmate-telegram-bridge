# Installed bridge edit reconciliation

The 2026-10-14 read-only capture of the installed `watch.py` and
`codex_binding.py` used installed base
`ee8573d9f113a9abbd2dde1c3aa295f0f28d103c`. Both files' contents and stat
identities remained unchanged throughout capture.

Every captured edit is already committed in the history leading to
`5db32f3b8de38cd3868dc75c734e406ce45f2320`, the merged Claude missing-reference
reply fix. The installed `watch.py` blob is byte-identical to the file at
`860a4f93065b7bd61faf78b802fd1aade72e5eec`; the installed `codex_binding.py`
blob is byte-identical to the file at
`a135ef4af9e35e5cb24dca3f29d95233feee9862`.

Three-way reconciliation of current/base/installed returned current unchanged
for both files. Reverse-application checks of both complete captured patches
against current also succeeded. Keeping current therefore retains every local
edit together with subsequent upstream recovery behavior and the Claude fix.
There is no semantic conflict to resolve or source change to replay. This
reconciliation adds executable coexistence coverage and this record; it does
not perform installation.

## Capture identities

| File | Installed base blob | Captured installed blob |
| --- | --- | --- |
| `watch.py` | `cdc07f96b29927d944b3092936b9236eb830ae76` | `d14fc534ecd4874cf22fe817a5fb0b30fb6802c2` |
| `codex_binding.py` | `4fbcd4f91767583fdec8e5e707b37f6902df9da2` | `324e279a8dc4cd263f3bf841f8c338bdae04a549` |

Captured binary-patch SHA-256 values:

- `watch.py`: `af55d5dab5a8acb9aded6d6979b099fb4c230edcf6726c543ad839312abeabbc`
- `codex_binding.py`: `43eba5c91cfae45cce15e82daee2e91dcb27a8c5d25a7ee69002823128c9dc0b`
- Combined, watch then Codex: `aae1d7ed00d406a4691339eb86bf0cb16ec8606873bfbed6456a4bf4dfe13304`

This capture contains 19 additions/11 deletions in `watch.py` and
30 additions/7 deletions in `codex_binding.py`. Those fresh patch counts differ
from the preceding deployment preflight's reported 147/238 changed lines;
the identities above describe the two files captured for this reconciliation.

## Complete retained-hunk inventory

The ranges below identify hunks in the installed-base-to-installed patches,
rather than current file line numbers. All watch hunks were committed in
`860a4f93065b7bd61faf78b802fd1aade72e5eec`; all Codex hunks were committed in
`a135ef4af9e35e5cb24dca3f29d95233feee9862`.

| Captured hunk | Retained behavior in current |
| --- | --- |
| watch `-236,6 +236,9` | Shared eight-message `LOST_BOUNDARY_MAX_PER_POLL` cap. |
| watch `-262,6 +265,7` | Initialize native backlog's pending-cap result. |
| watch `-273,10 +277,17` | Unknown native boundary drains oldest unsent messages by sent-marker; known timestamps still filter subsequent messages. |
| watch `-287,10 +298,7` | Return pending when native backlog exceeds the cap; remove the separate mid-run cap. |
| watch `-309,7 +317,7` | Non-Codex lost-boundary diagnostic reports the shared cap. |
| watch `-364,8 +372,8` | Non-Codex backlog uses the same cap and pending calculation. |
| Codex `-24,7 +24,10` | Mise install root and version validation pattern. |
| Codex `-63,14 +66,27` | Direct installed binaries from managed-release or mise roots pass executable validation; owner, mode and lifetime checks remain. |
| Codex `-339,8 +355,9` | `prepare_binding` validates an exact owner-bound candidate without persisting it. |
| Codex `-373,6 +390,12` | `bind` persists the validated candidate and returns it. |

## Executable verification

`tests/test_codex_binding.py` already exercises mise registration, preparation
without writes, owner/mode refusals, capped legacy-boundary recovery, confirmed
sent-marker skips and transport-failure retries. `tests/test_midrun.py` exercises
non-Codex capped backlog recovery. `tests/test_claude_session.py` exercises the
merged missing-reference fallback and ownership refusals.

`test_preserved_mise_binding_and_backlog_coexist_with_claude_fallback` additionally
runs the preserved behaviors in one watcher history: prepare a mise-owned
candidate without writing state, persist it, drain a legacy-boundary backlog
eight unsent messages at a time, then observe a Claude foreground owner with
an omitted session reference. The Claude reply is delivered exactly once while
the Codex binding and all prior sent-markers are retained. Herdr, Telegram,
process metadata, transcripts and state are synthetic offline fixtures.
