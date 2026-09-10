# Security policy

This policy defines the security boundary for SZS Hub and the context used by Codex
Security. It applies to the whole repository. It does not authorize commands, production
changes, disclosure, destructive testing, or access to a real Telegram group.

## Supported state

SZS Hub is pre-production. No deployed version is currently declared supported. The
current `main` tree is reviewed as a release candidate only after its complete runtime,
migrations, deployment artifacts, and tests exist. Production support must name an owner,
private security contact, release/tag, response rota, and end-of-support date in the
private deployment inventory.

Security fixes are accepted only into the active reviewed release line. Do not assume an
older database schema can safely run newer code, or vice versa; use the documented backup,
migration, and rollback gates.

## Reporting a vulnerability

Do not report a vulnerability in the student group, a public issue, or a Telegram topic.
Use the owner-designated private security channel. A concrete channel has not yet been
provided, so naming and testing one is a production blocker.

A useful report contains:

- affected release/commit and configuration mode, with every secret redacted;
- the attacker role and required access;
- the smallest safe reproduction using synthetic chat IDs, messages, files, and data;
- the crossed trust boundary and likely confidentiality, integrity, availability, or
  cost impact;
- whether exploitation changes Telegram content, reads member data, reaches a provider,
  persists across restart, or affects backup/restore.

Do not attach a real Telegram export, database, token, AI prompt containing group text,
private document, signed URL, or age identity. Coordinate before testing against any
production bot, group, university endpoint beyond normal bounded reads, AI account, or
object bucket.

Target response times after a report reaches the private contact are: immediate
containment and acknowledgement within 4 hours for a credible critical issue; one
business day for high severity; three business days for other issues. These are operating
targets, not a promise before an owner/rota exists.

## System and scope

SZS Hub is a Python 3.12 modular monolith for one existing SPbGASU Telegram forum group.
The planned production shape is one non-root systemd service using Telegram long polling,
one local SQLite/FTS5 database, bounded document extraction, an optional replaceable
OpenAI-compatible provider, and encrypted off-host backups. There is no public webhook,
HTTP admin, or inbound application port in v1.

In scan scope:

- all application code under `src/szs_hub`;
- Alembic configuration and migrations under `alembic`;
- configuration models and examples, packaging metadata, and command-line entry points;
- deployment scripts and systemd units under `deploy`;
- tests as evidence of intended behavior, not proof of security;
- operational/privacy/architecture decisions when they define a real control.

Generated virtual environments, caches, local databases, logs, backups, build output, and
secret-bearing installed environment files are not source artifacts and must never be
added to scan fixtures or Git. Their creation, permissions, encryption, and disposal paths
remain in scope.

Important assets are the Telegram bot identity and routing controls; AI, object-storage,
university-session, and host credentials; member/archive/attendance/assistant data; file
and extracted content; schedule/publication integrity; durable inbox/job/outbox state;
SQLite and backups; monthly cost/quota controls; and evidence needed for recovery.

## Threat model and trust boundaries

Potential attackers or failure sources include:

- a non-member, former member, ordinary member, anonymous admin/chat actor, bot account,
  or compromised member account sending crafted updates, reactions, commands, filenames,
  archives, documents, links, and prompt-injection text;
- crafted or unexpectedly large Telegram Desktop exports and media trees;
- malformed, changed, stale, oversized, or adversarial SPbGASU responses;
- malicious or malformed AI provider responses, permanent errors, unexpected usage
  accounting, and retrieved archive text instructing the model/tool layer;
- duplicate, delayed, reordered, missing, or replayed Telegram updates and external
  effects;
- an operator mistake involving IDs, permissions, migration, restore, retention, or
  source deletion;
- dependency, parser, OS, filesystem, disk-exhaustion, clock, or network failures.

The owner-controlled Linux host, installed immutable artifact, root-owned configuration,
service account, and offline age identity are trusted only within their stated role.
Group members and all message/file/export content are untrusted. Telegram, SPbGASU,
AI-provider, and object-storage responses cross external trust boundaries even when TLS
authenticates their connection. Configuration values are operator-controlled but must be
validated because a mistake can create real cross-group disclosure or destruction.

