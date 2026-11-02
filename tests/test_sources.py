from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from post_radar.config import SourceConfig
from post_radar.models import ExternalError, SourceState
from post_radar.sources import MAX_RESPONSE_BYTES, FeedSource

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Synthetic notices</title><link>https://example.invalid</link>
<description>Synthetic test feed</description><item><guid>notice-1</guid>
<title>Open workshop</title><description>&lt;p&gt;Registration is open.&lt;/p&gt;
&lt;script&gt;ignored()&lt;/script&gt;</description>
<link>https://example.invalid/workshop</link></item></channel></rss>"""


def source() -> SourceConfig:
    return SourceConfig(id="demo", url="https://feed.example.invalid/rss")


async def test_feed_parses_bytes_without_fetching_link_or_requiring_date() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=RSS, headers={"ETag": '"v1"'})

    result = await FeedSource(transport=httpx.MockTransport(handler)).fetch(source(), SourceState())
    assert len(calls) == 1
    assert result.etag == '"v1"'
    assert len(result.posts) == 1
    assert result.posts[0].external_id == "notice-1"
    assert result.posts[0].published_at is None
    assert "Registration is open." in result.posts[0].text
    assert "ignored" not in result.posts[0].text
    assert "<p>" not in result.posts[0].text


async def test_conditional_request_and_304_preserve_validators() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["If-None-Match"] == '"v1"'
        assert "If-Modified-Since" in request.headers
        return httpx.Response(304)

    state = SourceState(True, '"v1"', "Mon, 01 Jun 2026 10:00:00 GMT")
    result = await FeedSource(transport=httpx.MockTransport(handler)).fetch(source(), state)
    assert result.not_modified and not result.posts
    assert result.etag == state.etag and result.last_modified == state.last_modified


async def test_atom_fallback_id_is_stable_and_date_is_utc() -> None:
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Test</title>
    <entry><title>Workshop</title><summary>Sign up</summary>
    <updated>2026-06-01T10:00:00+02:00</updated></entry></feed>"""
    client = FeedSource(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=atom)))
    first = await client.fetch(source(), SourceState())
    second = await client.fetch(source(), SourceState())
    assert first.posts[0].external_id == second.posts[0].external_id
    assert first.posts[0].published_at.isoformat() == "2026-06-01T08:00:00+00:00"


@pytest.mark.parametrize(
    "content",
    [
        b"not XML",
        b"<rss><channel><item>",
        b'<!DOCTYPE rss [<!ENTITY test "value">]><rss version="2.0"><channel/></rss>',
        '<!DOCTYPE rss [<!ENTITY test "value">]><rss version="2.0"><channel/></rss>'.encode(
            "utf-16"
        ),
        b'<rss version="2.0"><channel><item><guid>empty</guid></item></channel></rss>',
    ],
)
async def test_malformed_feed_is_not_silently_acknowledged(content: bytes) -> None:
    client = FeedSource(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))
    )
    with pytest.raises(ExternalError, match="^invalid_feed$"):
        await client.fetch(source(), SourceState())


@pytest.mark.parametrize(
    "status,code,permanent",
    [
        (302, "unsafe_url", True),
        (401, "authentication_failed", True),
        (404, "request_rejected", True),
        (503, "provider_error", False),
        (429, "rate_limited", False),
    ],
)
async def test_source_http_failures_are_safe(status: int, code: str, permanent: bool) -> None:
    client = FeedSource(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status,
                text="synthetic private body",
                headers={"Retry-After": "123"},
            )
        )
    )
    with pytest.raises(ExternalError) as caught:
        await client.fetch(source(), SourceState())
    assert str(caught.value) == code
    assert caught.value.permanent is permanent
    if status == 429:
        assert caught.value.retry_after == 123


async def test_feed_response_size_is_bounded() -> None:
    client = FeedSource(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=b"x" * (MAX_RESPONSE_BYTES + 1),
            )
        )
    )
    with pytest.raises(ExternalError, match="^response_too_large$"):
        await client.fetch(source(), SourceState())


async def test_total_feed_timeout_stops_a_continuously_slow_stream(monkeypatch) -> None:
    class SlowStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(100):
                await asyncio.sleep(0.01)
                yield b"x"

        async def aclose(self) -> None:
            self.closed = True

    stream = SlowStream()
    monkeypatch.setattr("post_radar.sources.FEED_TIMEOUT_SECONDS", 0.04)
    source_client = FeedSource(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=stream),
        )
    )
    with pytest.raises(ExternalError, match="^network_error$"):
        await source_client.fetch(source(), SourceState())
    assert stream.closed
