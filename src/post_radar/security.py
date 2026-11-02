"""Small validation boundaries; never put untrusted text into error messages."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, urlsplit


def safe_https_url(value: str) -> str:
    try:
        if len(value) > 4096 or any(ord(char) < 33 for char in value) or "\\" in value:
            raise ValueError
        parts = urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        if (
            parts.scheme != "https"
            or not host
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or parts.port not in (None, 443)
        ):
            raise ValueError
        if host in {"localhost", "localhost.localdomain"} or host.endswith(
            (".localhost", ".local", ".internal")
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if all(re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", part) for part in host.split(".")):
                raise ValueError from None
            if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or "." not in host:
                raise ValueError from None
        else:
            if not address.is_global:
                raise ValueError
        if any(
            re.search(r"password|token|secret|authorization|cookie|credential|api.?key", key, re.I)
            for key, _ in parse_qsl(parts.query)
        ):
            raise ValueError
        if re.search(r"%(?:0[ad]|5c|40)", value, re.I):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("unsafe_url") from None
    return value


def validate_email(value: str) -> str:
    if len(value) > 254 or not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}",
        value,
    ):
        raise ValueError("invalid_email")
    return value


def redact_text(value: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        # Redaction must not grow bounded fields and make queued items unparseable.
        return "[redacted]" if len(match.group()) >= 10 else "*"

    value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", replacement, value)
    return re.sub(
        r"(?i)\b(password|token|secret|authorization|cookie|credential|api[_-]?key)"
        r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        replacement,
        value,
    )
