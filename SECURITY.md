# Security

## Scope and trust boundaries

PostRadar runs locally. Configuration is trusted operator input. Feed content and
model responses are untrusted data. They cannot choose network destinations,
credentials or email recipients, and they cannot execute tools or commands.

API and SMTP credentials are read from Windows Credential Manager under the
PostRadar namespace. No plaintext credential fallback is supported. Never include
credentials in configuration, URLs, logs, fixtures, screenshots, issue attachments
or environment files. Do not share local configuration or the state directory.

HTTPS and SMTP TLS certificate validation are mandatory. Configure only endpoints
you trust. The application does not follow links from post content to fetch extra
pages. API analysis and SMTP delivery require explicit configuration.

## Data handling

Candidates sent for evaluation leave the local machine and are processed by the
configured API provider. Email content is processed by the SMTP provider and sent
to the recipients configured by the operator. Local candidate content and evaluation
records have configurable retention. Minimal deduplication records remain after
content cleanup. Retention is application-level deletion and does not promise secure
erasure from database pages, storage media or backups.

Errors exposed by external-service boundaries use bounded local error codes rather
than copying arbitrary server responses. Treat local files and diagnostic output as
potentially personal information and review them before sharing.

## Delivery and spending limitations

An API admission budget uses configured model prices and conservative reservations.
It cannot certify provider billing. Requests with unknown usage retain their charge
reservation. Do not interpret a local usage report as the provider's invoice.

Email has an ambiguous delivery window: a server may accept a message before a
connection or process failure becomes visible. Such deliveries require operator
review and explicit retry. Automatic retry cannot guarantee both no loss and no
duplicates across that boundary.

## Reporting a vulnerability

Use the repository's **Security > Report a vulnerability** private reporting form
if it is available. If private reporting is unavailable, open an issue containing
only a request for a private contact channel. Do not post exploit details, personal
content, credentials or an unreviewed database in a public issue.

Provide the affected version, a concise description and a reproduction using
synthetic data. If a credential was exposed, revoke it with its provider; removing
it from the latest source file does not remove old Git history or other copies.

Security fixes target the latest published version. A release scan is a useful
guardrail, but it does not establish that all secrets or personal data are absent.
