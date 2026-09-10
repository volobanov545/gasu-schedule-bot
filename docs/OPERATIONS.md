# Operations runbook

This runbook is for the owner or designated operator of SZS Hub. It favors a safe,
observable stop over guessed repairs. The production service is a single systemd unit.
The selected outbound-only schedule profile has no public health URL, webhook, or
Telegram polling connection to maintain.

## Service map

| Unit | Purpose | Expected state |
|---|---|---|
| `szs-hub.service` | Schedule fetch, durable publication, and health heartbeat | active continuously |
| `szs-hub-doctor.service` | Offline configuration/runtime validation | inactive after a successful oneshot |
| `szs-hub-migrate.service` | Alembic schema upgrade; conflicts with the bot | inactive except during a release |
| `szs-hub-health.timer` | Durable-state check every five minutes | active/waiting |
| `szs-hub-health.service` | One health-check execution | inactive after success |
| `szs-hub-backup.timer` | Daily encrypted off-host backup | active/waiting |
| `szs-hub-backup.service` | Consistent copy, encryption, upload, rotation | inactive after success |
| `szs-hub-alert@.service` | Fixed-code private owner notification after a source unit fails | inactive after delivery |

Useful read-only commands:

```sh
sudo systemctl status --no-pager szs-hub.service
sudo systemctl list-timers 'szs-hub-*'
sudo systemctl --failed
sudo journalctl -u szs-hub.service --since today --no-pager
sudo journalctl -u szs-hub-health.service -n 20 --no-pager
sudo journalctl -u szs-hub-backup.service -n 20 --no-pager
```

Keep log windows bounded. Do not paste raw journal output into an issue before checking
it for Telegram text, identifiers, filenames, provider responses, and secrets.

## Health interpretation

Run a check on demand:

```sh
sudo systemctl start szs-hub-health.service
sudo systemctl status --no-pager szs-hub-health.service
```

The CLI emits JSON with no user content. Interpret it as follows:

- `state=ok`: database quick check, configured schedule freshness, runtime heartbeat,
  last Telegram rights probe, backup-success marker, and durable queues are all healthy;
- `state=degraded`: the database is readable, but the schedule/backup is stale or durable
  work has failed; investigate before relying on the next publication;
- `state=fail`: the database is unavailable/corrupt, the runtime heartbeat or Telegram
  probe is missing/stale, or the last rights probe failed; stop unsafe automation and
  investigate the relevant component;
- `last_schedule_fetch`: timestamp of the latest stored snapshot, not proof that the
  university source is currently online;
- `last_runtime_heartbeat`: the last minute-maintenance heartbeat written by the running
  application;
- `last_telegram_probe` and `telegram_probe_ok`: the last result saved by the runtime's
  own read-only admin/forum/headman check, not a network request made by the health unit;
- `last_backup_success`: the atomically published marker written only after upload and
  rotation finish;
- `recent_failed_updates`: terminal update failures during the last 24 hours;
- `failed_jobs` and `failed_outbox`: durable work requiring review, not permission to
  edit database rows by hand.

The CLI exits nonzero for both `degraded` and `fail`, so systemd treats either as an
unsuccessful health run. A Telegram failure notification is useful but not independent:
an out-of-band host/service monitor is still a production gate because Telegram or the
bot credential may be the failing dependency.

Main, health, backup, and migrate failures map to the fixed alert codes `main`, `health`,
`backup`, and `migrate`; arbitrary unit/log text is never included. Successful alerts are
deduplicated per code (30 minutes for main/migrate, six hours for health/backup), while a
delivery failure remains retryable. `ALERT_USER_ID` must identify the infrastructure
owner and that account must have sent `/start` to the bot. The alert template has no
`OnFailure` of its own, so it cannot recurse.

## Normal cadence

### Daily

- confirm the bot, health timer, and backup timer are active;
- confirm the newest health result is `ok`, or understand every degraded reason;
- confirm the previous backup service succeeded and an encrypted daily object exists;
- check free disk space and inode use; keep at least 20% free on the 15 GB candidate;
- inspect Telegram for a stale-source label, duplicate card, or failed material copy;
- when AI is enabled, check quota usage and projected monthly spend.

### Weekly

- review failed updates/jobs/outbox without manually marking them successful;
- confirm a weekly backup object and checksum exist;
- install host security updates in a planned window and test a clean reboot;
- sample one schedule result against the official page without exposing a session;
- review membership/access failures and unexpected admin actions;
- verify journal use is bounded. On a 15 GB host, a 250 MB journal ceiling and 14-day
  maximum retention are reasonable starting points, but journald limits are host-wide
  and must be approved with the server owner.

### Monthly

- reconcile the actual 4VPS/RUVDS, object-storage, traffic, and AI invoice against
  [COST.md](../COST.md);
