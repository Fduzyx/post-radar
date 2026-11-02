# Extending PostRadar

Most users only need `config/local.toml`: sources, keyword conditions, requirements,
recipients, and provider settings. An adapter is trusted Python code for a new service.
V1 does not dynamically import code from configuration or execute code from posts.

The contracts are in `post_radar.engine`; normalized messages are in `post_radar.models`.
Instantiate `Engine(config, store, source, evaluator, mailer)` to substitute an adapter.

| Interface | Contract |
| --- | --- |
| `SourceAdapter.fetch(source, state)` | Async; returns `FetchResult` of normalized `Post` objects |
| `Evaluator.estimate_cost(post, rule)` | Returns conservative integer micro-USD reservation |
| `Evaluator.evaluate(post, rule)` | Async; returns `EvaluationOutcome` with `MatchResult` and known cost or `None` |
| `Mailer.send(message)` | Async; one configured recipient; return only when submission is accepted |

Source IDs are chosen in configuration. `external_id` must be stable within a source;
two sources may use the same external ID. Never generate random IDs per fetch. The
first fetch establishes a baseline unless backfill is enabled. RSS state currently
contains initialization, ETag and Last-Modified. Adapters requiring other durable
cursor state need an explicit schema extension and recovery tests.

Treat post bodies as data. Never let content select an API endpoint, recipient,
credential name, command or file path. Validate URLs and preserve TLS verification.
Use bounded responses and total deadlines. Do not fetch links merely because a post
or model output contains them. Errors crossing an adapter boundary must use a safe
`ExternalError` code, never a provider body, header or credential.

Results are `match`, `no_match` or `uncertain`. A match needs nonempty, verbatim source
evidence and no missing required information. These checks cannot prove semantic
accuracy: a model can still misinterpret a post. No notification is an instruction
for the application to act on a post.

Unknown API costs must remain reserved. An SMTP timeout after sending DATA may mean
the server accepted the mail; return an uncertain error rather than an ordinary retry.
The engine quarantines uncertain deliveries and requires explicit operator retry after
a cooldown. Stable Message-ID values do not guarantee recipient-side deduplication.

Test new adapters with synthetic local responses. Cover malformed inputs, timeout,
rate limiting, credential redaction, cancellation and restart behavior before use.
