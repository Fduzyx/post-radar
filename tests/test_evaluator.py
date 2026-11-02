from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest

from post_radar.config import ApiConfig, RuleConfig
from post_radar.evaluator import ApiEvaluator
from post_radar.models import ExternalError, Post


class Credentials:
    def get(self, name: str) -> str:
        return "synthetic-not-a-credential"


def config(**overrides: object) -> ApiConfig:
    data = dict(
        enabled=True,
        model="synthetic-model",
        daily_budget_usd=1,
        input_usd_per_million=2,
        output_usd_per_million=4,
    )
    data.update(overrides)
    return ApiConfig.model_validate(data)


def post(text: str = "Registration is open to everyone.") -> Post:
    return Post(source_id="demo", external_id="one", title="Workshop", text=text)


def rule() -> RuleConfig:
    return RuleConfig(
        id="workshops",
        source_ids=("demo",),
        keywords_any=("workshop",),
        requirements="Open registration to everyone",
        recipients=("reader@example.invalid",),
    )


def match(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "decision": "match",
        "reason": "Open registration is stated.",
        "evidence": ["Registration is open to everyone."],
        "missing_information": [],
    }
    data.update(overrides)
    return data


def response(result: dict[str, object], protocol: str = "responses") -> dict[str, object]:
    if protocol == "responses":
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(result)},
                    ],
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
async def test_structured_protocol_has_no_tools_and_validates_evidence(protocol: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "tools" not in payload
        if protocol == "responses":
            assert request.url.path == "/v1/responses"
            schema = payload["text"]["format"]["schema"]
            assert payload["store"] is False
            assert "untrusted" in payload["instructions"]
            content = json.loads(payload["input"])
            assert content["current_time_utc"].endswith("+00:00")
            assert "published_at" in content
        else:
            assert request.url.path == "/v1/chat/completions"
            schema = payload["response_format"]["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        return httpx.Response(200, json=response(match(), protocol))

    evaluator = ApiEvaluator(
        config(protocol=protocol),
        Credentials(),
        transport=httpx.MockTransport(
            handler,
        ),
    )
    outcome = await evaluator.evaluate(post(), rule())
    assert outcome.result.decision == "match"
    assert outcome.cost_micro == 280
    assert evaluator.estimate_cost(post(), rule()) > outcome.cost_micro


@pytest.mark.parametrize(
    "result",
    [
        match(evidence=[]),
        match(evidence=["invented quotation"]),
        match(missing_information=["Who may attend?"]),
        match(unexpected="value"),
    ],
)
async def test_unsupported_or_invalid_matches_are_rejected(result: dict[str, object]) -> None:
    evaluator = ApiEvaluator(
        config(),
        Credentials(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response(result)),
        ),
    )
    with pytest.raises(ExternalError, match="^invalid_output$") as caught:
        await evaluator.evaluate(post(), rule())
    assert caught.value.uncertain


async def test_truncated_post_cannot_be_accepted_as_match() -> None:
    evaluator = ApiEvaluator(
        config(max_input_chars=100),
        Credentials(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response(match())),
        ),
    )
    outcome = await evaluator.evaluate(
        post("Registration is open to everyone." + "x" * 200), rule()
    )
    assert outcome.result.decision == "uncertain"
    assert outcome.result.missing_information


async def test_unknown_usage_is_preserved_for_budget_accounting() -> None:
    body = response(match())
    body.pop("usage")
    evaluator = ApiEvaluator(
        config(),
        Credentials(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=body),
        ),
    )
    assert (await evaluator.evaluate(post(), rule())).cost_micro is None