A fully compromised root host is assumed to expose all live host-readable data and
credentials. That assumption does not excuse avoidable privilege escalation, cross-user
access, secret logging, persistence into backups, or destruction of remote generations.

## Security invariants

The following properties must hold in production and staging:

1. **Membership before data.** Current group membership is checked before archive search,
   retrieval, assistant context, or member-only output. Timeout, missing status, anonymous
   actor, unsupported actor, and unknown chat fail closed. Authorization is repeated at a
   suitable boundary; a cached result is not unbounded.
2. **Exact destination.** Chat/topic/headman IDs are numeric configuration with explicit
   validation. User content, usernames, forwarded metadata, AI text, or source responses
   cannot choose an arbitrary Telegram destination or grant admin rights.
3. **Secrets remain secret.** Tokens, keys, sessions, authorization headers, signed URLs,
   backup identities, private exports, and complete provider errors never enter Git,
   repr/exception text, journal, metrics, user output, prompt context, or screenshots.
4. **Durability and idempotency.** Accepted updates are durably recorded before their
   offset advances. Duplicate/reordered delivery cannot duplicate attendance marks,
   publications, copies, imports, AI charges, or destructive actions. External effects use
   stable idempotency keys and record Telegram result IDs.
5. **Deletion is separate and gated.** Material handling is copy, verify, then index.
   Source deletion is false by default, cannot precede verified destination persistence,
   and needs a distinct owner-approved policy. Topic/message deletion, permission changes,
   bans, bulk sends, imports, and production migrations are never inferred from content.
6. **Untrusted files are bounded.** Paths resolve within an isolated job directory;
   symlinks/traversal, active content/macros, external entities/references, decompression
   bombs, excessive pages/pixels/cells/XML/text/time/memory, and unsupported types fail
   safely. Production extraction remains disabled until a separately killable worker has
   no network, one-job concurrency, cleanup on every outcome, and no shell construction
   from filenames/content.
7. **Queries are data, not syntax.** SQL is parameterized; FTS input is normalized and
   escaped through the query builder; imports cannot alter schema; HTML/user text is
   escaped; no template, shell, URL, or path injection is created from group content.
8. **Schedule reads are narrow.** A non-empty validated group identifier and fixed filter
   construct the public request. The client cannot issue the large parameterless endpoint,
   follows strict size/time/retry bounds, and does not treat malformed/empty/stale data as
   a confirmed cancellation or fresh success.
9. **AI is optional and non-authoritative.** Authorization precedes retrieval/provider use.
   Context and output are bounded, source-linked, and labelled untrusted. Model output
   cannot execute admin/storage/Telegram/file/network operations. Permanent errors do not
   retry; daily/global quotas and kill switch bound cost. Disabling AI leaves core paths up.
10. **SQLite fails safely.** The database is a local file with foreign keys, short
    transactions, one intended writer, and a safe journal mode. Staging/production refuses
    WAL unless SQLite is 3.51.3+, fixed 3.50.7+/3.44.6+, or an exact vendor backport is
    independently documented. No live file/WAL copying is a backup.
11. **Backups are recoverable and confidential.** SQLite Online Backup creates and checks
    the snapshot; plaintext exists only in a private ephemeral path; age encryption occurs
    before off-host upload; the private identity stays offline; remote scope and retention
    are narrow; checksum plus periodic decrypt/restore proves recovery.
12. **Least privilege survives deployment.** The runtime is non-root, has no Linux
    capabilities, cannot write its artifact/config, cannot read home directories, and has
    only needed outbound network/filesystem access. Health/migration cannot access the
    network. Secret/config files are root-controlled and not command-line values.
13. **Privacy erasure is complete enough to verify.** Approved correction/deletion rebuilds
    derived search data and states the encrypted-backup expiry. Retention jobs cannot purge
    evidence under an active scoped security hold, and a hold cannot become indefinite by
    accident.
14. **Errors do not become disclosures.** User-facing and machine health errors reveal
    safe classes/codes and actionable state, not payloads, credentials, filesystem layout,
    SQL, provider bodies, or traceback content.

