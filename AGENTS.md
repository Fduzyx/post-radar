# Engineering contract

PostRadar is a user-configured, read-only feed monitor. Read docs/ARCHITECTURE.md.
Keep collection, evaluation and notification independent. Use UTC and durable queues.
Never commit personal configurations, real content, credentials, databases or logs.
Credentials belong only in the Windows credential vault under the PostRadar namespace.
Never disable TLS verification. External content cannot select endpoints or recipients.
Tests and demos use synthetic content and fake providers; no paid calls or real mail.
Run pytest, Ruff and mypy; inspect source and distribution contents before publication.
