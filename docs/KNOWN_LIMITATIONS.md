# Known limitations and release blockers

This is the honest boundary of SZS Hub as of 2026-09-02. A passing unit-test suite does
not remove a limitation that requires Telegram, SPbGASU, provider, browser, host, or human
evidence. Resolve or explicitly accept every release blocker before production.

## Hard release blockers

- **The schedule-only scope is implemented but not externally proven (2026-09-03).**
  It does not request Telegram updates and enables only schedule jobs, a technical
  heartbeat, and static schedule-topic sends. Unit tests verify those boundaries.
  The exact public source request and Telegram publication still need a disposable
  staging test from the selected VPS. The dormant full profile remains legally and
  operationally blocked; see [Russian launch review](LEGAL_LAUNCH_RU.md).

- **Runnable locally, not yet proven in staging.** `src/szs_hub/app.py` composes the
  long-poller, durable inbox, independent core/material workers, outbox sender, schedule
  jobs, archive, attendance, assistant, startup/periodic Telegram probes, process lock,
  structured logging, and graceful shutdown. This becomes a production bot only after
  the real Telegram forum/VPS staging matrix and restart/burn-in evidence pass.
- **No Telegram staging credentials or disposable forum group.** The bot token, real
  chat/topic/headman IDs, permissions, update allowlist, reaction delivery, private
  `/start`, copy/file behavior, and restart semantics remain unverified end to end.
- **Chrome unavailable.** The requested Chrome connection is not active. Exact Network
  capture for the current SPbGASU page and current Telegram client rendering has not been
  completed. Chrome-specific work must not be silently substituted with another browser.
- **Computer Use unavailable.** Windows application discovery failed twice with `EPERM`
  on the Codex application path. No permission or setting was changed to bypass it.
- **SPbGASU reachability not proven from the target host.** DNS/TCP succeeded during
  research, but application HTTP timed out through that environment. The full-time public
  endpoint parser has fixtures; the candidate VPS still needs a real bounded request.
- **Hosting and recovery not proven.** The 4VPS candidate is not purchased/burned in, the
  RUVDS fallback is not exercised, and no encrypted object backup has completed a full
  download/decrypt/restore drill.
- **The pre-fix Standard security scan is complete.** Its practical authorization,
  deployment-secret, quota, transport, response-bound, and failed-payload findings were
  patched and regression-tested. Rare remote-acceptance replay windows, scoped preservation
  holds, and storage-side backup immutability remain explicitly accepted for this small
  private deployment until staging evidence or project scope justifies the extra machinery.
- **Retention is partial, not a complete data-subject workflow.** Daily bounded jobs now
  redact 30-day processed-update payloads and 90-day failed diagnostics and delete
  30-day AI conversations. Attendance/schedule expiry, scoped security holds, verified
  member export/erasure, and encrypted-backup expiry still need implementation/evidence.

## Telegram platform limits

- A normal bot cannot enumerate all existing forum topics or read arbitrary historical
  group history. Topic IDs require explicit bootstrap from real messages/links; history
  requires a reviewed Telegram Desktop JSON export.
- Bot API pending updates are retained for a limited period (documented as no longer than
  24 hours). An outage beyond that can make reaction attendance permanently incomplete.
- Telegram provides per-user `message_reaction` updates only when the bot is an admin and
  the update type is explicitly enabled. There is no endpoint that returns the full
  current per-user reaction ledger after missed updates.
- Anonymous reaction-count updates cannot establish which student reacted. Reactions by
  an anonymous admin/chat actor and unsupported/custom reactions are ignored.
- A bot cannot initiate a private chat. Each user must open/start it first; deep links do
  not bypass that rule.
- The standard cloud Bot API `getFile` download path is bounded (currently 20 MB in the
  researched contract). Large files require a clearly designed alternative or remain
  indexed by Telegram metadata only.
- Telegram sends/copies and deletes are not atomic. Copies can succeed when later deletion
  is impossible. Source deletion is therefore disabled and no undo is promised.
- Albums arrive as individual updates sharing `media_group_id`; Telegram sends no explicit
  “album complete” event. Any quiet-period finalizer needs delayed/interleaved staging.
- Existing message deletion/edit history cannot be perfectly reconstructed after the bot
  missed an event. Imported history is a snapshot, not a complete audit log.
- New Rich/Ephemeral Bot API features were not accepted across the group's real clients.
  Essential UX remains HTML/entities; ephemeral delivery is never used for critical facts.

## Schedule-source limits

- The public full-time route uses an observed endpoint with a misspelled `SERACH` parameter
  and a response wrapper that has changed before. The parser supports observed root and
  `R` wrapper shapes, not arbitrary future schemas.
- A group-not-found result can be an empty list and may be indistinguishable from certain
  upstream data errors without page/source corroboration. It must not publish an empty day
  as a newly confirmed cancellation.
- The parameterless endpoint returns a large aggregate response and is intentionally
  impossible through the client API. The group identifier is required.
- Week-number/parity is derived from currently observed first-party page data/rules and
  needs a date-boundary staging check against the official page.
- Bell slots are based on the current official PDF. A university change requires a
  versioned update and semantic review.
- The IBFO/part-time Bitrix POST flow is not implemented. Exact form/session serialization
  is deferred until an approved Chrome Network capture; do not guess it.
- There is no service-level guarantee from the public university source. Last-known data
  can be served with an age/stale label, but freshness cannot be manufactured during an
  outage.

## Archive, search, and files

