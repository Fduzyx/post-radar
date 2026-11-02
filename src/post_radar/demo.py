"""Offline, synthetic demonstration. No network or credential store is involved."""

from __future__ import annotations

from pathlib import Path

from post_radar.config import ApiConfig, AppConfig, RuleConfig, SmtpConfig, SourceConfig
from post_radar.engine import Engine
from post_radar.models import (
    EvaluationOutcome,
    FetchResult,
    MailMessage,
    MatchResult,
    Post,
    SourceState,
)
from post_radar.store import Store


class DemoSource:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, source: SourceConfig, state: SourceState) -> FetchResult:
        self.calls += 1
        old = Post(
            source_id=source.id,
            external_id="old",
            title="Previous workshop",
            text="An older online workshop with registration.",
        )
        if self.calls == 1:
            return FetchResult(posts=(old,))
        return FetchResult(
            posts=(
                old,
                Post(
                    source_id=source.id,
                    external_id="new",
                    title="Synthetic online workshop",
                    text="Online attendance is available. Register using the event form.",
                    url="https://events.example.invalid/workshop",
                ),
                Post(
                    source_id=source.id,
                    external_id="unknown",
                    title="Synthetic workshop notice",
                    text="More details will be announced later.",
                ),
                Post(
                    source_id=source.id,
                    external_id="excluded",
                    title="Cancelled workshop",
                    text="The event has been cancelled.",
                ),
            )
        )


class DemoEvaluator:
    def estimate_cost(self, post: Post, rule: RuleConfig) -> int:
        return 1

    async def evaluate(self, post: Post, rule: RuleConfig) -> EvaluationOutcome:
        matches = "Online attendance is available." in post.text
        return EvaluationOutcome(
            result=MatchResult(
                decision="match" if matches else "uncertain",
                reason="Synthetic demonstration result; no AI service was contacted.",
                evidence=["Online attendance is available."] if matches else [],
                missing_information=[] if matches else ["Attendance and registration details"],
            ),
            cost_micro=0,
        )


class PreviewMailer:
    def __init__(self) -> None:
        self.messages: list[MailMessage] = []

    async def send(self, message: MailMessage) -> None:
        self.messages.append(message)


async def run_demo(directory: Path) -> tuple[dict[str, int], list[MailMessage]]:
    config = AppConfig(
        state_dir=directory,
        api=ApiConfig(
            enabled=True,
            model="synthetic-model",
            daily_budget_usd=1,
            input_usd_per_million=1,
            output_usd_per_million=1,
        ),
        smtp=SmtpConfig(enabled=True),
        sources=(SourceConfig(id="demo", url="https://feed.example.invalid/events.xml"),),
        rules=(
            RuleConfig(
                id="online-events",
                source_ids=("demo",),
                keywords_any=("workshop",),
                exclude=("cancelled",),
                requirements="Online attendance and registration must be explicit.",
                recipients=("reader@example.invalid",),
            ),
        ),
    )
    store = Store(directory / "post-radar.sqlite3")
    mailer = PreviewMailer()
    engine = Engine(config, store, DemoSource(), DemoEvaluator(), mailer)
    await engine.collect_once()
    await engine.collect_once()
    for _ in range(10):
        if not await engine.evaluate_once():
            break
    for _ in range(10):
        if not await engine.notify_once():
            break
    # Re-reading overlapping entries must not create additional delivery jobs.
    await engine.collect_once()
    return store.status(), mailer.messages
