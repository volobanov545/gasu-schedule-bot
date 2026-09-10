# Research log

This is the chronological evidence log for SZS Hub. Claims become architectural decisions only after they are reproduced or corroborated.

## 2026-08-29 — Environment discovery

- Created an isolated project directory at `szs-hub`; the synced `sources/` directory remains untouched and read-only.
- Initialized a local Git repository on branch `main`.
- Host Git: 2.55.0 for Windows.
- Host Node.js: 24.18.0.
- Docker CLI was not found on `PATH`; Docker is therefore not assumed for local development.
- System Python was not found on `PATH`. A Codex-bundled Python runtime is available and will be used for development checks until a project-local runtime is selected.
- The workspace contained no inherited application source code or project-specific implementation to preserve.
- Windows Computer Use discovery failed twice with `EPERM` while reading the Codex application path. No settings or permissions were changed; terminal-based discovery remains available.
- The requested Chrome surface was not connected when first probed. Browser research can continue through public sources, while Chrome-only Network validation remains an explicit pending experiment.

## Research rules

- Prefer official current documentation and official SPbGASU endpoints.
- For critical assumptions, record official evidence, independent evidence, and a reproducible experiment when feasible.
- Never place tokens, cookies, passwords, private group content, or personally identifying exports in this log.
- Record failures and negative results; they are evidence too.

## 2026-08-29 — Telegram official-documentation pass

- Verified Bot API 10.3 and captured forum-topic, per-user reaction, membership, update retention, copy/delete, album, file, private-chat, Ephemeral, and Rich Message constraints in `RESEARCH.md`.
- Selected a durable update inbox and outbox boundary; reaction attendance cannot be reconstructed after missed updates.
- Rejected a production userbot: topic/history bootstrap can be done with lower-risk explicit inputs.

## 2026-08-29 — SPbGASU connectivity probe

- Confirmed the university's public navigation path to `rasp.spbgasu.ru`.
- DNS resolved the host and TCP/443 connected, but application requests timed out over the active tunnel route.
- Kept endpoint/schema claims open pending official HTTP or Chrome Network evidence.

## 2026-08-29 — Local implementation bootstrap

- Created a Python 3.12 virtual environment and installed the initial application/test toolchain.
- Added typed fail-closed configuration, semantic schedule diffs, idempotent attendance policy, and conservative material workflow.
- Verification result after the first domain slice: 20 tests passed; Ruff and strict MyPy passed.

## 2026-08-29 — Hosting, storage, search, and AI comparison

- Compared current full costs for 4VPS, RUVDS, Cloud.ru, Yandex Cloud, Selectel, Timeweb, VK Cloud, and a home device.
- Selected a 4VPS long-polling candidate with external Yandex Object Storage backup, subject to burn-in and SPbGASU reachability; documented RUVDS and home-host fallbacks.
- Selected SQLite/FTS5 with one writer and Russian Snowball normalization; confirmed local SQLite 3.53.1 is newer than the WAL-reset fix.
- Added explicit AI usage/budget controls because token spend can exceed compute even at low per-call cost.
- Added Russian tokenization/stemming, Telegram Desktop JSON import planning, safe media-path handling, HTML escaping, provider failure handling, and membership/reaction adapters.

## 2026-08-29 — SPbGASU public endpoint

- Confirmed the full-time public GET contract, its `SERACH` typo, fixed filters, response wrapper variants, group-not-found behavior, and client-side parity switching.
- Recorded the dangerous parameterless aggregate response and fixed the application client so it cannot construct one.
- Implemented a five-minute-or-longer cached client, concurrent request collapse, two-megabyte response ceiling, transient-only retries, weekly-template parser, official bell-slot materialization, and current-week parity calculation.
- Kept the IBFO Bitrix POST out of the production path until exact Chrome Network serialization is available.
