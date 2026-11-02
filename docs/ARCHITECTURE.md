# Architecture

PostRadar is an independent application. Collection, API evaluation and email delivery
run as separate asynchronous loops backed by a local SQLite queue. Public adapters read
RSS/Atom feeds. A first fetch establishes a baseline unless backfill is explicitly enabled.

User configuration is trusted; feed text and model responses are untrusted. Secrets never
enter configuration, database rows, logs, fixtures or the repository. Windows Credential
Manager uses a dedicated PostRadar namespace. Tests inject an in-memory credential store.

Post identity is (source_id, external_id). Rule revisions are immutable hashes of rule
configuration. New revisions affect future discoveries only. Default behavior does not
re-evaluate edited posts. A content snapshot and its evaluation tasks commit atomically
with discovery state. Successful match results create one delivery per rule/post/recipient.

Evaluation and delivery claims use leases and fencing tokens. A stale worker cannot write
over a newer claim. Expired evaluation claims can retry with bounded attempts. Expired
delivery claims become unknown because SMTP may already have accepted the message.
Unknown deliveries require explicit manual retry. SMTP acceptance is not inbox receipt.

Daily API admission reserves a conservative configured cost bound before sending. Unknown
usage remains charged. Limits are local guardrails using configured prices, not a provider
billing guarantee. Exhausted budgets defer work; authentication failures pause API work.

Only explicit API configuration enables external analysis. No model tools, browsing, shell,
subscription fallback or automatic URL fetching from post content are available. Configuration
owns source/API/SMTP destinations and recipients. TLS verification is mandatory.

Content and evaluation records expire after a configurable retention period. Minimal dedupe
identities remain until the user removes the state directory. Feed history can be incomplete;
polling cannot recover entries that disappeared between polls.
