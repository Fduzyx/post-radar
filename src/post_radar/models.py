"""Typed, provider-neutral messages. Content is never an instruction to the application."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Post(StrictModel):
    source_id: str
    external_id: str
    title: str = Field(max_length=1000)
    text: str = Field(max_length=50000, repr=False)
    url: str = ""
    published_at: datetime | None = None


class MatchResult(StrictModel):
    decision: Literal["match", "no_match", "uncertain"]
    reason: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(max_length=10)
    missing_information: list[str] = Field(max_length=10)


@dataclass(frozen=True)
class SourceState:
    initialized: bool = False
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class FetchResult:
    posts: tuple[Post, ...]
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


@dataclass(frozen=True)
class EvaluationOutcome:
    result: MatchResult
    cost_micro: int | None = None


@dataclass(frozen=True)
class MailMessage:
    delivery_id: str
    recipient: str
    subject: str
    body: str


class ExternalError(Exception):
    """Only a local error code may escape an external boundary."""

    def __init__(
        self,
        code: str,
        *,
        retry_after: float = 60,
        permanent: bool = False,
        uncertain: bool = False,
    ) -> None:
        safe_codes = {
            "network_error",
            "rate_limited",
            "authentication_failed",
            "request_rejected",
            "response_too_large",
            "invalid_feed",
            "invalid_output",
            "provider_refusal",
            "unsafe_url",
            "mail_rejected",
            "delivery_unknown",
            "missing_credential",
            "credential_store_unavailable",
            "api_disabled",
            "budget_exhausted",
            "provider_error",
            "model_mismatch",
            "already_running",
        }
        self.code = code if code in safe_codes else "provider_error"
        self.retry_after = max(1.0, min(86400.0, retry_after))
        self.permanent = permanent
        self.uncertain = uncertain
        super().__init__(self.code)
