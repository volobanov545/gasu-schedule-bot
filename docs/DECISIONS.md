# Decision journal

Decisions record the evidence available at the time. A decision is revised when its stated trigger occurs.

## D-001 — Use the normal Telegram Bot API, not a userbot

**Problem.** Existing forum topics and historical messages cannot all be queried through Bot API.

**Options.** Bot API with explicit bootstrap and Telegram Desktop export; persistent MTProto userbot; mixed bot/user session.

**Evidence and tests.** Bot API supports topic-targeted messages/copy, per-user reaction updates, membership checks, and all planned live automation. Topic enumeration and full historical retrieval are user-only, but each has a safer bounded bootstrap: topic IDs from real messages/links and history from an owner-created Telegram Desktop JSON export.

**Decision.** Use Bot API only in production. Do not operate a user account session.

**Why.** It reduces account-ban risk, secret scope, operational burden, and destructive capability while preserving the product goal.

**Revisit when.** Telegram publishes a bot-safe historical/topic enumeration API or a required acceptance scenario is proven impossible through the documented bootstrap.

## D-002 — Treat Telegram updates as a durable event stream

**Status.** Dormant `full` profile only; selected v1 does not receive updates (D-010).

**Problem.** Reactions and some membership facts cannot be reconstructed through read APIs, webhook delivery can repeat, and pending updates expire.

**Options.** Process handlers directly; store only final business rows; persist an inbox and process idempotently.

**Evidence and tests.** Telegram documents `update_id`, webhook retry behavior, a 24-hour pending-update limit, explicit reaction/member allowlisting, and no endpoint for the full per-user reaction set.

**Decision.** Persist every accepted raw update with a unique `(bot_instance, update_id)` before business processing. Handlers use unique business keys and an outbox for external side effects.

**Why.** It makes retries and restarts safe and gives an auditable recovery boundary.

**Revisit when.** Never for current scale; storage retention may be shortened after processing if privacy and diagnostics support it.

## D-003 — Copy-before-delete, with source deletion disabled initially

**Problem.** Automatic material organization can destroy ordinary conversation if classification or copy fails.

**Options.** Delete then repost; copy then delete; copy only; metadata-only catalog.

**Evidence and tests.** Telegram copy and delete are non-atomic; deletions are permission- and age-limited. Domain tests prohibit a delete transition before a verified destination ID. A generic PDF alone scores below the review threshold; explicit academic context is needed for auto-copy.

**Decision.** Copy → verify → index. Source deletion is a separately configured action, false by default, and only reachable after copy success.

**Why.** It preserves user content under uncertainty and makes the rollout reversible.

**Revisit when.** Staging false-positive tests and a production UX trial show a sustained safe threshold, followed by explicit owner approval.

**Implementation note, 2026-08-31.** The current runtime implements classify → copy or
review → verify/index and has no Telegram source-delete transition at all. The configured
delete flag is therefore reserved and the effective recovery manifest records false.

## D-004 — HTML schedule cards are the compatibility baseline

**Problem.** New Rich and Ephemeral primitives improve UX but were released days before this design and may not be uniformly supported.

**Options.** Rich-only UI; legacy plain text; HTML/entities with optional Rich enhancement.

**Evidence and tests.** Bot API 10.1–10.3 introduced Rich/Ephemeral features in June–August 2026. Telegram documents non-guaranteed ephemeral delivery/lifetime.

**Decision.** Use concise HTML/entity cards for all essential schedule and attendance information. Add Rich/Ephemeral rendering only behind capability/configuration flags after a client matrix spike.

**Why.** Critical daily information remains reliable while the newer UI can be tested safely.

**Revisit when.** Staging confirms current aiogram support and acceptable behavior across the clients used by the group.

## D-005 — One long-polling modular monolith on a small Russian VPS

**Status.** Superseded for v1 by D-010; retained for the dormant `full` profile.