@pytest.mark.parametrize(
    "status,code,permanent,uncertain",
    [
        (401, "authentication_failed", True, False),
        (429, "rate_limited", False, False),
        (503, "provider_error", False, True),
        (302, "unsafe_url", True, False),
    ],
)
async def test_safe_api_failures(
    status: int,
    code: str,
    permanent: bool,
    uncertain: bool,
) -> None:
    evaluator = ApiEvaluator(
        config(),
        Credentials(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status, text="synthetic sensitive error", headers={"Retry-After": "99"}
            ),
        ),
    )
    with pytest.raises(ExternalError) as caught:
        await evaluator.evaluate(post(), rule())
    assert str(caught.value) == code
    assert caught.value.permanent is permanent
    assert caught.value.uncertain is uncertain


async def test_timeout_has_safe_text_and_potential_charge() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic sensitive error", request=request)

    evaluator = ApiEvaluator(config(), Credentials(), transport=httpx.MockTransport(handler))
    with pytest.raises(ExternalError, match="^network_error$") as caught:
        await evaluator.evaluate(post(), rule())
    assert caught.value.uncertain


@pytest.mark.parametrize(
    "body,code",
    [
        ({"status": "incomplete", "output": []}, "invalid_output"),
        (
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "refusal", "refusal": "synthetic refusal"},
                        ],
                    }
                ],
            },
            "provider_refusal",
        ),
    ],
)
async def test_incomplete_and_refusal_outputs_do_not_match(body: dict, code: str) -> None:
    evaluator = ApiEvaluator(
        config(),
        Credentials(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=body),
        ),
    )
    with pytest.raises(ExternalError, match=f"^{code}$"):
        await evaluator.evaluate(post(), rule())


async def test_publication_date_is_supplied_for_relative_time_requirements() -> None:
    dated = post().model_copy(update={"published_at": datetime(2026, 6, 1, tzinfo=UTC)})

    def handler(request: httpx.Request) -> httpx.Response:
        content = json.loads(json.loads(request.content)["input"])
        assert content["published_at"] == "2026-06-01T00:00:00+00:00"
        return httpx.Response(200, json=response(match()))

    evaluator = ApiEvaluator(config(), Credentials(), transport=httpx.MockTransport(handler))
    await evaluator.evaluate(dated, rule())


@pytest.mark.parametrize("escaped", [False, True])
async def test_credential_echo_never_leaves_evaluator(escaped: bool) -> None:
    result = match(reason=Credentials().get("api-key"))
    body = response(result)
    raw = json.dumps(body)
    if escaped:
        raw = raw.replace("synthetic-not-a-credential", r"\\u0073ynthetic-not-a-credential")
    evaluator = ApiEvaluator(
        config(),
        Credentials(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=raw),
        ),
    )
    with pytest.raises(ExternalError, match="^invalid_output$"):
        await evaluator.evaluate(post(), rule())


async def test_invalid_header_credential_is_rejected_without_request() -> None:
    class InvalidCredentials:
        def get(self, name: str) -> str:
            return "synthetic\r\ninvalid"

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not contact provider")

    evaluator = ApiEvaluator(config(), InvalidCredentials(), transport=httpx.MockTransport(handler))
    with pytest.raises(ExternalError, match="^authentication_failed$"):
        await evaluator.evaluate(post(), rule())


async def test_evidence_from_unsent_tail_is_rejected() -> None:
    text = "x" * 120 + "Registration is open to everyone."
    evaluator = ApiEvaluator(
        config(max_input_chars=100),
        Credentials(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response(match())),
        ),
    )
    with pytest.raises(ExternalError, match="^invalid_output$"):
        await evaluator.evaluate(post(text), rule())


async def test_total_api_timeout_stops_a_slow_stream_and_keeps_unknown_charge() -> None:
    class SlowStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(100):
                await asyncio.sleep(0.01)
                yield b"x"

        async def aclose(self) -> None:
            self.closed = True

    stream = SlowStream()
    evaluator = ApiEvaluator(
        config().model_copy(update={"timeout_seconds": 0.04}),
        Credentials(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    )
    with pytest.raises(ExternalError, match="^network_error$") as caught:
        await evaluator.evaluate(post(), rule())
    assert caught.value.uncertain
    assert stream.closed
