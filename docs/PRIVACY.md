# Privacy and data-handling policy

2026-09-03: `run` now defaults to the verified-in-unit-tests `schedule_only` profile and
a pre-connection stop through `LIVE_PROCESSING_APPROVED=false`. This profile does not
request Telegram updates and therefore does not archive members, messages, files, or
reactions. The sections below describe the dormant `full` profile; see the
[Russian launch review](LEGAL_LAUNCH_RU.md) before enabling it.

This document describes the intended SZS Hub data boundary. It is an engineering policy,
not legal advice. Before production, the group owner must publish a short member notice,
choose a private contact for requests/incidents, confirm provider terms and applicable
rules, and record approval for any historical import or AI processing.

## Principles

- Collect only what makes the existing group functions work.
- Do not ask students for SPbGASU credentials, passwords, location, phone contacts, or a
  separate SZS Hub registration.
- Treat Telegram membership as an authorization signal, not proof of identity or
  enrollment.
- Keep ordinary schedule, attendance, archive, and local search functional without AI.
- Send a provider only the minimum bounded context needed for an authorized question.
- Store secrets separately from user data and never in logs, prompts, source, or database
  backups; only encrypted off-host backup transport may contain the database itself.
- Prefer reversible copy/index actions. Automatic source-message deletion is off.
- Make stale, incomplete, inferred, and imported data visibly different from confirmed
  current data.

## Data inventory

The schema can contain:

| Category | Examples | Why it exists |
|---|---|---|
| Telegram identity | numeric user ID, username, display-name snapshot, language code, bot flag | membership gate, attribution, attendance roster, source links |
| Membership | chat ID, current status, join/leave/check times, headman flag | fail-closed access and voluntary attendance summary |
| Group structure | chat/topic IDs, topic names and state | route cards/materials without renaming or deleting topics |
| Messages | message ID, author, topic, reply/forward metadata, text/caption/entities, timestamps | group archive, search, material context, idempotency |
| Files | Telegram file IDs, unique ID, name, MIME/size/hash, bounded extracted/OCR text, optional protected path | deduplication, material organization, local search |
| Reactions/attendance | session message, configured reaction, window, user mark/remove times | voluntary headman summary; not geolocation or official attendance proof |
| Schedule | group key, raw source payload, parsed lessons, hashes, changes, fetch time | cards, stale cache, semantic change notices and audit |
| Operations | raw accepted Telegram update, handler result, job/outbox payload/state, Telegram result ID, safe error class/detail | retry, idempotency, incident diagnosis |
| Assistant | conversation user/chat, user and assistant content, sources, provider/model/token count | follow-up context, source attribution, quotas and debugging |
| Administration | import fingerprint/statistics, secret-free runtime recovery values, actor and target metadata | repeat-safe import, recovery and change audit |
| Search | exact/normalized content and metadata; optional embedding column | local member-only retrieval; embeddings are not enabled by default |

Raw Telegram updates may duplicate message/user fields. They are a reliability boundary,
not permission to retain everything forever. Forward metadata can identify people outside
the current group and needs the same protection as direct member data.

## What is intentionally not collected

- SPbGASU usernames/passwords or a persistent student browser account;
- location, device contacts, microphone/camera data, or presence inference;
- private chats the user has not explicitly opened with the bot;
- a user-account/MTProto session (no userbot);
- full Telegram history through scraping;
- advertising, analytics, fingerprinting, or behavioral profiles;
- biometric recognition or face matching from uploaded media;
- the entire archive in an AI prompt.

Telegram Desktop history is imported only from an owner-created JSON export after a
dry-run report, scope review, member notice, and explicit approval. The export itself is
temporary sensitive source material and is not a normal backup format.

## Access boundaries

- Group/archive assistant access requires a fresh/current Telegram `getChatMember`
  decision and fails closed on timeout, unknown status, or unsupported actor type.
- Numeric Telegram IDs are the authorization key; usernames and display names are
  presentation only.
- The headman receives the configured summary only because their numeric ID is explicitly
  configured. Being headman does not automatically grant server or backup access.
- Server login, BotFather, AI billing, object storage, and offline backup decryption are
  separate roles/credentials. Combine them only when the owner accepts the resulting risk.
- The age private identity stays off the VPS. Object storage receives ciphertext plus a
  checksum; the service host keeps only the public recipient.
- Direct database, export, log, and backup access is limited to the owner/operator and
  must not be exposed through Telegram admin commands.

Membership checks can fail during Telegram outages. An outage must deny a new archive/AI
request rather than reuse an unbounded stale authorization.

## Data flows and external parties

### Telegram

Telegram carries bot updates and outbound group/private messages. Telegram retains and
processes that data under its own service terms. SZS Hub cannot enumerate old topics,
recover full history, list every current per-user reaction, or initiate a private chat
before the user starts it.

### SPbGASU schedule source

The public full-time request contains the configured group identifier, not a student
identity. The optional `SPBGASU_SESSION` remains unset unless a later approved adapter
requires it; a browser/session value is a secret and must never enter a log or research
fixture.

### AI provider