- stop AI before projected total spend reaches 480 RUB; investigate at 350 RUB;
- confirm the 4VPS published renewal price still keeps the total below 500 RUB;
- verify 7 daily, 4 weekly, and 3 monthly encrypted generations are represented;
- review users with operator or backup credentials and remove obsolete access;
- review retention/purge status under [Privacy](PRIVACY.md);
- verify the daily `maintenance.retention` job completes and inspect only aggregate counts;
- apply a tested release with OS and application security fixes.

### Quarterly and before a risky release

- download a backup using a read-only recovery credential;
- verify the ciphertext checksum, decrypt with the offline age identity on an isolated
  device, restore to a disposable database, run integrity/health checks, and record the
  recovery time and result;
- rehearse code rollback and full stop;
- re-run the release security scan and staging smoke matrix.

### Historical import

Historical import is never a live maintenance action. Review the read-only `import-plan`
JSON first, stop the service, use the exact reported fingerprint, supply a new verified
backup destination outside the disposable export directory, and pass
`--confirmed-offline`. The exclusive database lock is authoritative: the confirmation
flag cannot override a running bot. Parser failures and missing media each require their
own explicit acceptance flag. A successful command records a minimal audit event and
verifies every planned message, expected file row, search document, and in-export reply
link. Retain the private export after a metadata-only import because historical media
content is not yet copied into managed storage.

## Backups

The application backup command uses the SQLite Online Backup API and runs `quick_check`
before publishing the local copy. The systemd backup service then:

1. writes that plaintext copy only below its private `/run` directory;
2. encrypts it to an age recipient whose private identity is offline;
3. computes a SHA-256 sidecar;
4. uploads immutable ciphertext and checksum objects;
5. keeps the newest 7 daily, 4 weekly, and 3 monthly remote generations;
6. keeps seven local ciphertexts and removes the plaintext on every exit path;
7. atomically updates `/var/lib/szs-hub-backup/last-success` only after every preceding
   step succeeds; health checks the age of that marker.

The script refuses an unexpected local path, an empty/broad remote target, or a second
concurrent backup. The object-storage key must be scoped to the configured private
prefix. `rclone --immutable` prevents overwriting the named object during this upload; it
does not provide provider object lock or make the credential unable to delete other
generations. `rclone --checksum` and the sidecar protect transfer/restore handling, but
only a download, decrypt, SQLite integrity check, and application smoke test prove
recovery.

Run and inspect one backup manually:

```sh
sudo systemctl start szs-hub-backup.service
sudo systemctl status --no-pager szs-hub-backup.service
sudo journalctl -u szs-hub-backup.service -n 30 --no-pager
```

If it fails, do not delete the previous remote generation. Check, in order: local disk,
SQLite health, age recipient syntax, object credentials/clock, outbound network, bucket
quota, and cost guard. Never enable public bucket access to diagnose an upload.

## Restore drill

Perform routine drills on an isolated host, not over the live database:

1. download one `.age` object and its `.sha256` through a read-only credential;
2. compare the ciphertext SHA-256 with the sidecar;
3. attach the offline identity only for the decryption step;
4. decrypt to an encrypted or ephemeral local volume with mode `0600`;
5. restore into a new disposable path using a local environment;
6. use the restore command's `quick_check` result, `doctor`, and `health --offline`, then
   verify representative message/search/schedule counts without starting publication;
7. if testing full runtime recovery, attach only staging credentials, start the runtime,
   wait for its heartbeat/startup Telegram probe, and only then require `health=ok`;
8. securely dispose of the drill plaintext and disconnect the identity.

Example after independently verifying the checksum:

```sh
age --decrypt --identity /media/offline/age-identity.txt \
  --output /secure-recovery/szs-hub-recovery.sqlite3 \
  /secure-recovery/szs-hub-backup.sqlite3.age
APP_ENV=local \
DATABASE_URL=sqlite+aiosqlite:////secure-recovery/restore-test.sqlite3 \
  /opt/szs-hub/current/.venv/bin/szs-hub restore \
  --input /secure-recovery/szs-hub-recovery.sqlite3 --confirmed-offline
APP_ENV=local \
DATABASE_URL=sqlite+aiosqlite:////secure-recovery/restore-test.sqlite3 \
  /opt/szs-hub/current/.venv/bin/szs-hub health --offline
```

Offline health deliberately skips schedule freshness, runtime heartbeat, Telegram probe,
and backup-marker age; it checks database integrity and durable failures only and is not
evidence that the restored service is ready to publish. The age identity path is an
example, not a location to keep the key permanently.

## Live disaster restore

Use this only after declaring an incident and identifying a trusted backup generation.