**Problem.** The complete system must stay below 500 ₽/month while keeping local search, jobs, document processing, and simple recovery.

**Options.** Yandex/Cloud.ru serverless; managed PostgreSQL; 4VPS/RUVDS small VPS; home device; larger Timeweb/Selectel/Yandex VM.

**Evidence and tests.** Current full-price comparisons are in `COST.md`. Serverless requires external state and a different job/OCR model. Managed PostgreSQL and mainstream VMs exceed the cap. 4VPS publishes 2 vCPU/2 GB at 200 ₽ including IPv4; RUVDS publishes a more conservative 1 GB SSD plan at 349 ₽.

**Decision.** Build for one Linux host with Telegram long polling. Use 4VPS after a 7–14-day burn-in and real SPbGASU connectivity test; fall back to RUVDS if it fails. Keep the artifact portable to a home device.

**Why.** It retains the simplest reliable SQLite/FTS/OCR architecture, removes webhook/HTTPS/NAT cost, and leaves budget for backup and AI.

**Revisit when.** Candidate pricing changes, burn-in fails, the provider cannot reach SPbGASU, or one-process write serialization becomes a measured bottleneck.

## D-006 — SQLite + FTS5 now, PostgreSQL only on a measured trigger

**Problem.** The archive needs durable relational state and Russian full-text search under very low load and RAM.

**Options.** SQLite/FTS5; local PostgreSQL; managed PostgreSQL; separate vector database.

**Evidence and tests.** One runtime process with short core/material/update/outbox
transactions for 20–40 people produces little concurrent write pressure; SQLite
serializes the actual writer. SQLite includes FTS5/BM25 and Online Backup without a
resident DB server. Managed PostgreSQL alone violates the budget. The current runtime
SQLite is 3.53.1.

**Decision.** SQLite on local SSD, one writer, short transactions, FTS5 and application-side Russian stemming/aliases. Gate WAL on a safe SQLite release because versions 3.7.0–3.51.2 have the documented 2026 WAL-reset bug.

**Why.** Lowest operational and memory cost with an explicit migration boundary.

**Revisit when.** Multiple writer processes, sustained `SQLITE_BUSY`, remote multi-user admin, HA/PITR, or corpus/ranking measurements require PostgreSQL.

## D-007 — BM25 first; embeddings require evaluation evidence

**Problem.** Russian group search must handle abbreviations and morphology without making AI/storage expensive.

**Options.** Vector-first search; PostgreSQL/pgvector; SQLite FTS5 BM25 with normalization; hybrid from day one.

**Evidence and tests.** The small corpus is rich in exact names, filenames, topics, dates, authors, and group-specific aliases. Russian Snowball stems plus `ЖБК ↔ железобетонные конструкции`-style aliases address the highest-value misses locally.

**Decision.** Exact metadata filters + FTS5/BM25 + Russian normalization and surrounding/reply context. Build an evaluation set before adding embeddings.

**Why.** Explainable, private, cheap, and sufficient to establish a quality baseline.

**Revisit when.** A representative query set repeatedly misses semantic paraphrases after alias/query-expansion tuning.

## D-008 — AI is metered enhancement, never the core control plane

**Problem.** Even low per-token prices can exceed the full 500 ₽ ceiling at thousands of requests, and the owner's DeepSeek-compatible service can fail or change model IDs.

**Options.** Hard-code a DeepSeek model; send every workflow through AI; use a configured provider boundary with quotas and graceful degradation.

**Evidence and tests.** Current DeepSeek production model IDs changed in July 2026. Pricing calculations in `COST.md` show that uncontrolled usage can exceed compute cost. Provider tests cover retryable errors, malformed responses, and secret redaction.

**Decision.** Provider/model/capabilities remain configuration. Local search retrieves bounded context. Per-user/global quotas, usage accounting, cache, circuit breaker, and kill switch protect the budget. Schedule, attendance, archive, and local search do not depend on AI availability.

