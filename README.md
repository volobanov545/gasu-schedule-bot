# SZS Hub

SZS Hub currently defaults to a deliberately small Telegram feature: it reads the public
SPbGASU schedule for one configured group and publishes evening cards and schedule changes
to one configured Telegram topic. It does not receive chat updates or process students'
messages, files, reactions, or Telegram user IDs in the default `schedule_only` profile.

The selected deployment is GitHub Actions, not a VPS or an always-on home computer.
The publishing workflow fetches tomorrow's schedule, sends one static card, and exits.
Its automatic schedule is temporarily paused because a real GitHub-hosted run confirmed
that `rasp.spbgasu.ru` is unreachable from that runner. Setup and bridge status are in
[docs/GITHUB_ACTIONS_RU.md](docs/GITHUB_ACTIONS_RU.md).

The runnable modular monolith, durable storage, schedule/attendance/archive flows, and
deployment artifacts are implemented and locally tested. Production actions remain
intentionally blocked until the remaining internal release gates, real Telegram/VPS
staging checks, security review, and explicit owner configuration are complete.

## Non-negotiable constraints

- Existing production topics and history are preserved until their real use is audited.
- Destructive production changes require an explicit approval at action time.
- Students do not register or provide SPbGASU credentials.
- AI is replaceable and cannot break schedule or attendance workflows.
- Infrastructure must remain below 500 RUB/month, with a target of 0–200 RUB/month.
- Secrets never enter Git, logs, screenshots, or documentation.

Engineering research is recorded in `docs/RESEARCH_LOG.md` and consolidated in the
decision, architecture, deployment, privacy, and limitations documents below.

## Owner and release documentation

- [Architecture](docs/ARCHITECTURE.md) and [decision journal](docs/DECISIONS.md)
- [Deployment guide](docs/DEPLOYMENT.md) and [operations runbook](docs/OPERATIONS.md)
- [Privacy and data handling](docs/PRIVACY.md)
- [Legal/data launch checklist, in Russian](docs/LEGAL_LAUNCH_RU.md)
- [Known limitations and release blockers](docs/KNOWN_LIMITATIONS.md)
- [Security policy](SECURITY.md)
- [Cost model and guardrails](COST.md)

The older VPS deployment templates under `deploy/` are retained as historical artifacts;
they are not used by the selected GitHub Actions release.

GitHub publishing requires only four repository secrets: bot token, target chat ID,
schedule topic ID, and SPbGASU group ID. The older full profile remains available for
development but is not invoked by either GitHub workflow.