1. stop the bot, both timers, and any running health/backup/migrate oneshot;
2. record service status, release ID, timestamps, and bounded logs;
3. download and verify ciphertext before decrypting;
4. place the decrypted database in `/var/lib/szs-hub/recovery` owned by `szshub`, mode
   `0600`; do not put it in `/tmp` or the release tree;
5. invoke restore with the service offline;
6. preserve the automatic `pre-restore` safety copy;
7. run migrations only if the restored schema and chosen code release require them;
8. run `doctor`, start with publication controls still disabled, wait for a heartbeat and
   successful startup probe, then require `health=ok` before smoke checks.

```sh
sudo systemctl stop szs-hub-health.timer szs-hub-backup.timer szs-hub.service \
  szs-hub-health.service szs-hub-backup.service szs-hub-migrate.service
sudo install -d -o szshub -g szshub-state -m 0700 /var/lib/szs-hub/recovery
sudo chown szshub:szshub-state /var/lib/szs-hub/recovery/recovered.sqlite3
sudo chmod 0600 /var/lib/szs-hub/recovery/recovered.sqlite3
sudo -u szshub env -i \
  PATH=/opt/szs-hub/current/.venv/bin:/usr/bin:/bin \
  APP_ENV=local \
  DATABASE_URL=sqlite+aiosqlite:////var/lib/szs-hub/szs_hub.sqlite3 \
  /opt/szs-hub/current/.venv/bin/szs-hub restore \
  --input /var/lib/szs-hub/recovery/recovered.sqlite3 --confirmed-offline
sudo systemctl start szs-hub-doctor.service szs-hub.service
sudo systemctl start szs-hub-health.service
```

`run` and `restore` acquire the same OS advisory lock; the restore refuses a live runtime
even when `--confirmed-offline` was supplied. A stale lock file after a crash is harmless
and does not need deletion. Before replacing the database, restore creates a verified
`pre-restore` safety backup and moves old `-wal`/`-shm` files to timestamped safety
sidecars. Do not restart while `database_integrity` is not `ok`, and never copy a live
SQLite/WAL pair as a substitute for this path.

## Safe configuration changes

For every change: save a redacted change record, create a backup, edit with `sudoedit`,
run the doctor unit, restart once, run health, and perform the narrow smoke test. If the
doctor fails, restore the previous environment file without starting the bot.

After a successful startup probe, the runtime stores a secret-free recovery manifest in
the database under `runtime.configuration`. It records routing/group IDs, attendance and
schedule settings, AI provider/model flags, and material thresholds, so it travels with a
database backup. It deliberately excludes tokens, API/session/storage credentials and is
not a copy of `runtime.env`; recover or rotate those secrets from a separate protected
source after a disaster.

### Change the headman

1. Obtain the exact numeric Telegram user ID through the approved staging/owner flow.
2. Confirm that account is a current member and the intended recipient.
3. Change `HEADMAN_USER_ID`; update `ADMIN_USER_IDS` only if operator rights are also
   explicitly approved.
4. Restart and exercise a staging attendance summary.

Changing the headman affects routing/configuration; it does not rewrite historical
attendance records. Never reuse a username as identity.

### Change the attendance reaction

Change `ATTENDANCE_REACTION` to one standard emoji, restart, and verify add/remove in
staging. Existing attendance sessions persist the reaction with which they were opened;
the new setting applies to subsequently opened sessions. Avoid changing it during an
open window.

### Change attendance duration or evening time

Set `ATTENDANCE_WINDOW_MINUTES` within 5–120. Set `EVENING_SCHEDULE_TIME` as `HH:MM`; the
application timezone is `Europe/Moscow`. Make the change outside the publication window,
restart, and test the next scheduled job. A configuration restart must not be treated as
proof that a previously queued job was rescheduled; inspect the resulting behavior in
staging.

### Change topics or group

Treat `TARGET_CHAT_ID` and topic IDs as production routing controls. Validate every new
ID with a non-destructive staging send. Back up before changing them. Never delete,
rename, close, or reorder production topics as part of a configuration edit.

### Enable or disable AI

AI is off when `AI_KILL_SWITCH=true`, regardless of other provider values. To enable it,
approve provider privacy/terms, set a private key, model and base URL, retain daily/global
limits, and pass member-gate/quota/injection/error tests. To stop it immediately, set the
kill switch true and restart. Schedule, attendance, archive, and local search must remain
available; verify that invariant after the restart.

### Material source deletion

The current runtime has no source-delete transition and never deletes the original, even
if `MATERIAL_AUTO_DELETE_SOURCE=true` is accidentally supplied; the recovery manifest
records the effective value as false. Keep the setting false. Adding deletion requires a
separate reviewed release, staging false-positive evidence, explicit owner approval, and
a recovery limitation notice because Telegram deletion is not an archive/undo mechanism.

## Restart, pause, and full stop

A normal restart is:

```sh
sudo systemctl restart szs-hub.service
sudo systemctl start szs-hub-health.service
```

