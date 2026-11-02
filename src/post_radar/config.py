"""Strict non-secret configuration. Error messages never echo user input."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from post_radar.models import StrictModel
from post_radar.security import safe_https_url, validate_email

IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


class SourceConfig(StrictModel):
    id: str = Field(pattern=IDENTIFIER)
    url: str
    enabled: bool = True
    poll_interval_seconds: int = Field(default=120, ge=30, le=86400)
    backfill: bool = False

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return safe_https_url(value)


class RuleConfig(StrictModel):
    id: str = Field(pattern=IDENTIFIER)
    source_ids: tuple[str, ...]
    keywords_any: tuple[str, ...] = ()
    keywords_all: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    requirements: str = Field(min_length=1, max_length=8000)
    recipients: tuple[str, ...] = Field(repr=False)
    enabled: bool = True

    @field_validator("recipients")
    @classmethod
    def valid_recipients(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("recipient_required")
        normalized = tuple(validate_email(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate_recipient")
        return normalized

    @field_validator("keywords_any", "keywords_all", "exclude")
    @classmethod
    def valid_keywords(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 100 or any(not value.strip() or len(value) > 100 for value in values):
            raise ValueError("invalid_keywords")
        return tuple(value.strip() for value in values)

    @model_validator(mode="after")
    def has_keywords(self) -> RuleConfig:
        if not self.keywords_any and not self.keywords_all:
            raise ValueError("keyword_required")
        if not self.source_ids:
            raise ValueError("source_required")
        return self

    @property
    def revision(self) -> str:
        payload = json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()


class ApiConfig(StrictModel):
    enabled: bool = False
    protocol: Literal["responses", "chat_completions"] = "responses"
    base_url: str = "https://api.openai.com/v1"
    model: str = "configure-your-model"
    credential_name: Literal["api-key"] = "api-key"
    timeout_seconds: int = Field(default=60, ge=5, le=180)
    max_output_tokens: int = Field(default=1024, ge=128, le=4096)
    max_input_chars: int = Field(default=12000, ge=100, le=40000)
    daily_budget_usd: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    input_usd_per_million: float = Field(default=0, ge=0, le=1000, allow_inf_nan=False)
    output_usd_per_million: float = Field(default=0, ge=0, le=1000, allow_inf_nan=False)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        url = safe_https_url(value)
        if "?" in url:
            raise ValueError("api_query_forbidden")
        return url.rstrip("/")

    @field_validator("model")
    @classmethod
    def valid_model(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", value):
            raise ValueError("invalid_model")
        return value

    @model_validator(mode="after")
    def configured(self) -> ApiConfig:
        if self.enabled and (
            self.model == "configure-your-model"
            or self.daily_budget_usd <= 0
            or self.input_usd_per_million <= 0
            or self.output_usd_per_million <= 0
        ):
            raise ValueError("api_model_prices_and_budget_required")
        return self


class SmtpConfig(StrictModel):
    enabled: bool = False
    host: str = "smtp.example.invalid"
    port: int = Field(default=465, ge=1, le=65535)
    mode: Literal["ssl", "starttls"] = "ssl"
    username: str = Field(default="sender@example.invalid", repr=False)
    sender: str = Field(default="sender@example.invalid", repr=False)
    credential_name: Literal["smtp-password"] = "smtp-password"
    timeout_seconds: int = Field(default=60, ge=5, le=180)

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        safe_https_url("https://" + value)
        if not re.fullmatch(r"[A-Za-z0-9.-]+", value):
            raise ValueError("invalid_smtp_host")
        return value

    @field_validator("sender")
    @classmethod
    def valid_sender(cls, value: str) -> str:
        return validate_email(value)

    @field_validator("username")
    @classmethod
    def valid_username(cls, value: str) -> str:
        if not value or len(value) > 254 or any(ord(c) < 32 for c in value):
            raise ValueError("invalid_smtp_username")
        return value


class AppConfig(StrictModel):
    state_dir: Path = Path("state")
    retention_days: int = Field(default=7, ge=1, le=90)
    max_attempts: int = Field(default=3, ge=1, le=10)
    api: ApiConfig = ApiConfig()
    smtp: SmtpConfig = SmtpConfig()
    sources: tuple[SourceConfig, ...] = ()
    rules: tuple[RuleConfig, ...] = ()

    @model_validator(mode="after")
    def references(self) -> AppConfig:
        source_ids = {source.id for source in self.sources}
        if len(source_ids) != len(self.sources) or (
            len({r.id for r in self.rules}) != len(self.rules)
        ):
            raise ValueError("duplicate_id")
        if any(not set(rule.source_ids) <= source_ids for rule in self.rules):
            raise ValueError("unknown_source")
        return self


def load_config(path: Path) -> AppConfig:
    try:
        if path.stat().st_size > 128 * 1024:
            raise ValueError("too_large")
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        config = AppConfig.model_validate(data)
        state_dir = config.state_dir
        if not state_dir.is_absolute():
            state_dir = path.resolve().parent / state_dir
        return config.model_copy(update={"state_dir": state_dir.resolve()})
    except (OSError, ValueError, TypeError):
        raise ValueError("invalid_configuration") from None
