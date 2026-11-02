# PostRadar

PostRadar monitors RSS and Atom feeds, filters new posts with your keywords, and
uses your configured AI API to check whether they meet your requirements. Matching
posts enter a durable email queue.

You choose the sources, rules, API provider and recipients. The application reads
feeds; it does not post, reply or act on the content it finds.

## What it does

- Include any or all of your keywords and exclude unwanted terms.
- Describe the requirements for a match in plain language.
- Request structured AI results with a decision, reason and supporting excerpts.
- Email only validated matches; uncertain results do not trigger mail.
- Keep collection, evaluation and delivery independent, with durable state and retries.
- Limit API admission with a daily budget based on your configured prices.
- Store API and SMTP credentials in Windows Credential Manager.

For example, a subscription can find upcoming public workshops that include a
registration link. The included examples and offline demonstration are synthetic.

## Requirements and installation

The supported credential-backed runtime is Windows with Python 3.12 or 3.13.
Linux CI verifies the core with fake credentials; Linux production credential
storage is not supported in this release.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```powershell
git clone https://github.com/Fduzyx/post-radar.git
cd post-radar
uv sync --locked --group dev
uv run post-radar demo
```

`demo` runs offline with synthetic posts, a fake evaluator and a fake mail sink.
It needs no credentials and makes no paid API requests or email deliveries.

## Configure a subscription

```powershell
uv run post-radar init
```

Edit the generated local configuration using
[`config/example.toml`](config/example.toml) as the reference. Configure:

| Setting | Purpose |
| --- | --- |
| Sources | Feed URLs, polling intervals and optional initial backfill |
| Rules | Source IDs, keyword conditions, plain-language requirements and recipients |
| API | Protocol, HTTPS endpoint, model, token limits, prices and daily budget |
| SMTP | Server, TLS mode, sender and login name |
| Retention | How long to retain candidate content and evaluation records |

Secrets do not belong in TOML, environment files or command-line arguments. Store
them through the masked prompts:

```powershell
uv run post-radar secret set api-key
uv run post-radar secret set smtp-password
uv run post-radar check --config config/local.toml
uv run post-radar doctor
```

The example disables API analysis and email delivery. Enable them only after
configuring the intended endpoints, recipients, model prices and budget. A provider
must support the selected structured-output protocol; compatibility is not assumed
for every service that exposes a similar API.

## Run and inspect

```powershell
uv run post-radar run --config config/local.toml --once
uv run post-radar run --config config/local.toml
uv run post-radar status --config config/local.toml
```

The first fetch establishes a baseline by default. Existing feed entries are not
analyzed or mailed unless that source explicitly enables backfill. Rule changes
apply to future discoveries; they do not automatically replay history. Edited posts
are not automatically evaluated again in this release.

Configuration is read at startup. Stop the process, edit the configuration, and
restart it for changes to take effect. After fixing an API credential or a paused
source, clear its persistent pause explicitly:

```powershell
uv run post-radar resume-api --config config/local.toml
uv run post-radar resume-source events --config config/local.toml
```

Replace `events` with the configured source ID. These commands clear a local pause;
they do not validate credentials or contact the provider.

Feed history can be incomplete: entries removed between polls cannot be recovered
from a feed that no longer serves them. Stable source IDs and entry IDs are needed
for reliable deduplication.

SMTP acceptance does not prove inbox receipt. If a connection or process fails at
an ambiguous point in delivery, the item becomes `unknown` and is not automatically
resent. After checking the receiving mailbox, an operator can explicitly retry:

```powershell
uv run post-radar retry-delivery DELIVERY_ID --config config/local.toml
```

A manual retry may produce a duplicate if the first attempt was accepted.
Unknown deliveries also have a cooldown before manual retry; an expired retained
item cannot be retried. `status` lists the IDs of unknown deliveries.

Missing credentials or authentication failures pause the corresponding API or mail
worker. After correcting your credentials, use `post-radar resume-api` or
`post-radar resume-mail`. Commands accept the same `--config` option as `run`.

## Privacy and boundaries

Enabling AI analysis sends matching candidate text and the rule requirements to
the configured API provider. Enabling mail sends the notification to your configured
SMTP service and recipients. Review those services' policies before supplying content.
PostRadar does not provide cloud hosting or a shared backend.

The daily budget is a local admission guard based on configured prices and
conservative reservations. It is not a guarantee about a provider's invoice. Failed
requests with unknown usage keep their reservation.

Feed text and model output cannot change endpoints or recipients. The evaluator has
no shell, browsing or model tools. TLS verification is always required. Local state
contains candidate content for the configured retention period; minimal deduplication
identities persist until the state directory is removed. Protect that directory as
personal data. Retention removes application records; it is not a secure-erasure
guarantee for storage media or backups. See [SECURITY.md](SECURITY.md) for the security
model and reporting.

## Development

```powershell
uv sync --locked --group dev
uv run pytest -q
uv run ruff check .
uv run mypy src
uv build
uv run python scripts/check_release.py --source --artifacts dist
```

Tests use synthetic data and fake external services. The release check inspects
tracked source files and package members, rejects disallowed files and known secret
patterns, and reports locations without printing matching values. It is one check,
not proof that a publication contains no private information. Review the exact
source and distributions before releasing them.

See [the architecture](docs/ARCHITECTURE.md) and
[adapter guide](docs/ADAPTERS.md), plus the
[contribution guide](CONTRIBUTING.md). Licensed under [MIT](LICENSE).