AI starts disabled. If enabled, the application sends a bounded question/context bundle,
with retrieved excerpts labelled as untrusted data. It should include only sources the
current member may access. Provider/model identifiers and terms can change; the owner
must approve current retention/training/location terms before enabling a key. A kill
switch, request/token quotas, timeouts, output bound, and permanent-error no-retry policy
are mandatory. Provider output is untrusted and must not authorize or execute admin work.

### Hosting and off-host backup

The selected VPS stores the live SQLite database, encrypted-local backup cache, and
bounded journal metadata. Private Yandex Object Storage is the current candidate for
age-encrypted off-host generations. Provider-side encryption supplements, but does not
replace, age encryption. The storage account must have public access disabled.

No marketing analytics, error-reporting SaaS, vector database, managed database, or
public web admin is part of v1.

The database contains a `runtime.configuration` recovery manifest after a successful
startup probe. It includes non-secret numeric routing/group IDs and functional settings,
but excludes Telegram/AI/session/storage credentials. That manifest is included in the
encrypted database backup; credentials must be recovered from a separate protected
source or reissued.

## Retention schedule

The daily durable retention job now enforces the three narrow row-level periods marked
"automatic" below in bounded, idempotent transactions. The complete schedule remains a
production target: person-level attendance/schedule expiry, member export/erasure,
security-hold administration, and encrypted-backup expiry still require operator evidence.

| Data | Target retention | Disposal behavior |
|---|---|---|
| Plaintext extraction files | only during the bounded job | remove on success, failure, timeout, or restart cleanup; never place in backup tree |
| Journal | up to 14 days and a bounded host-wide size | journald vacuum according to approved host policy |
| Successfully processed raw inbox updates | 30 days | automatic redaction of the raw payload after the processed/ignored receipt is durable |
| Failed update/job/outbox diagnostic detail | 90 days after closure | automatic redaction of exception detail and failed raw update payload; retain status, timestamps, attempts, and safe identifiers |
| AI conversation/message content | 30 days after last activity by default | automatic conversation/message deletion; aggregate usage retention is not yet implemented |
| Attendance marks | through the current academic term plus 30 days | delete person-level marks; retain only an approved aggregate if needed |
| Schedule snapshots | current and previous academic year, unless incident evidence requires longer | delete old raw payload and derived lessons together |
| Group messages/material index | while the group archive purpose remains approved | owner/member request and annual scope review; do not silently promise permanent storage |
| Import source (`result.json` and media copy) | through reviewed import, reconciliation, and any separately approved managed-media migration | never place in release/backup trees; keep one private owner copy while the current metadata-only import still depends on it, then securely dispose under the approved procedure |
| Encrypted backups | newest 7 daily, 4 weekly, 3 monthly | exact prefix rotation; expiry also bounds delayed erasure from backup |
| Admin/security incident audit | minimum needed to close and review the event | redact user content; review annually |

Legal preservation or a live security incident may temporarily pause disposal for a
defined scope. Record who approved the hold, why, and its expiry; do not use an indefinite
hold by default.

## Member notice and controls

Before enabling the production bot, tell group members in plain Russian:

- what the bot does and which topics/messages/files it indexes;
- that reaction attendance is voluntary and is not location or official proof;
- whether historical content will be imported and the covered date range;
- whether an external AI provider is enabled and that local search works without it;
- who operates the bot and the private route for access/correction/deletion concerns;
- the high-level retention periods and backup deletion delay;
- that removing a Telegram message may not immediately erase an already indexed record
  or an unexpired encrypted backup, and how to request reconciliation.

An AI opt-out should leave local search and core group functionality usable. A member who
does not react is simply not marked; the system must not infer absence from other activity.

## Access, correction, and deletion requests

The owner must verify the requester through a private Telegram interaction already tied
to the numeric account or another approved method. Never ask for a password or identity
document in the bot.

For each request:

1. record only a minimal request ID, scope, verifier, and date;
2. locate direct identity, membership, attendance, AI, authored archive, file, search,
   and quoted/forwarded references;
3. show the owner the effects on shared conversation integrity before changing data;
4. export only to the verified requester through a private channel;
5. perform the smallest approved correction/deletion, rebuild affected search entries,
   and verify they no longer appear;
6. document when encrypted backup rotation will expire the old copy;
7. avoid deleting other members' authored content merely because it replies to the
   requester; pseudonymize linkage when appropriate.

The current repository does not yet expose a complete self-service export/purge command.
Until it does, requests require a reviewed, logged operator procedure and production
launch needs an owner who accepts that operational duty.

## Logging rules

Allowed: event class, stable internal ID, coarse size/count, status, duration, provider
name/model, token counts, safe exception class, source freshness, and release ID.

Forbidden: bot/AI/storage/session credentials; authorization headers; raw Telegram
updates; full messages/prompts/responses/documents; Telegram Desktop exports; full file
paths containing user-supplied names; signed URLs; age private identity; or a database
dump. Hashes and numeric IDs are still personal/security-relevant metadata and should be
included only when diagnosis requires them.

## Privacy incident

Stop the disclosure path without deleting evidence. Rotate affected credentials, preserve
bounded redacted logs and object access records, identify data/people/time range, and have
the owner assess notification and other obligations. Re-enable only after the access gate
or output path is fixed and tested. See [Incident response](OPERATIONS.md#incident-response).
