# Cost model

Prices and quotas were checked against official sources on 2026-08-29. Estimates use 720 hours/month and include VAT where the provider publishes VAT-inclusive prices. Actual invoices must be checked before production purchase.

## Selected baseline

| Component | Plan and expected load | Normal | Worst reasonable | Guardrail |
|---|---|---:|---:|---|
| Compute | 4VPS RU, 2 vCPU / 2 GB / 15 GB NVMe / IPv4 | 200 ₽ | 200 ₽ while price is unchanged | Do not renew if the published plan exceeds the cap; export/restore to fallback host |
| DB/search/jobs/logs | Local SQLite/FTS5, one writer, systemd/journald | 0 ₽ | 0 ₽ | Disk and WAL alerts; bounded logs and temporary files |
| HTTPS/NAT/registry | Long polling, no public ingress or paid registry | 0 ₽ | 0 ₽ | No web admin in v1 |
| Off-site backup | Yandex Object Storage, approximately 5 GB standard storage | 9.50 ₽ | approximately 21.40 ₽ at 10 GB billable after free 1 GB | Retention and lifecycle policy; alert at 8 GB |
| AI | Owner's current third-party DeepSeek-compatible allowance | 0 ₽ expected | 250 ₽ configured monthly ceiling | Per-user/day and global token/RUB quotas; kill switch before total reaches 500 ₽ |
| **Total** | | **209.50 ₽ expected** | **up to 471.40 ₽ guarded** | Hard stop at 480 ₽ projected spend |

The compute plan is a candidate, not yet purchased. It must pass 7–14 days of CPU steal, disk, reboot, packet-loss, Telegram, and SPbGASU connectivity tests. [4VPS plans](https://4vps.su/vps/ru), [Yandex Object Storage pricing](https://yandex.cloud/ru/docs/storage/pricing).

## Fallbacks considered

| Option | Full practical monthly cost | Result |
|---|---:|---|
| Existing home mini-PC/NAS + 5 GB Yandex backup | about 9.50 ₽ plus 3.6–10.8 kWh electricity at 5–15 W | Cheapest if a reliable always-on device already exists; power/ISP/disk risks need mitigation |
| RUVDS START HIT SSD 1 vCPU / 1 GB / 20 GB + 5 GB Yandex backup | about 358.50 ₽ | Operational fallback if 4VPS burn-in fails; 1 GB requires one OCR job and swap |
| Cloud.ru Free VM + IP/backup | potentially 0–146.88 ₽ | Only for eligible accounts created before the current cutoff; free terms can change |
| Yandex serverless | 0–100 ₽ at very low use | Rejected for v1: webhook/external DB redesign breaks the simple SQLite/FTS/OCR monolith |
| Selectel Shared Line + IPv4 + backup | at least 543.71 ₽ | Above hard ceiling |
| Timeweb MSK40 + IPv4 | about 1,050 ₽ | Above hard ceiling |
| Yandex Compute VM | more than 1,413 ₽ before disk/backup | Above hard ceiling |
| VK Cloud | Unverified | Rejected until an official current RUB quote can be saved |

Primary pricing sources: [RUVDS VPS](https://ruvds.com/ru/vps_start/), [RUVDS backup](https://ruvds.com/ru/helpcenter/backup/), [Cloud.ru Free VM](https://cloud.ru/docs/virtual-machines/ug/topics/overview__free-tier), [Selectel pricing](https://selectel.ru/prices/), [Timeweb VPS](https://timeweb.cloud/services/vps-linux), [Yandex Compute](https://yandex.cloud/ru/docs/compute/pricing), [Yandex VPC](https://yandex.cloud/ru/docs/vpc/pricing).

## AI spend model

Model IDs and prices are configuration, never code constants. DeepSeek's official 2026 production lineup and tariffs can change; the configured third-party provider is expected to be free but the application still meters returned usage. [DeepSeek pricing](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/), [model updates](https://api-docs.deepseek.com/updates/).

At the 2026-08-28 CBR rate of 12.7691 ₽/CNY, an illustrative Flash request with 2,000 uncached input tokens and 500 output tokens costs about 0.067 ₽ off-peak or 0.134 ₽ peak on the official API. Thousands of uncontrolled questions can therefore consume the entire project ceiling even when compute is cheap. [CBR daily rate](https://www.cbr.ru/currency_base/daily/?UniDbQuery.Posted=True&UniDbQuery.To=28.08.2026).

Required controls:

- bounded retrieved context and output tokens;
- per-user daily quota and global monthly token/RUB quota;
- cache keyed by normalized input, prompt version, provider, and model;
- no retry for authentication, balance, validation, or other permanent errors;
- global AI kill switch that does not affect schedule, attendance, archive, or local search;
- budget alert at 350 ₽ projected total and automatic AI stop at 480 ₽ projected total.

## Backup accounting

Use SQLite Online Backup API or `VACUUM INTO`; never copy a live database file as the primary backup method. Compress, encrypt locally, upload, verify checksum, and retain 7 daily + 4 weekly + 3 monthly snapshots within the storage cap. [SQLite Online Backup API](https://sqlite.org/backup.html).