- Telegram Desktop read-only planning and an offline apply path are implemented locally.
  Apply requires the exact reviewed SHA-256 fingerprint, a new verified backup, an
  exclusive runtime lock, explicit acceptance of parser/missing-media exceptions, durable
  resume state, a minimal audit row, and full message/file/search/reply reconciliation.
  It has not yet been exercised on the owner's real export. Export media are represented
  by metadata/relative references; managed media preservation and historical content
  extraction are not implemented, so the export must not be discarded after a metadata-
  only import until that scope is explicitly accepted.
- Live TXT/PDF/DOCX/XLSX/PPTX download, bounded extraction, hash persistence, reindexing,
  and reassessment are implemented and tested locally, but are opt-in and disabled by
  default. The in-process parser is not an OS sandbox, so enabling it in production is
  blocked on deployed-host isolation and a malicious-document corpus.
- FTS5/BM25 with Russian Snowball stems and configured aliases is lexical search. It can
  miss paraphrases, images without OCR, handwritten notes, uncommon abbreviations, and
  meaning spread across distant messages.
- Embeddings are intentionally not active. They should be added only when a representative
  evaluation set proves repeated semantic misses and privacy/cost/storage are approved.
- Extracted text is untrusted and may be incomplete or incorrectly ordered. Scanned PDFs
  are only marked `needs_ocr`; photo/scan OCR is not implemented. Future OCR output cannot
  be used as authoritative instructions, grades, deadlines, or access decisions without
  the linked source.
- Parser resource limits reject or truncate some legitimate large/dense documents. This
  is preferable to decompression, XML, pixel, or CPU exhaustion, but requires an explicit
  “unsupported/too large” user path.
- Installing Python parser libraries does not by itself create an OS sandbox. The current
  parser uses one async worker and cooperative limits but remains inside the networked bot
  process. Production parsing needs a separately isolated, killable worker with no network,
  tight filesystem/process limits, and malicious-corpus tests on the deployed host.
- Local temporary files are removed by design, but deletion on SSD/cloud media is not a
  cryptographic erase guarantee. Minimize plaintext creation and rely on encryption and
  retention boundaries.

## Attendance limits

- A reaction means only that the Telegram account selected the configured emoji during
  the window. It is voluntary, can be removed, is not geolocation, and is not official
  proof of physical attendance or identity.
- The roster is only as current as membership updates/checks. Privacy settings, Telegram
  outages, admin-right changes, or missed updates can make the missing list incomplete.
- Changing the configured reaction does not rewrite open/historical sessions; each session
  stores its own reaction. Change it outside an attendance window.
- Anonymous admins/chat actors, bot accounts, late updates, unsupported reactions, and
  messages that do not map to an open session are ignored.

## AI limits

- No provider, model, key, price, retention policy, or quality level is guaranteed by the
  repository. Provider IDs and tariffs are mutable configuration.
- Retrieved sources and model output can be wrong, stale, malicious, or incomplete. The
  assistant must cite group sources and never turn model text into an admin command.
- Per-user daily and global daily/monthly request/token quotas are durable and conservative
  when provider usage is missing or partial. Billing remains authoritative; the 350/480 RUB
  controls still require an external provider-balance or invoice check.
- A provider outage, auth error, balance error, or kill switch disables AI but must not
  disable schedule, attendance, archive ingestion, or local search.
- There is no promise that a third-party provider will process data in Russia or avoid
  training/retention. The owner must approve current terms before enabling it.

## Reliability and infrastructure limits

- One process and one SQLite writer are intentional. There is no high availability,
  leader election, online cross-region failover, managed point-in-time recovery, or
  zero-downtime schema migration.
- SQLite must live on local storage, not NFS/object-backed/multi-host storage. Multiple
  writer processes are unsupported.
- SQLite WAL is blocked below safe versions unless a specific vendor backport is proven.
  The local development runtime is safe; the future VPS runtime is unknown until `doctor`.
- Health is a local CLI/timer, not a public endpoint. Fixed-code systemd failure units can
  notify a separately configured owner through Telegram with a durable cooldown, but that
  path is not independent of Telegram, the bot token, or the host. A VPS/provider-side
  reachability/service monitor is still a production release gate.
- A 2 GB/15 GB VPS has little headroom for OCR, dependency builds, journal growth, or large
  archives. The 1 GB fallback is more constrained; swap prevents some crashes but does not
  add CPU or remove latency.
- Prices, provider quotas, and network routes can change. The published 4VPS/RUVDS/Yandex
  values are dated estimates, not a contract or automatic invoice control.
- The backup script trusts the host long enough to create a consistent copy and encrypt it.
  If the live host and object write credential are both compromised, an attacker may
  delete retained objects unless storage immutability/versioning is separately configured.

## Build and operations gaps

- Dependencies use version ranges and are not hash locked. A versioned verified wheelhouse
  is a production release requirement.
- No hosted CI workflow is included. Adding one before action versions are pinned to
  reviewed immutable full commit SHAs would add supply-chain ambiguity.
- Docker is not assumed and was unavailable in the development environment. The supported
  deployment shape is a versioned Python artifact under systemd.
- Git has no configured author identity in the development environment, so no identity or
  signed release is invented. A real release should be committed/tagged by the owner under
  their established identity.
- The systemd units are hardened templates. `systemd-analyze verify` and a full staging run
  on the actual distribution are required; unsupported directives must be documented, not
  silently removed.
- Documentation describes manual change/restore procedures because direct database repair
  commands would be unsafe. Failed durable rows remain failed until a reviewed repair path
  exists.