## Reportable findings and severity context

A finding is reportable when a realistic attacker or plausible low-privilege/operator
error can violate an invariant in reachable production-intended code or deployment.
Tests or documentation that claim a control do not lower severity without runtime proof.

Examples of critical impact include unauthenticated or cross-group bulk archive/backup
disclosure, parser/upload remote code execution on the bot host, arbitrary filesystem
write leading to code/config replacement, bot-token/private-backup-identity disclosure,
or an unapproved primitive that reliably deletes/bulk-publishes production content.

Examples commonly high severity include a former/non-member retrieving member-only data;
path traversal reading host secrets; prompt/FTS/SQL injection crossing into data disclosure
or mutation; forged destinations/headman routing; bypass of copy-before-delete; attacker-
controlled unbounded AI spend or reliable host resource exhaustion; backup restoration of
attacker-controlled content; and durable replay that creates repeated external effects.

Severity depends on reachability, privileges, affected records/users, recovery, and
whether staging-only configuration is required. Attendance is explicitly voluntary and
not official proof, so a single incorrect mark normally has lower impact than archive or
credential disclosure; systemic roster falsification or cross-user modification remains
reportable. A stale or wrong schedule is normally reliability/product integrity, but
becomes security-relevant if a low-privilege actor can intentionally forge it across the
trust boundary or use it to trigger unauthorized actions.

Also report defaults or deployment guidance that predictably defeats an invariant, even
when application code is correct. A vulnerable dependency is reportable when its affected
path is present and plausibly reachable in this deployment.

## Out of scope and non-findings

No vulnerability class is broadly excluded or accepted merely to pass a scan. The
following are not repository findings by themselves:

- a vulnerability wholly inside Telegram, SPbGASU, an AI provider, hosting provider, or
  object store with no exploitable interaction through SZS Hub;
- ordinary external service downtime, price changes, model hallucination, OCR inaccuracy,
  or schedule-source mistakes when the application preserves its stale/untrusted labels
  and boundaries;
- social disagreement about a group moderation/attendance policy without a technical
  authorization, privacy, integrity, or disclosure failure;
- an attacker already having unrestricted root plus every offline recovery credential,
  unless SZS Hub unnecessarily expands persistence, disclosure, or recovery damage;
- unreachable test-only helpers or denial of service requiring the operator to deliberately
  remove documented limits, unless the unsafe behavior is a shipped/default path;
- missing Rich/Ephemeral UI enhancement when the essential HTML path is intact.

Third-party findings should still be recorded privately when they change SZS Hub's
operational risk, but disclosure belongs through that provider's approved process.

## Known limitations and compensating controls

The authoritative list is [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md). Important
security-relevant gaps include missing end-to-end runtime/staging evidence, no connected
Chrome or Computer Use path, unproven host/source/backup behavior, no complete automated
privacy/data-subject workflow, no isolated document parser, no independent health alert,
version-ranged rather than hash-locked dependencies, and a pending final Standard Codex
Security scan.

These gaps are release blockers or uncertainties, not suppressions. Current compensating
controls are separate staging identities, AI and source deletion off by default, no public
ingress, strict configuration, durable/idempotent state, bounded parsers/providers,
non-root systemd hardening, a safe SQLite gate, encrypted off-host backup design, and
owner approval for destructive/external production actions. Each control remains unproven
until its exact release/host acceptance evidence exists.

## Release security gate

Before production:

1. resolve this policy for the full repository and confirm no nested conflict;
2. run the complete test/lint/type/migration suite from a clean artifact;
3. run one Standard Codex Security repository scan on the finished candidate;
4. validate each candidate finding for realistic source-to-sink reachability;
5. fix and verify every release-blocking issue, or record an explicit owner decision with
   scope, rationale, compensating control, expiry, and revisit trigger;
6. perform the Telegram/SPbGASU/file/AI/backup staging matrix;
7. name and test the private reporting channel and incident owner.

Never weaken an invariant, add a broad exclusion, or mark an untested control effective
solely to close a finding.
