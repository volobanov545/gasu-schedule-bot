# Deployment guide

This runbook deploys SZS Hub as a single, non-root Python service with a local SQLite
database. The default `schedule_only` profile makes outbound requests only: it does not
poll Telegram updates and opens no inbound public port. Read [Operations](OPERATIONS.md),
[Privacy](PRIVACY.md), [Known limitations](KNOWN_LIMITATIONS.md),
and [Security](../SECURITY.md) before a production cutover.

## Release status and external gates

As of 2026-09-03, the selected release is `FEATURE_PROFILE=schedule_only`. Live startup
additionally requires the operator's explicit
`LIVE_PROCESSING_APPROVED=true` after [data/legal launch review](LEGAL_LAUNCH_RU.md).
Both environment examples default to false. The runtime checks this before opening
the database or constructing Telegram or schedule clients. It applies in local, staging,
and production; it is not a legal certification. Keep AI disabled. Old hosting
recommendations below are historical:
the current owner budget is at most 200 RUB/month and no provider is approved yet.

The repository is not evidence that production is ready. A release is eligible only
after all commands and staging scenarios in this guide pass on the exact artifact and
host. At the time this guide was written, these external gates remain open:

- a separate BotFather test bot and disposable forum group have not been supplied;
- the real chat, schedule-topic, and group-schedule identifiers have not been supplied;
- Chrome is not connected to Codex, so the exact browser Network request and current
  Telegram client behavior have not been captured;
- Windows Computer Use failed with an `EPERM` error while accessing the Codex app path;
- the SPbGASU application endpoint timed out from the research environment, so the
  selected VPS must prove direct reachability;
- the 4VPS candidate has not completed its 7–14-day burn-in, encrypted restore drill,
  or the final Codex Security scan.

Do not work around those gates with production credentials. Never place a bot token,
AI key, backup credential, Telegram export, or university session in Git, a ticket,
chat, screenshot, command-line argument, or shell history.

## Deployment topology

- Candidate: 4VPS RU, 2 vCPU, 2 GB RAM, 15 GB NVMe, after burn-in.
- Fallback: RUVDS START HIT SSD, 1 vCPU, 1 GB RAM, 20 GB; enable a small encrypted
  swap file and retain document-extraction concurrency at one.
- Process: one `szs-hub.service`; no Telegram update polling in `schedule_only`.
- State: `/var/lib/szs-hub/szs_hub.sqlite3` on local SSD, readable only by the
  dedicated state group.
- Configuration: `/etc/szs-hub/runtime.env`, mode `0600`; systemd reads it as root before
  starting the unprivileged service.
- Releases: immutable directories below `/opt/szs-hub/releases`, selected through
  `/opt/szs-hub/current`.
- Logs: systemd journal. User message bodies and secrets must not be logged.
- Backup: a separate `szshub-backup` account can read the database but cannot read bot
  or AI secrets; it encrypts an application-consistent copy and owns the off-host
  credential. The bot account cannot read that credential or delete remote backups.

The full cost rationale and stop thresholds are in [COST.md](../COST.md).

## Host requirements

Use a supported 64-bit Linux distribution with systemd, current security updates, and:

- Python 3.12;
- Python's linked SQLite with FTS5 and either SQLite 3.51.3 or newer, fixed branches
  3.50.7+/3.44.6+, or a vendor-documented backport of the WAL-reset fix;
- `age`, `rclone`, `flock`, `sha256sum`, `find`, and timezone data;
- outbound DNS and HTTPS to Telegram, the approved SPbGASU source, the configured AI
  provider when enabled, and the private backup endpoint;
- no public SSH password authentication; use provider console recovery and operator
  SSH keys with least privilege.