**Why.** It preserves product utility and cost control during provider failure or commercial changes.

**Revisit when.** A provider offers a verifiable fixed free quota or local inference becomes reliable within the host budget.

## D-009 — Use the public SPbGASU weekly JSON endpoint

**Problem.** The bot needs authoritative schedule data without student accounts or brittle per-fetch browser automation.

**Options.** Public weekly JSON; Excel export; HTML/browser parsing; authenticated portal/PWA; IBFO Bitrix action.

**Evidence and tests.** The official schedule page embeds `rasp.spbgasu.ru`. Its first-party client uses `ajax.php` with fixed `SERACH`, `FILTER=GROUPS`, `GROUP=`, `SELECT=*`; a valid unknown group returns `[]` without authentication. Day and parity switching are local. Unit tests verify the exact misspelled parameter, response wrappers, cache, size limit, slot mapping, and unknown-group behavior.

**Decision.** Full-time schedule uses the public JSON endpoint, one cached weekly request per group, and immutable snapshots. The bot never emits a parameterless aggregate request. IBFO remains a separate adapter after a Chrome Network serialization spike.

**Why.** It is the lightest official source and removes browser/session credentials from the daily path.

**Revisit when.** Origin responses change, provider-host reachability fails, official terms/API guidance changes, or the chosen SZS group is served only through the IBFO flow.

## D-010 — Ship only an outbound schedule profile first

**Problem.** The broader bot would process messages, files, reactions, member IDs, and AI
context that are unnecessary for the owner's immediate need and materially increase legal
and operational work.

**Evidence and tests.** The schedule source is group-scoped and public. The application can
publish scheduled cards without receiving Telegram updates. Tests confirm the minimal
configuration needs no student/headman ID, only schedule/heartbeat jobs are registered,
non-schedule outbox envelopes are denied, and teacher names are removed before persistence.

**Decision.** Default to `FEATURE_PROFILE=schedule_only` with
`TELEGRAM_MODE=outbound_only`. Do not construct a Telegram dispatcher/inbox, archive,
attendance, materials, membership, or AI components. Publish static cards without callback
buttons. Keep `full` dormant and separately gated.

**Why.** This directly solves the useful problem for about 30 people at the lowest cost
and sharply reduces collected data and failure modes.

**Revisit when.** The owner explicitly requests another feature and its exact data flow,
legal basis, deletion behavior, provider terms, and staging tests are ready.

## D-011 — Run the first release as a stateless GitHub Actions job

**Problem.** The owner does not want a paid VPS or a computer that must remain switched on.

**Options.** Continuous local process; VPS/systemd service; scheduled GitHub Actions job.

**Decision.** A workflow runs daily at 17:30 UTC (20:30 Moscow), invokes the dedicated
`publish-tomorrow` command, sends one card, and exits. A separate push/PR workflow runs
Ruff, mypy, and pytest. Four repository secrets provide routing and the bot token.

**Why.** There is no hosting bill or machine to administer, and the selected function is
stateless enough for CI. The command never starts the database, polling runtime, archive,
attendance, materials, or AI.

**Tradeoff.** Manual reruns can duplicate a card, scheduled jobs may start several minutes
late, and GitHub may disable schedules after prolonged repository inactivity. If schedule
change detection or strict delivery timing becomes necessary, revisit persistent hosting.

## D-012 — Split source access and Telegram delivery across two CI providers

**Problem.** The real GitHub-hosted run cannot connect to `rasp.spbgasu.ru`, while a
Russian runner cannot be assumed to reach Telegram reliably. One provider cannot cover
both network edges without a VPS or an always-on computer.

**Decision.** GitVerse runs the public schedule fetch at 17:30 UTC, removes teacher names,
renders the final card, and calls one GitHub receiver workflow. GitHub validates the
expected group, Base64 payload shape, source link, and Telegram size limit before sending.
The Telegram token exists only in GitHub. GitVerse receives a fine-grained GitHub token
limited to the one repository and Actions write permission; it cannot read the bot token.

