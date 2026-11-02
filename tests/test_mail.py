from __future__ import annotations

import asyncio
import smtplib
import ssl
import threading
from email.message import EmailMessage

import pytest

from post_radar.config import SmtpConfig
from post_radar.mail import SmtpMailer
from post_radar.models import ExternalError, MailMessage


class Credentials:
    def get(self, name: str) -> str:
        return "synthetic-not-a-credential"


class Connection:
    def __init__(self, *, send_error: Exception | None = None, quit_error: bool = False) -> None:
        self.events: list[str] = []
        self.sent: list[EmailMessage] = []
        self.recipients: list[list[str]] = []
        self.send_error = send_error
        self.quit_error = quit_error
        self.context: ssl.SSLContext | None = None

    def ehlo(self) -> tuple[int, bytes]:
        self.events.append("ehlo")
        return 250, b"ok"

    def starttls(self, *, context: ssl.SSLContext) -> tuple[int, bytes]:
        self.events.append("tls")
        self.context = context
        return 220, b"ok"

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        self.events.append("login")
        return 235, b"ok"

    def send_message(
        self,
        msg: EmailMessage,
        from_addr: str,
        to_addrs: list[str],
    ) -> dict[str, tuple[int, bytes]]:
        self.events.append("send")
        if self.send_error:
            raise self.send_error
        self.sent.append(msg)
        self.recipients.append(to_addrs)
        return {}

    def quit(self) -> tuple[int, bytes]:
        self.events.append("quit")
        if self.quit_error:
            raise smtplib.SMTPServerDisconnected("synthetic disconnect")
        return 221, b"ok"

    def close(self) -> None:
        self.events.append("close")


class Factory:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.context: ssl.SSLContext | None = None

    def __call__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        local_hostname: str,
        context: ssl.SSLContext | None,
    ) -> Connection:
        self.context = context
        assert local_hostname == "localhost"
        return self.connection


def message(subject: str = "Workshop matches your rule") -> MailMessage:
    return MailMessage("delivery-one", "reader@example.invalid", subject, "Synthetic workshop.")


@pytest.mark.parametrize("mode", ["ssl", "starttls"])
async def test_smtp_tls_precedes_login_and_delivery_has_one_recipient(mode: str) -> None:
    connection = Connection()
    factory = Factory(connection)
    mailer = SmtpMailer(SmtpConfig(enabled=True, mode=mode), Credentials(), factory=factory)
    await mailer.send(message())
    context = factory.context if mode == "ssl" else connection.context
    assert context is not None
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    if mode == "starttls":
        assert connection.events.index("tls") < connection.events.index("login")
    assert connection.recipients == [["reader@example.invalid"]]
    assert connection.sent[0].get_content_type() == "text/plain"
    assert "Bcc" not in connection.sent[0]


async def test_message_id_is_stable_and_quit_failure_does_not_retry_success() -> None:
    connection = Connection(quit_error=True)
    mailer = SmtpMailer(SmtpConfig(enabled=True), Credentials(), factory=Factory(connection))
    await mailer.send(message())
    await mailer.send(message())
    assert connection.sent[0]["Message-ID"] == connection.sent[1]["Message-ID"]
    assert "close" in connection.events


@pytest.mark.parametrize(
    "error,code,permanent,uncertain",
    [
        (smtplib.SMTPServerDisconnected("synthetic data"), "delivery_unknown", False, True),
        (TimeoutError("synthetic data"), "delivery_unknown", False, True),
        (smtplib.SMTPDataError(451, b"synthetic data"), "mail_rejected", False, False),
        (smtplib.SMTPDataError(550, b"synthetic data"), "mail_rejected", True, False),
        (
            smtplib.SMTPRecipientsRefused({"reader@example.invalid": (550, b"no")}),
            "mail_rejected",
            True,
            False,
        ),
    ],
)
async def test_mail_failure_preserves_data_acceptance_uncertainty(
    error: Exception,
    code: str,
    permanent: bool,
    uncertain: bool,
) -> None:
    connection = Connection(send_error=error)
    mailer = SmtpMailer(SmtpConfig(enabled=True), Credentials(), factory=Factory(connection))
    with pytest.raises(ExternalError) as caught:
        await mailer.send(message())
    assert str(caught.value) == code
    assert caught.value.permanent is permanent
    assert caught.value.uncertain is uncertain


async def test_mail_header_injection_is_rejected_before_network() -> None:
    connection = Connection()
    mailer = SmtpMailer(SmtpConfig(enabled=True), Credentials(), factory=Factory(connection))
    with pytest.raises(ExternalError, match="^mail_rejected$"):
        await mailer.send(message("Subject\r\nBcc: extra@example.invalid"))
    assert not connection.events


async def test_overall_timeout_does_not_allow_a_later_send_after_slow_login() -> None:
    release = threading.Event()

    class SlowConnection(Connection):
        def login(self, user: str, password: str) -> tuple[int, bytes]:
            self.events.append("login")
            release.wait(timeout=1)
            return 235, b"ok"

    connection = SlowConnection()
    config = SmtpConfig(enabled=True).model_copy(update={"timeout_seconds": 0.02})
    mailer = SmtpMailer(config, Credentials(), factory=Factory(connection))
    try:
        with pytest.raises(ExternalError, match="^delivery_unknown$") as caught:
            await mailer.send(message())
        assert caught.value.uncertain
    finally:
        release.set()
    await asyncio.sleep(0.05)
    assert not connection.sent


async def test_cancellation_waits_for_in_progress_smtp_acceptance() -> None:
    entered = threading.Event()
    release = threading.Event()

    class ActiveSend(Connection):
        def send_message(
            self,
            msg: EmailMessage,
            from_addr: str,
            to_addrs: list[str],
        ) -> dict[str, tuple[int, bytes]]:
            entered.set()
            release.wait(timeout=1)
            return super().send_message(msg, from_addr, to_addrs)

    connection = ActiveSend()
    mailer = SmtpMailer(SmtpConfig(enabled=True), Credentials(), factory=Factory(connection))
    task = asyncio.create_task(mailer.send(message()))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
    finally:
        release.set()
    await task
    assert len(connection.sent) == 1
