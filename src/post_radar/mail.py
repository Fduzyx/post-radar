"""One-recipient SMTP delivery with explicit uncertainty after a DATA attempt."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import smtplib
import ssl
import threading
from email.message import EmailMessage
from typing import Any, Protocol

from post_radar.config import SmtpConfig
from post_radar.credentials import CredentialStore
from post_radar.models import ExternalError, MailMessage
from post_radar.security import validate_email


class SMTPConnection(Protocol):
    def ehlo(self) -> tuple[int, bytes]: ...
    def starttls(self, *, context: ssl.SSLContext) -> tuple[int, bytes]: ...
    def login(self, user: str, password: str) -> tuple[int, bytes]: ...
    def send_message(
        self,
        msg: EmailMessage,
        from_addr: str,
        to_addrs: list[str],
    ) -> dict[str, tuple[int, bytes]]: ...
    def quit(self) -> tuple[int, bytes]: ...
    def close(self) -> None: ...


class SMTPFactory(Protocol):
    def __call__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        local_hostname: str,
        context: ssl.SSLContext | None,
    ) -> SMTPConnection: ...


def _connect(
    host: str,
    port: int,
    *,
    timeout: float,
    local_hostname: str,
    context: ssl.SSLContext | None,
) -> SMTPConnection:
    if context is not None:
        return smtplib.SMTP_SSL(
            host,
            port,
            timeout=timeout,
            local_hostname=local_hostname,
            context=context,
        )
    return smtplib.SMTP(host, port, timeout=timeout, local_hostname=local_hostname)


def _permanent(code: Any) -> bool:
    return isinstance(code, int) and 500 <= code <= 599


def _observe_background(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        # An already reported unknown delivery may finish after its deadline.
        task.exception()


class SmtpMailer:
    def __init__(
        self,
        config: SmtpConfig,
        credentials: CredentialStore,
        *,
        factory: SMTPFactory | None = None,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self._factory = factory or _connect

    async def send(self, message: MailMessage) -> None:
        abort = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(self._send, message, abort))
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self.config.timeout_seconds * 8,
            )
        except TimeoutError:
            abort.set()
            # A running DATA operation cannot be safely cancelled or retried automatically.
            raise ExternalError("delivery_unknown", uncertain=True) from None
        except asyncio.CancelledError:
            abort.set()
            # Wait for an active send to settle so the caller can record acceptance
            # before finishing shutdown. A pre-DATA abort remains safe to retry.
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=self.config.timeout_seconds * 8,
                )
            except TimeoutError:
                raise ExternalError("delivery_unknown", uncertain=True) from None
        finally:
            if not task.done():
                task.add_done_callback(_observe_background)

    def _send(self, message: MailMessage, abort: threading.Event) -> None:
        if not self.config.enabled:
            raise ExternalError("mail_rejected", permanent=True)
        try:
            recipient = validate_email(message.recipient)
            if not message.subject or any(ord(c) < 32 for c in message.subject):
                raise ValueError("invalid_subject")
            if len(message.subject) > 1000 or len(message.body) > 200000:
                raise ValueError("message_too_large")
            mail = EmailMessage()
            mail["From"] = self.config.sender
            mail["To"] = recipient
            mail["Subject"] = message.subject
            digest = hashlib.sha256(message.delivery_id.encode()).hexdigest()
            mail["Message-ID"] = f"<{digest}@post-radar.invalid>"
            mail.set_content(message.body)
        except (ValueError, TypeError):
            raise ExternalError("mail_rejected", permanent=True) from None
        try:
            password = self.credentials.get(self.config.credential_name)
        except Exception:
            raise ExternalError("credential_store_unavailable", permanent=True) from None
        if not password:
            raise ExternalError("missing_credential", permanent=True)
        connection: SMTPConnection | None = None
        sending = False
        try:
            context = ssl.create_default_context()
            connection = self._factory(
                self.config.host,
                self.config.port,
                timeout=self.config.timeout_seconds,
                local_hostname="localhost",
                context=context if self.config.mode == "ssl" else None,
            )
            connection.ehlo()
            if self.config.mode == "starttls":
                connection.starttls(context=context)
                connection.ehlo()
            connection.login(self.config.username, password)
            if abort.is_set():
                raise ExternalError("network_error")
            sending = True
            refusals = connection.send_message(
                mail,
                from_addr=self.config.sender,
                to_addrs=[recipient],
            )
            if refusals:
                raise ExternalError(
                    "mail_rejected", permanent=all(_permanent(v[0]) for v in refusals.values())
                )
        except ExternalError:
            raise
        except smtplib.SMTPAuthenticationError:
            raise ExternalError("authentication_failed", permanent=True) from None
        except smtplib.SMTPRecipientsRefused as exc:
            raise ExternalError(
                "mail_rejected", permanent=all(_permanent(v[0]) for v in exc.recipients.values())
            ) from None
        except smtplib.SMTPResponseException as exc:
            raise ExternalError("mail_rejected", permanent=_permanent(exc.smtp_code)) from None
        except Exception:
            if sending:
                raise ExternalError("delivery_unknown", uncertain=True) from None
            raise ExternalError("network_error") from None
        finally:
            if connection is not None:
                try:
                    connection.quit()
                except Exception:
                    # QUIT cannot undo an accepted message. Never create a duplicate retry.
                    with contextlib.suppress(Exception):
                        connection.close()
