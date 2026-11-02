"""Bounded, read-only RSS/Atom collection. Feed links are never fetched."""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

import feedparser
import httpx

from post_radar.config import SourceConfig
from post_radar.models import ExternalError, FetchResult, Post, SourceState
from post_radar.security import safe_https_url

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
FEED_TIMEOUT_SECONDS = 30.0


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "li", "section"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li", "section"} and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str) -> str:
    parser = _PlainText()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts)
    return "".join(c for c in text if ord(c) >= 32 or c in "\n\t").strip()


def retry_after(value: str | None) -> float:
    if value is None:
        return 60.0
    try:
        seconds = float(value)
        if not 0 <= seconds <= 86400:
            return 60.0
        return max(1.0, seconds)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            return max(1.0, min(86400.0, (date - datetime.now(UTC)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 60.0


def _validator(value: str | None) -> str | None:
    if value is None or len(value) > 1024 or any(ord(c) < 32 for c in value):
        return None
    return value


def _post(source_id: str, entry: Any) -> Post:
    title = plain_text(str(entry.get("title", "")))
    contents = entry.get("content", [])
    raw = contents[0].get("value", "") if contents else entry.get("summary", "")
    text = plain_text(str(raw))
    if not title and not text:
        raise ValueError("empty_entry")
    link = str(entry.get("link", ""))
    try:
        link = safe_https_url(link) if link else ""
    except ValueError:
        link = ""
    timestamp = entry.get("published_parsed") or entry.get("updated_parsed")
    published = datetime.fromtimestamp(calendar.timegm(timestamp), UTC) if timestamp else None
    identity = str(entry.get("id", "")).strip() or link
    if not identity:
        canonical = "\0".join((title, text, str(entry.get("published", ""))))
        identity = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if len(identity) > 2048:
        raise ValueError("identity_too_large")
    return Post(
        source_id=source_id,
        external_id=identity,
        title=title,
        text=text,
        url=link,
        published_at=published,
    )


class FeedSource:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def fetch(self, source: SourceConfig, state: SourceState) -> FetchResult:
        headers = {
            "Accept": "application/atom+xml, application/rss+xml, application/xml",
            "Accept-Encoding": "identity",
        }
        if etag := _validator(state.etag):
            headers["If-None-Match"] = etag
        if modified := _validator(state.last_modified):
            headers["If-Modified-Since"] = modified
        try:
            async with (
                asyncio.timeout(FEED_TIMEOUT_SECONDS),
                httpx.AsyncClient(
                    transport=self._transport,
                    verify=True,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=30.0,
                ) as client,
                client.stream("GET", source.url, headers=headers) as response,
            ):
                if response.status_code == 304:
                    if not state.initialized:
                        raise ExternalError("invalid_feed")
                    return FetchResult(
                        (),
                        _validator(response.headers.get("etag")) or state.etag,
                        _validator(response.headers.get("last-modified")) or state.last_modified,
                        not_modified=True,
                    )
                if response.status_code == 429:
                    raise ExternalError(
                        "rate_limited", retry_after=retry_after(response.headers.get("retry-after"))
                    )
                if response.status_code in {401, 403}:
                    raise ExternalError("authentication_failed", permanent=True)
                if 300 <= response.status_code < 400:
                    raise ExternalError("unsafe_url", permanent=True)
                if response.status_code >= 500:
                    raise ExternalError("provider_error")
                if response.status_code != 200:
                    raise ExternalError("request_rejected", permanent=True)
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ExternalError("invalid_feed")
                chunks = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE_BYTES:
                        raise ExternalError("response_too_large")
                etag = _validator(response.headers.get("etag"))
                modified = _validator(response.headers.get("last-modified"))
        except ExternalError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TimeoutError):
            raise ExternalError("network_error") from None
        try:
            data = bytes(chunks)
            # NUL removal also detects declarations in UTF-16/UTF-32 XML encodings.
            if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", data.replace(b"\0", b""), re.IGNORECASE):
                raise ValueError("xml_declaration_forbidden")
            feed = feedparser.parse(data)
            if feed.get("bozo") or not feed.get("version") or len(feed.entries) > 5000:
                raise ValueError("invalid_feed")
            posts = tuple(_post(source.id, entry) for entry in feed.entries)
        except Exception:
            raise ExternalError("invalid_feed") from None
        return FetchResult(posts, etag, modified)