A reversible pause stops publications and update processing but preserves configuration,
credentials, database, and timers unless explicitly stopped:

```sh
sudo systemctl stop szs-hub.service
```

For a full operational stop:

```sh
sudo systemctl disable --now \
  szs-hub.service szs-hub-health.timer szs-hub-backup.timer
sudo systemctl reset-failed 'szs-hub-*'
```

Then confirm no SZS Hub process is running. Preserve the database and newest verified
encrypted backup by default. If compromise is suspected, separately revoke/rotate the
BotFather token, AI key, object-storage key, university session, and operator SSH keys.
Removing the bot from the group is a separate owner decision; do not delete topics,
messages, or the database during containment.

## Incident response

### First five actions

1. Protect people and data: stop the affected service or AI subsystem.
2. Preserve evidence: record UTC/Moscow time, release ID, symptom, systemd state, and
   bounded redacted logs; do not modify database rows.
3. Limit access: revoke only credentials within the suspected scope.
4. Establish integrity: run offline `doctor`, health, and backup verification as safe.
5. Recover through a tested release/backup, then document root cause and prevention.

| Incident | Immediate containment | Recovery gate |
|---|---|---|
| Bot token exposed | Stop bot; revoke/regenerate in BotFather; replace env file without logging the token. | Doctor passes, bot identity is rechecked, old token is proven invalid, staging message succeeds. |
| AI key or runaway cost | Set `AI_KILL_SWITCH=true`, restart, revoke key, inspect usage/billing. | New scoped key, quotas and provider errors tested; projected total remains below cap. |
| Backup key exposed | Disable backup timer; revoke object key; preserve existing ciphertext and audit access. | New least-privilege object key; if the offline age identity leaked, create a new recipient and re-encrypt future backups. |
| Database integrity failure | Stop bot/timers; preserve files and disk evidence; do not checkpoint or vacuum. | Trusted backup restores on isolation, integrity passes, suspected disk/runtime cause is addressed. |
| Suspected malicious document | Stop extraction worker/bot if coupled; retain metadata, not unsafe rendered output; isolate temp area. | Parser limits and exploit path are fixed/scanned; staged malicious corpus passes. |
| SPbGASU outage/schema drift | Keep last confirmed snapshot visibly stale; disable change publication if semantics are uncertain. | Exact group request and parser fixtures pass; owner verifies card against official page. |
| Telegram outage/missed reactions | Do not infer attendance or replay guessed reactions; preserve inbox and timestamps. | Polling recovers, owner is told which windows are incomplete; a new session is used if needed. |
| Wrong topic/publication | Stop bot; do not bulk-delete; record destination/message IDs and notify owner. | Routing IDs are revalidated in staging and cleanup is separately approved. |
| Member data disclosed | Stop the disclosure path, revoke affected links/keys, preserve evidence, notify owner. | Access gate fixed and verified; affected people and response obligations are assessed. |
| Host compromise | Stop at provider edge if safe, revoke all host-readable credentials, snapshot for investigation. | Rebuild a clean host from verified artifacts; restore data only after integrity review. |

## Queue and delivery recovery

Durable inbox, jobs, and outbox are designed for retry and idempotency. That does not
authorize direct SQL edits. On a failed item:

- identify whether the cause is permanent configuration/input or transient transport;
- fix the cause before restarting; permanent authentication/validation errors must not
  be retried in a loop;
- preserve the idempotency key and recorded Telegram message ID;
- use a reviewed application/admin repair command when one exists;
- if no repair command exists, leave the row failed and open an implementation task.

Delivery is at-least-once, not exactly-once. A process or network failure after Telegram
accepted a send/copy but before the local confirmation commit can produce a duplicate on
retry. Keep user-visible publications idempotent where Telegram exposes a destination ID,
and include this residual case in restart staging rather than promising impossibility.

Telegram keeps pending updates for no more than roughly 24 hours. After a longer outage,
state explicitly that reaction attendance may be incomplete; it cannot be reconstructed
from a current per-user reaction-list endpoint.

## Cost and capacity guard

The expected baseline is approximately 209.50 RUB/month (4VPS plus about 5 GB of Yandex
Object Storage), with AI expected off/free. Prices are mutable. Guardrails:

- warning at 350 RUB projected total;
- force AI off at 480 RUB projected total;
- never exceed 500 RUB by silently adding a managed database, public IPv4 service,
  vector database, commercial OCR, or unbounded storage;
- object-storage alert at 8 GB and investigation before 10 GB;
- one extraction/OCR worker, bounded file/page/pixel/cell/time limits;
- migrate to RUVDS or an existing home device only through the backup/restore drill,
  never by copying a live SQLite/WAL pair.

Provider invoices and resource graphs are authoritative. Application token accounting is
a guardrail, not billing proof.
