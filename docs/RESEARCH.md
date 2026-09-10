# Research synthesis

Evidence reviewed on 2026-08-29. Links point to primary sources unless explicitly noted.

## Telegram capabilities and constraints

The current [Bot API changelog](https://core.telegram.org/bots/api-changelog) identifies Bot API 10.3, released 2026-08-24. SZS Hub can use the normal Bot API; an MTProto userbot is neither required nor justified for the planned production behavior.

### Forum topics

- Sending and copy methods accept `message_thread_id`, including [sendMessage](https://core.telegram.org/bots/api#sendmessage), [sendMediaGroup](https://core.telegram.org/bots/api#sendmediagroup), and [copyMessages](https://core.telegram.org/bots/api#copymessages).
- Bot API cannot enumerate existing forum topics. Telegram's `messages.getForumTopics` is user-only. Existing topic IDs therefore need a one-time bootstrap from real messages or links; future IDs are stored when the bot observes or creates topics.
- A reaction update contains `chat + message_id`, not the topic ID. The message registry must retain the topic mapping.

### Attendance reactions

- [`message_reaction`](https://core.telegram.org/bots/api#messagereactionupdated) carries the actor and full old/new reaction sets, but requires the bot to be an administrator and requires explicit `allowed_updates` configuration.
- `message_reaction_count` is delayed aggregate data and cannot identify a student.
- Bot API has no read endpoint for the complete current user-reaction list. Individual attendance is therefore an idempotent update ledger, not a value that can be reconstructed after a long update gap.
- Only the configured emoji from a real `user` actor counts. `actor_chat`, anonymous aggregates, other emoji, duplicates, and events outside the open window are ignored.
- Attendance uses a dedicated text message rather than an album item because album reaction behavior targets the first surviving media item.

### Updates and reliability

- Telegram retains pending updates for no more than 24 hours. Webhook and long polling are mutually exclusive. [`update_id`](https://core.telegram.org/bots/api#update) is the deduplication key.
- `chat_member`, `message_reaction`, and `message_reaction_count` are not included by default. SZS Hub will set an explicit allowlist rather than inherit a previous bot configuration.
- Webhook mode must validate `X-Telegram-Bot-Api-Secret-Token`, persist a raw inbox row before acknowledging, and process asynchronously. Polling mode uses the same durable inbox contract before advancing its confirmed offset.
- Ordinary user-message deletion is not exposed as a Bot API update. The archive needs an owner-operated purge path and must disclose the possible lag between Telegram deletion and its private search index.

### Materials and files

- [`copyMessage`](https://core.telegram.org/bots/api#copymessage) and [`copyMessages`](https://core.telegram.org/bots/api#copymessages) are copy operations, not atomic moves. The safe state machine is copy → verify destination → optionally delete source.
- Source deletion is normally limited to messages younger than 48 hours and requires administrator rights. Source deletion remains disabled by default until the group approves the UX.
- Incoming albums arrive as individual updates sharing `media_group_id`; there is no official completion marker. A durable quiet-window collector is required.
- Cloud Bot API [`getFile`](https://core.telegram.org/bots/api#getfile) downloads files up to 20 MB. `file_id` is reusable by the same bot; `file_unique_id` is a cross-bot deduplication hint but cannot download content. Real file-size distribution must be measured before considering the operational cost of a local Bot API server.

### Member-only assistant

- [`getChatMember`](https://core.telegram.org/bots/api#getchatmember) is reliable for arbitrary members when the bot is an administrator. Access must fail closed for `left`, `kicked`, and a restricted user whose `is_member` is false.
- A bot cannot initiate a normal private chat. The assistant entry point uses an opaque deep-link payload and a one-time Start. No personal data is encoded into the payload.
- Bot API 10.2/10.3 added [Ephemeral Messages](https://core.telegram.org/bots/api#ephemeral-messages-and-commands). They are promising for quiet group interactions but have non-guaranteed lifetime/delivery and require client and aiogram compatibility tests. They cannot be the only delivery path for important information.

### Presentation

- Critical schedule cards use ordinary HTML/entities as the dependable baseline.
- [Rich Messages](https://core.telegram.org/bots/features#rich-messages) are a progressive enhancement only after Android, iOS, Desktop, Web, and library spikes. A schedule must remain legible on clients without the newest rich primitives.

## SPbGASU schedule discovery

- The university's official [schedule page](https://www.spbgasu.ru/students/raspisanie/) links to an embedded all-forms page, which in turn identifies `https://rasp.spbgasu.ru/` as the direct schedule application.
- The official 2025 freshman guide describes public lookup by group number and states that the current week appears automatically; this is evidence that student credentials may not be necessary for the ordinary group timetable.
- On 2026-08-29, `rasp.spbgasu.ru` resolved to `92.255.65.12`, and TCP/443 succeeded from the local machine, but both HTTP and HTTPS application requests timed out without response. The route used a tunnel interface. This is a negative result, not evidence that the service is permanently unavailable.
- The requested Chrome connection was unavailable, so JavaScript bundles and Network/XHR calls have not yet been observed in the user's Chrome session. Endpoint selection remains provisional until that experiment or a reproducible public HTTP request succeeds.

### Confirmed public request contract

The ordinary full-time group client calls:

```http
GET https://rasp.spbgasu.ru/local/components/gasu/raspisanie.csv/ajax.php
    ?SERACH=<group identifier>
    &FILTER=GROUPS
    &GROUP=
    &SELECT=*
Accept: application/json
```

`SERACH` is the source's real misspelling and must not be corrected. A validly formed unknown group returns `[]` without a login redirect. The current response is wrapped in `R`; older first-party JavaScript expected the day mapping at the root, so the parser accepts exactly those two shapes. The payload contains weekdays → lesson slots → `числ.`/`знам.` entries with `DATE`, `LESSON`, `GROUP`, `AUDITORIUM`, and `PROFESSOR`.

The page embeds `window.GROUPS` and filters group suggestions locally after three characters; selecting a group triggers the GET. It also embeds `window.NUMBER_WEEK`; day and numerator/denominator switching are client-side and should not issue more requests. The bot therefore fetches one full weekly template per group, caches it for at least five minutes, and materializes dates locally from the official current week number.

Important failure evidence:

- calling the endpoint without parameters returns a large aggregate response; application code cannot construct this request;
- invalid `FILTER` produced an HTTP 500 with a verbose Bitrix trace; application code fixes `FILTER=GROUPS`;
- direct origin access from the current tunnel route timed out, so origin rate headers remain unknown;
- the safe initial policy is one group request every 5–15 minutes, concurrent request collapse, bounded response size, retry only transient errors, and respect for `Retry-After`.

The official [bell schedule](https://doc.spbgasu.ru/oipip_raspisanie/raspisanie_zvonkov.pdf) maps slots 1–7 from 09:00–10:30 through 20:15–21:45. The mapping is versioned/configurable because the JSON payload identifies slots rather than providing a trusted standalone time table.

The public IBFO/part-time flow uses a separate Bitrix component POST with a public `PHPSESSID`/`sessid`. It is intentionally out of the first vertical slice until exact form serialization is captured through Chrome Network; user cookies will never be copied into the bot.

## Implementation baseline already verified

- Python 3.12.13 project-local environment.
- aiogram 3.31, FastAPI 0.141, SQLAlchemy 2.0, Alembic 1.19, Pydantic 2.13, HTTPX 0.28.
- Pure domain tests cover semantic schedule changes, reaction add/remove/duplicates/window close, conservative material routing, and the copy-before-delete state machine.

## Hosting, database, search, and AI

- Current cost comparison selects a 200 ₽ 4VPS candidate plus approximately 9.50 ₽ of Yandex off-site object storage. RUVDS at approximately 358.50 ₽ including the same backup is the fallback; larger Selectel, Timeweb, and Yandex VM combinations exceed the ceiling. Full figures and primary links are in `COST.md`.
- Long polling removes the need for a public IP/HTTPS ingress beyond the VPS plan and keeps local SQLite, jobs, FTS, and OCR in one operational unit.
- SQLite/FTS5 is preferred for the measured scale. WAL must be version-gated because [SQLite documents a 2026 WAL-reset race](https://sqlite.org/wal.html#the_wal_reset_bug) fixed in 3.51.3+ and specific backports. Local Python reports SQLite 3.53.1.
- Russian retrieval uses NFKC/casefold/`ё→е`, application-side Snowball stems, group aliases, FTS5 BM25, and metadata filters. Embeddings wait for an evaluation set showing real semantic misses.
- Document parsing is bounded and isolated. OCR is page-selective, Tesseract `rus+eng`, concurrency one; archives/Office files have decompression, page, pixel, cell, and timeout ceilings.
- Current official DeepSeek model IDs are mutable and must stay in configuration. Returned usage is metered even when the owner's third-party allowance is nominally free; AI stops before the full project budget is threatened.

Primary technical sources: [SQLite FTS5](https://sqlite.org/fts5.html), [SQLite Online Backup](https://sqlite.org/backup.html), [OCRmyPDF security guidance](https://ocrmypdf.readthedocs.io/en/stable/cloud.html), [DeepSeek updates](https://api-docs.deepseek.com/updates/).

## Required staging spikes

1. Reaction add, change, remove, `actor_chat`, reconnect, and lost-update behavior.
2. Existing topic bootstrap and message-to-topic mapping.
3. Album quiet-window collection and copy mapping with a partially non-copyable batch.
4. Cloud Bot API file-size histogram, including files over 20 MB.
5. Ephemeral/Rich compatibility matrix with HTML/private-chat fallback.
6. Member access for `member`, `administrator`, `creator`, `restricted`, `left`, and `kicked`.
7. SPbGASU group lookup, week/date navigation, empty day, changed response, outage, and stale cache.