**Why.** The bridge keeps the deployment stateless and avoids paid infrastructure while
placing each network call on the CI provider that can reach it.

**Tradeoff.** Two provider accounts and one narrowly scoped bridge token are required.
Delivery depends on both CI services, and a manual rerun can still create a duplicate.

## D-013 — Rich nightly digest plus four quiet comparisons

**Problem.** A daily plain-text card works, but does not provide a useful week view and
cannot distinguish a genuine edit from the next week merely entering the source horizon.

**Decision.** GitVerse fetches a dated two-week schedule at 06:50, 12:20, 17:20 and
20:30 Europe/Moscow. GitHub keeps the last validated envelope in a tiny Actions cache.
Only dates present in both the old and new horizons are semantically compared. Dates
newly appearing beyond the previous horizon are publication of the next week, not a
change. Expired past dates are ignored.

At the evening run the bot sends one Bot API 10.3 Rich Message: tomorrow is immediately
visible as a compact table, while the following seven days live in a native expandable
details block. Actual changes are separate minimal diffs; today/tomorrow changes notify
normally and later changes are silent. Identical snapshots produce no message. A classic
HTML fallback preserves the essential tomorrow card if Rich Messages are rejected.

**Why.** Four checks cover overnight, midday, end-of-office-day and evening changes for
roughly 120 short source jobs per month. Users receive one predictable preparation card,
not four status messages. The overlap rule prevents a rolling two-week window from
manufacturing “added lessons” every Monday.

**Tradeoff.** GitHub cache is small operational state, not an audit database. If it is
evicted, the next run safely establishes a new baseline and therefore may miss one change;
it will not generate a false alarm. A future VPS/database release can preserve full
snapshot history without changing the user-facing rules.

GitVerse's workflow checks out the private GitHub `main` explicitly through the existing
fine-grained bridge token (Contents read-only). GitHub is therefore the single code source;
future fixes do not need to be copied through the GitVerse web editor file by file.

## D-014 — Parse the current Bitrix component, fail closed, and add calm reminders

**Supersedes.** The source contract in D-009 and the direct GitHub sender in D-011 are
historical. The old weekly JSON endpoint stopped returning usable lesson data.

**Evidence.** The current first-party page calls Bitrix `runComponentAction` for
`gasu:raspisanie.csv/getRasp`. A first request returns a refreshed public CSRF token and
the repeated POST returns HTML containing dated week/day/lesson blocks. A no-send GitVerse
probe parsed 42 lessons for the exact configured group and dates including 11.09.2026.

**Decision.** GitVerse uses the component POST, retries once with the returned CSRF token,
and converts the current HTML into a bounded dated envelope. Any schedule-bearing day,
slot, subject, parity, or nonempty date that no longer matches the known structure aborts
the run instead of publishing a partial snapshot. GitHub ignores out-of-order snapshots.

A forced digest is a corrective rebaseline: it sends exactly one requested card and does
not interpret an earlier empty/broken cache as dozens of newly added lessons. The obsolete
direct `publish-schedule.yml` workflow and every source button/link were removed.

GitHub also runs one reminder command at the finite set of bell-derived times. It sends the
first-class notice about two hours before class and, near the end of a current class, names
the next class, start time, location, and remaining wait. It sends nothing after the final
class. Persistent markers deduplicate normal retries; the full configured group name is
hidden while a real subgroup label remains visible.

**Reliability tradeoff.** The two state workflows use one queued concurrency group and a
versioned Actions cache. Cache remains best-effort rather than a database: eviction can
temporarily suppress reminders until the next schedule snapshot or allow a duplicate.
This is accepted for the zero-cost, approximately 30-person release. Strict delivery would
justify a durable external store or always-on host later.