The application refuses WAL in staging/production when it cannot prove the runtime is
safe. Do not set `SQLITE_WAL_BACKPORT_CONFIRMED=true` merely to make `doctor` pass.
Use that override only after preserving the distributor advisory that proves the exact
package contains the fix. The upstream issue is the
[SQLite WAL-reset bug](https://sqlite.org/wal.html#the_wal_reset_bug).

Optional OCR or office-document converters are not a base requirement. Add them only
after the extraction sandbox and all resource limits pass on the target host.

## Build and artifact gate

Build on a clean Python 3.12 environment. The repository currently has bounded version
ranges but no reviewed hash-locked dependency set. Before production, create a
versioned wheelhouse, record every artifact SHA-256, scan it, and preserve that manifest
with the release. Do not install an unbounded dependency set directly from the network
on the production host.

A release directory should contain at least:

```text
/opt/szs-hub/releases/RELEASE_ID/
├── .venv/
├── alembic/
├── deploy/
├── docs/
├── alembic.ini
├── pyproject.toml
└── RELEASE-SHA256SUMS
```

Run the repository gates before transfer:

```sh
python -m pytest
python -m ruff check .
python -m mypy src
alembic upgrade head
alembic check
git diff --check
```

Use a disposable database for the two Alembic commands. Record the command output,
commit ID, Python version, `sqlite3.sqlite_version`, and artifact hashes in the release
record. The absence of a hosted CI workflow is intentional for now: no workflow should
be added until its third-party actions are pinned to reviewed immutable commit SHAs and
the dependency lock is in place.

## One-time host preparation

The examples assume an operator account with `sudo`. Replace `RELEASE_ID` with a simple
immutable identifier such as the commit ID; do not interpolate data from an untrusted
archive filename.

```sh
sudo groupadd --system szshub-state
sudo groupadd --system szshub-backup
sudo useradd --system --gid szshub-state --home /nonexistent --shell /usr/sbin/nologin szshub
sudo useradd --system --gid szshub-backup --groups szshub-state \
  --home /nonexistent --shell /usr/sbin/nologin szshub-backup
sudo install -d -o root -g root -m 0755 /opt/szs-hub/releases
sudo install -d -o root -g root -m 0755 /etc/szs-hub
sudo install -d -o szshub -g szshub-state -m 0750 /var/lib/szs-hub
sudo install -d -o szshub-backup -g szshub-backup -m 0755 /var/lib/szs-hub-backup
sudo install -d -o szshub-backup -g szshub-backup -m 0700 \
  /var/lib/szs-hub-backup/objects
```

Install the reviewed release into `/opt/szs-hub/releases/RELEASE_ID`, create its virtual
environment as root, install only from the verified wheelhouse, and make the result
read-only to the runtime account:

```sh
sudo python3.12 -m venv /opt/szs-hub/releases/RELEASE_ID/.venv
sudo /opt/szs-hub/releases/RELEASE_ID/.venv/bin/pip install \
  --no-index --find-links /opt/szs-hub/releases/RELEASE_ID/wheelhouse szs-hub
sudo chown -R root:root /opt/szs-hub/releases/RELEASE_ID
sudo chmod -R go-w /opt/szs-hub/releases/RELEASE_ID
```

Verify `RELEASE-SHA256SUMS` before installation. Never copy a populated `.env`, database,
Telegram export, backup identity, or `rclone.conf` into a release directory.

## Secret and runtime configuration

Copy [the runtime example](../deploy/runtime.env.example) without secret values, then
edit the installed copy through a private root session:

```sh
sudo install -o root -g root -m 0600 \
  /opt/szs-hub/releases/RELEASE_ID/deploy/runtime.env.example \
  /etc/szs-hub/runtime.env
sudoedit /etc/szs-hub/runtime.env
```

Requirements:

- staging uses its own bot, forum group, and fresh database;
- production uses `APP_ENV=production`, `FEATURE_PROFILE=schedule_only`, and
  `TELEGRAM_MODE=outbound_only`; this combination cannot call `getUpdates`;
- use the absolute four-slash SQLite URL shown in the example;
- configure only `TELEGRAM_BOT_TOKEN`, `TARGET_CHAT_ID`, `SCHEDULE_TOPIC_ID`, and
  `SPBGASU_GROUP_ID`; the two Telegram numbers identify the group and topic, not students;
- begin with `AI_KILL_SWITCH=true` and `MATERIAL_AUTO_DELETE_SOURCE=false`;
- keep `AI_MAX_RETRIES=0` until the selected provider supports a reviewed idempotency key;
- omit `SPBGASU_SESSION` for the public full-time endpoint; if a later source requires a
  session, store it only in this file and rotate it separately;
- the bot token is obtained or rotated by the owner in BotFather and transferred through
  the owner's approved secret channel, never through this repository.

After the read-only Telegram startup probe succeeds, the application writes a secret-free
`runtime.configuration` recovery manifest into SQLite. In `schedule_only` it preserves
only technical routing, group, schedule, health, and backup settings; it omits every
member/material/AI field. It never includes bot/API/session/storage credentials and cannot
replace the protected `runtime.env` or secret-reissuance procedure.

Generate an age identity on an offline recovery device. Put only its public recipient
in `/etc/szs-hub/backup.env`; keep the private identity off the VPS. Configure a private
Yandex Object Storage prefix using `rclone config` and a least-privilege key limited to
that bucket/prefix. Then install:

```sh
sudo install -o root -g szshub-backup -m 0640 \
  /opt/szs-hub/releases/RELEASE_ID/deploy/backup.env.example \
  /etc/szs-hub/backup.env
sudoedit /etc/szs-hub/backup.env
sudo install -o root -g szshub-backup -m 0640 /path/from/private/setup/rclone.conf \
  /etc/szs-hub/rclone.conf
sudo install -o root -g root -m 0755 \
  /opt/szs-hub/releases/RELEASE_ID/deploy/scripts/backup-encrypted.sh \
  /usr/local/libexec/szs-hub-backup
```

The backup unit does not load `runtime.env`; `backup.env` contains only the database
path and backup-specific values. Confirm the object bucket blocks public access, uses provider-side encryption in
addition to age encryption, has billing alerts, and grants no bucket-list/delete access
beyond what the rotation script needs. A separate read-only recovery credential is
preferred for restore drills.

## Install the service definitions

Review the units against the target systemd version first. Hardening directives must
not be silently removed; if a directive is unsupported, document the exact exception
in the release record and retain an equivalent boundary.

```sh
sudo systemd-analyze verify \
  /opt/szs-hub/releases/RELEASE_ID/deploy/systemd/*.service \
  /opt/szs-hub/releases/RELEASE_ID/deploy/systemd/*.timer
sudo install -o root -g root -m 0644 \
  /opt/szs-hub/releases/RELEASE_ID/deploy/systemd/*.service /etc/systemd/system/
sudo install -o root -g root -m 0644 \
  /opt/szs-hub/releases/RELEASE_ID/deploy/systemd/*.timer /etc/systemd/system/
sudo ln -sfn /opt/szs-hub/releases/RELEASE_ID /opt/szs-hub/current
sudo systemctl daemon-reload
```

The main service is deliberately non-root, has no Linux capabilities, can write only
under `/var/lib/szs-hub`, and allows only outbound IPv4/IPv6/Unix sockets. The health and
migration units cannot use the network. The backup unit can use outbound network but
cannot read bot/AI credentials, home directories, or modify the installed release.

## First staging start

Do not point staging at the real group. With the temporary bot and group configured:

```sh
sudo systemctl start szs-hub-doctor.service
sudo systemctl status --no-pager szs-hub-doctor.service
sudo systemctl stop szs-hub.service
sudo systemctl start szs-hub-migrate.service
sudo systemctl status --no-pager szs-hub-migrate.service
sudo systemctl start szs-hub.service
sudo systemctl start szs-hub-health.service
sudo systemctl status --no-pager szs-hub.service szs-hub-health.service
```

`doctor` is local-only. It must report FTS5 and a safe SQLite runtime. Immediately after
first start, `health` is expected to be non-OK until the runtime heartbeat and successful
Telegram startup probe exist, the first schedule fetch is confirmed, and the backup unit
has atomically published a fresh success marker. Run the backup oneshot, then require the
next health run to be exactly `ok`; `database_integrity` must be `ok` throughout. Because
the CLI exits nonzero for `degraded` as well as `fail`, an expected first-run degradation
still appears as a failed oneshot and must be cleared only after its cause is resolved.

Inspect only bounded logs:

```sh
sudo journalctl -u szs-hub.service --since '-15 minutes' --no-pager
sudo journalctl -u szs-hub-health.service -n 20 --no-pager
```

Stop immediately if a token, private message body, document text, or AI key appears in
logs. Follow the credential incident procedure in [Operations](OPERATIONS.md).

## Staging acceptance matrix

For the selected `schedule_only` release, only Startup, Topics, Schedule, Publication,
Changes, Data boundary, Backup, Operations, and Security apply. The remaining rows are
future gates only if the owner deliberately switches to `full`.

| Area | Required evidence |
|---|---|
| Startup | Clean stop/start, host reboot, and restart during an in-flight update do not duplicate a user-visible action. |
| Telegram access | Non-member assistant query fails closed; current member succeeds; bot cannot initiate a private chat before `/start`. |
| Topics | Known topic IDs route correctly; an unknown topic does not cause a destructive fallback. |
| Schedule | Exact group request succeeds from the VPS; empty/invalid responses and source timeout serve a visibly stale last-known snapshot. |
| Publication | Evening card occurs once in the configured Moscow-time window; restart around the boundary does not duplicate it. |
| Changes | Room/teacher/time/add/remove cases produce one semantic notification; formatting-only changes do not. |
| Data boundary | No Telegram poller, durable inbox, interactive buttons, attendance, materials, archive, or AI worker starts; a non-schedule outbox envelope is rejected. |
| Attendance | Add/remove of the configured standard reaction is idempotent, late reactions are ignored, and only the configured headman receives/edits the summary. |
| Materials | Low confidence remains untouched; medium confidence creates one silent owner/headman review; approved material is copied and verified; source deletion remains impossible in this release. |
| Albums | Interleaved/delayed groups do not mix; a late member creates a bounded follow-up that copies only the new message. |
| Archive/search | Telegram Desktop dry-run reports counts/failures; an approved import is idempotent; Russian aliases, dates, authors, and topic filters return source links. |
| Files | Oversize, malformed, compressed-bomb, path-traversal, timeout, and unsupported-format cases fail safely and leave no plaintext temporary file. |
| AI | Kill switch leaves core features working; non-member queries never reach retrieval/provider; quota and permanent-provider errors do not retry indefinitely. |
| Backup | Daily unit produces age ciphertext, remote object and checksum; offline identity decrypts a downloaded copy; `PRAGMA quick_check` and a disposable restore pass. |
| Operations | Health detects stale schedule/backup, missing runtime heartbeat, failed Telegram rights probe, failed work/outbox, and corrupt schema without leaking exception text. Every source unit maps to its fixed failure-alert code, duplicate alerts are bounded, and an independent external monitor sees host/Telegram-alert failure. |
| Security | Standard Codex Security scan is complete and all release-blocking findings are resolved or explicitly accepted by the owner. |

`MATERIAL_EXTRACTION_ENABLED` stays `false` during the general matrix. Enable it only in a
separately isolated, killable no-network parser worker and then execute the Files row with
the malicious-document corpus. The current in-process worker does not satisfy that gate.

Also capture current Telegram Desktop/Android/iOS rendering for the HTML compatibility
card. New Rich/Ephemeral features are not required for the release.

## Enable timers only after acceptance

Exercise each oneshot manually before enabling its timer:

```sh
sudo systemctl start szs-hub-backup.service
sudo systemctl status --no-pager szs-hub-backup.service
sudo systemctl enable --now szs-hub.service
sudo systemctl enable --now szs-hub-health.timer szs-hub-backup.timer
sudo systemctl list-timers 'szs-hub-*'
```

Do not enable publication schedules while staging is still connected to any production
topic. `systemctl enable` is not a substitute for Telegram-side acceptance.

## Production cutover

Production requires explicit owner approval at the action time. Before cutover:

1. freeze the approved artifact and configuration diff;
2. create and verify an encrypted off-host backup;
3. record the target group/topic IDs, permissions, bot identity, and publication time
   without recording secret values;
4. confirm monthly projected cost remains at or below 200 RUB and AI stays disabled;
5. confirm source deletion is false and no topic rename/delete is planned;
6. apply migrations while the service is stopped;
7. start the service, verify health, then perform one non-destructive owner command;
8. monitor logs, Telegram delivery, queue health, disk, and spend closely for 24 hours.

Historical import is a separate approval-gated, offline operation. Keep `result.json` and
its media in one disposable private directory outside the release tree. First run
`szs-hub import-plan`, record its exact `source_fingerprint`, date range, failures, and
missing-media list, and obtain owner approval. Stop the runtime and invoke
`szs-hub import-apply` with that fingerprint, a new backup path outside the export tree,
and `--confirmed-offline`; add either acceptance flag only for the exact reviewed
exceptions. The command refuses a running runtime, creates and verifies the backup before
writing, resumes by fingerprint, and returns nonzero unless message/file/search/reply
reconciliation succeeds. Preserve the export until managed historical media and content
extraction are separately complete.

## Upgrade and rollback

For every upgrade:

1. keep the previous release directory intact;
2. stop the bot and run a fresh encrypted backup;
3. run `doctor` from the new artifact;
4. review migration upgrade and downgrade behavior;
5. point `current` at the new release, apply migrations while stopped, then start;
6. verify health and the bounded acceptance smoke set.

If code fails but the migration is backward compatible, stop the service, repoint
`current` to the prior release, reload systemd, and start. Never run a guessed Alembic
downgrade.

If the schema change is not backward compatible, stop all SZS Hub units and restore the
pre-upgrade encrypted backup using the verified procedure in [Operations](OPERATIONS.md).
The restore command requires `--confirmed-offline` and creates another local safety copy
of the database it replaces. It also requires the shared runtime/restore advisory lock;
the flag cannot override a live process. Old WAL/SHM files are moved to timestamped safety
sidecars. Preserve failed-release logs, sidecars, and the pre-restore safety copy until
the incident is closed.
