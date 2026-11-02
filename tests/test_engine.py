from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from post_radar.config import ApiConfig, AppConfig, RuleConfig, SmtpConfig, SourceConfig
from post_radar.engine import Engine, keyword_match
from post_radar.models import (
    EvaluationOutcome,
    ExternalError,
    FetchResult,
    MailMessage,
    MatchResult,
    Post,
    SourceState,
)
from post_radar.store import Store


@dataclass
class Clock:
    value: datetime = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def configuration(*, enabled: bool = True, backfill: bool = True, budget: float = 1) -> AppConfig:
    return AppConfig(
        api=ApiConfig(
            enabled=enabled,
            model="synthetic-model",
            daily_budget_usd=budget,
            input_usd_per_million=1,
            output_usd_per_million=2,
        ),
        smtp=SmtpConfig(enabled=True),
        sources=(
            SourceConfig(id="feed", url="https://example.invalid/feed.xml", backfill=backfill),
        ),
        rules=(
            RuleConfig(
                id="reading",
                source_ids=("feed",),
                keywords_any=("reading",),
                exclude=("cancelled",),
                requirements="A free reading group.",
                recipients=("reader@example.invalid",),
            ),
        ),
    )


def post(identifier: str = "one", text: str = "A free reading group.") -> Post:
    return Post(source_id="feed", external_id=identifier, title="Announcement", text=text)


class FakeSource:
    def __init__(self, *responses: FetchResult | ExternalError) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def fetch(self, source: SourceConfig, state: SourceState) -> FetchResult:
        self.calls += 1
        response = self.responses.pop(0) if self.responses else FetchResult((), not_modified=True)
        if isinstance(response, ExternalError):
            raise response
        return response


class FakeEvaluator:
    def __init__(self, *, failure: ExternalError | None = None, decision: str = "match") -> None:
        self.failure = failure
        self.decision = decision
        self.calls = 0
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    def estimate_cost(self, post: Post, rule: RuleConfig) -> int:
        return 100

    async def evaluate(self, post: Post, rule: RuleConfig) -> EvaluationOutcome:
        self.calls += 1
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.failure is not None:
            raise self.failure
        result = MatchResult.model_validate(
            {
                "decision": self.decision,
                "reason": "The announcement describes a reading group.",
                "evidence": ["reading group"],
                "missing_information": [],
            }
        )
        return EvaluationOutcome(result, 25)


class FakeMailer:
    def __init__(self, failure: ExternalError | None = None) -> None:
        self.messages: list[MailMessage] = []
        self.failure = failure
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def send(self, message: MailMessage) -> None:
        self.messages.append(message)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.failure is not None:
            raise self.failure


def engine(
    tmp_path: Path,
    *,
    config: AppConfig | None = None,
    source: FakeSource | None = None,
    evaluator: FakeEvaluator | None = None,
    mailer: FakeMailer | None = None,
    clock: Clock | None = None,
) -> tuple[Engine, Store, FakeEvaluator, FakeMailer, Clock]:
    active_clock = clock or Clock()
    store = Store(tmp_path / "state.sqlite3")
    active_evaluator, active_mailer = evaluator or FakeEvaluator(), mailer or FakeMailer()
    runner = Engine(
        config or configuration(),
        store,
        source or FakeSource(FetchResult((post(),))),
        active_evaluator,
        active_mailer,
        clock=active_clock,
    )
    return runner, store, active_evaluator, active_mailer, active_clock


def test_keyword_normalization_all_any_and_exclusions() -> None:
    rule = configuration().rules[0].model_copy(update={"keywords_all": ("free",)})
    assert keyword_match(post(text="ＦＲＥＥ READING group"), rule)
    assert not keyword_match(post(text="Reading group requires payment"), rule)
    assert not keyword_match(post(text="Free reading group cancelled"), rule)


async def test_end_to_end_has_one_email_per_matching_rule_recipient(tmp_path: Path) -> None:
    runner, store, evaluator, mailer, _ = engine(tmp_path)
    assert await runner.collect_once() == 1
    assert await runner.evaluate_once()
    assert await runner.notify_once()
    assert not await runner.evaluate_once()
    assert not await runner.notify_once()
    assert evaluator.calls == 1 and len(mailer.messages) == 1
    assert mailer.messages[0].recipient == "reader@example.invalid"
    assert store.status()["mail_accepted"] == 1


async def test_baseline_and_explicit_manual_second_fetch(tmp_path: Path) -> None:
    feed = FakeSource(
        FetchResult((post("old"),)),
        FetchResult((post("old"), post("new"), post("excluded", "reading group cancelled"))),
    )
    runner, store, _, _, _ = engine(tmp_path, config=configuration(backfill=False), source=feed)
    assert await runner.collect_once() == 0
    assert await runner.collect_once() == 1
    assert store.status()["identities"] == 3
    assert store.status()["evaluation_pending"] == 1


async def test_api_disabled_collects_only_metadata_and_never_calls_external_workers(
    tmp_path: Path,
) -> None:
    runner, store, evaluator, mailer, _ = engine(tmp_path, config=configuration(enabled=False))
    assert await runner.collect_once() == 0
    assert not await runner.evaluate_once()
    assert not await runner.notify_once()
    assert store.status()["identities"] == 1
    assert evaluator.calls == 0 and not mailer.messages


async def test_api_disabled_also_blocks_previously_queued_mail(tmp_path: Path) -> None:
    runner, store, evaluator, mailer, clock = engine(tmp_path)
    await runner.collect_once()
    await runner.evaluate_once()
    disabled = Engine(
        configuration(enabled=False), store, FakeSource(), evaluator, mailer, clock=clock
    )
    assert not await disabled.notify_once()
    assert not mailer.messages and store.status()["mail_pending"] == 1


async def test_budget_exhaustion_defers_without_burning_attempts(tmp_path: Path) -> None:
    runner, store, evaluator, _, clock = engine(tmp_path, config=configuration(budget=0.00005))
    await runner.collect_once()
    assert not await runner.evaluate_once()
    assert evaluator.calls == 0 and store.budget_spent(clock()) == 0
    clock.advance(61)
    work = store.claim_evaluation(now=clock(), lease_seconds=60, max_attempts=3)
    assert work is not None and work.attempt == 1


async def test_auth_failure_pauses_until_explicit_resume_and_missing_key_releases_budget(
    tmp_path: Path,
) -> None:
    evaluator = FakeEvaluator(failure=ExternalError("missing_credential", permanent=True))
    runner, store, _, _, clock = engine(tmp_path, evaluator=evaluator)
    await runner.collect_once()
    assert await runner.evaluate_once()
    assert store.api_paused() and store.budget_spent(clock()) == 0
    clock.advance(1000)
    assert not await runner.evaluate_once() and evaluator.calls == 1
    await runner.resume_api()
    evaluator.failure = None
    assert await runner.evaluate_once() and evaluator.calls == 2


async def test_rate_limit_honors_retry_after_and_keeps_unknown_cost(tmp_path: Path) -> None:
    evaluator = FakeEvaluator(failure=ExternalError("rate_limited", retry_after=180))
    runner, store, _, _, clock = engine(tmp_path, evaluator=evaluator)
    await runner.collect_once()
    assert await runner.evaluate_once()
    assert store.budget_spent(clock()) == 100
    clock.advance(179)
    assert not await runner.evaluate_once()
    evaluator.failure = None
    clock.advance(1)
    assert await runner.evaluate_once()
    assert store.budget_spent(clock()) == 125


async def test_invalid_output_retries_stop_at_configured_limit(tmp_path: Path) -> None:
    evaluator = FakeEvaluator(failure=ExternalError("invalid_output"))
    runner, store, _, mailer, clock = engine(tmp_path, evaluator=evaluator)
    await runner.collect_once()
    for _ in range(3):
        assert await runner.evaluate_once()
        clock.advance(3601)
    assert not await runner.evaluate_once()
    assert store.status()["evaluation_failed"] == 1
    assert evaluator.calls == 3 and not mailer.messages


@pytest.mark.parametrize("decision", ["no_match", "uncertain"])
async def test_non_matches_never_enqueue_mail(tmp_path: Path, decision: str) -> None:
    runner, store, _, mailer, _ = engine(tmp_path, evaluator=FakeEvaluator(decision=decision))
    await runner.collect_once()
    await runner.evaluate_once()
    assert not await runner.notify_once()
    assert not mailer.messages and "mail_pending" not in store.status()


async def test_fabricated_evidence_cannot_create_mail(tmp_path: Path) -> None:
    runner, store, _, mailer, _ = engine(
        tmp_path,
        source=FakeSource(FetchResult((post(text="A reading list."),))),
    )
    await runner.collect_once()
    await runner.evaluate_once()
    assert not await runner.notify_once()
    assert not mailer.messages and store.status()["evaluation_completed"] == 1


async def test_removed_recipient_blocks_queued_delivery(tmp_path: Path) -> None:
    runner, store, evaluator, mailer, clock = engine(tmp_path)
    await runner.collect_once()
    await runner.evaluate_once()
    changed_rule = runner.config.rules[0].model_copy(
        update={"recipients": ("replacement@example.invalid",)},
    )
    changed = Engine(
        runner.config.model_copy(update={"rules": (changed_rule,)}),
        store,
        FakeSource(),
        evaluator,
        mailer,
        clock=clock,
    )
    assert await changed.notify_once()
    assert store.status()["mail_failed"] == 1 and not mailer.messages


async def test_collection_runs_while_model_is_waiting(tmp_path: Path) -> None:
    evaluator = FakeEvaluator()
    evaluator.release = asyncio.Event()
    feed = FakeSource(FetchResult((post("one"),)), FetchResult((post("two"),)))
    runner, store, _, _, _ = engine(tmp_path, source=feed, evaluator=evaluator)
    await runner.collect_once()
    task = asyncio.create_task(runner.evaluate_once())
    try:
        await asyncio.wait_for(evaluator.entered.wait(), timeout=2)
        assert await asyncio.wait_for(runner.collect_once(), timeout=2) == 1
        assert store.status()["evaluation_pending"] == 1
    finally:
        evaluator.release.set()
        await task


async def test_unknown_smtp_result_needs_manual_retry_and_reuses_identity(tmp_path: Path) -> None:
    mailer = FakeMailer(ExternalError("delivery_unknown", uncertain=True))
    runner, store, _, _, clock = engine(tmp_path, mailer=mailer)
    await runner.collect_once()
    await runner.evaluate_once()
    await runner.notify_once()
    assert store.status()["mail_unknown"] == 1
    assert not await runner.notify_once() and len(mailer.messages) == 1
    identity = mailer.messages[0].delivery_id
    assert not store.retry_unknown(identity, now=clock())
    clock.advance(runner.config.smtp.timeout_seconds * 8 + 61)
    assert store.retry_unknown(identity, now=clock())
    mailer.failure = None
    await runner.notify_once()
    assert len(mailer.messages) == 2 and mailer.messages[1].delivery_id == identity


async def test_cancelled_smtp_attempt_becomes_unknown(tmp_path: Path) -> None:
    mailer = FakeMailer()
    mailer.release = asyncio.Event()
    runner, store, _, _, _ = engine(tmp_path, mailer=mailer)
    await runner.collect_once()
    await runner.evaluate_once()
    task = asyncio.create_task(runner.notify_once())
    await asyncio.wait_for(mailer.entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.status()["mail_unknown"] == 1
    assert not await runner.notify_once()


async def test_source_authentication_failure_does_not_loop_or_log_remote_text(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    feed = FakeSource(ExternalError("authentication_failed"))
    runner, store, _, _, _ = engine(tmp_path, source=feed)
    await runner.collect_once()
    await runner.collect_once()
    assert feed.calls == 1 and store.status()["sources_paused"] == 1
    assert "authentication_failed" in caplog.text


async def test_total_api_timeout_retains_budget_and_schedules_retry(tmp_path: Path) -> None:
    evaluator = FakeEvaluator()
    evaluator.release = asyncio.Event()
    config = configuration()
    # A tiny test-only deadline keeps this regression offline and fast.
    config = config.model_copy(
        update={
            "api": config.api.model_copy(update={"timeout_seconds": 0.01}),
        }
    )
    runner, store, _, _, clock = engine(tmp_path, config=config, evaluator=evaluator)
    await runner.collect_once()
    assert await runner.evaluate_once()
    assert store.budget_spent(clock()) == 100
    assert store.status()["evaluation_retry"] == 1
    assert not await runner.evaluate_once()


async def test_shutdown_after_known_smtp_acceptance_records_acceptance(tmp_path: Path) -> None:
    class CompletionAwareMailer(FakeMailer):
        async def send(self, message: MailMessage) -> None:
            self.messages.append(message)
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Models an adapter that waited for an in-flight SMTP thread to finish.
                return

    mailer = CompletionAwareMailer()
    runner, store, _, _, _ = engine(tmp_path, mailer=mailer)
    await runner.collect_once()
    await runner.evaluate_once()
    task = asyncio.create_task(runner.notify_once())
    await asyncio.wait_for(mailer.entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.status()["mail_accepted"] == 1
    assert not store.unknown_deliveries()


async def test_cancelled_run_exits_without_stop_event_after_smtp_completion(tmp_path: Path) -> None:
    class CompletionAwareMailer(FakeMailer):
        async def send(self, message: MailMessage) -> None:
            self.messages.append(message)
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return

    mailer = CompletionAwareMailer()
    runner, store, _, _, _ = engine(tmp_path, mailer=mailer)
    await runner.collect_once()
    await runner.evaluate_once()
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(stop))
    await asyncio.wait_for(mailer.entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert not stop.is_set()
    assert store.status()["mail_accepted"] == 1


async def test_redaction_expansion_preserves_snapshot_model_limits(tmp_path: Path) -> None:
    item = post(text="reading group " + "token=x " * 6200).model_copy(
        update={"title": "token=x " * 125},
    )
    runner, store, _, _, clock = engine(tmp_path, source=FakeSource(FetchResult((item,))))
    assert await runner.collect_once() == 1
    work = store.claim_evaluation(now=clock(), lease_seconds=60, max_attempts=3)
    assert work is not None
    assert len(work.post.title) <= 1000 and len(work.post.text) <= 50000
    assert "token=x" not in work.post.text


async def test_smtp_missing_credential_pauses_all_jobs_until_explicit_resume(
    tmp_path: Path,
) -> None:
    mailer = FakeMailer(ExternalError("missing_credential", permanent=True))
    config = configuration()
    config = config.model_copy(
        update={
            "rules": (
                config.rules[0].model_copy(
                    update={
                        "recipients": ("reader@example.invalid", "friend@example.invalid"),
                    }
                ),
            )
        }
    )
    runner, store, _, _, _ = engine(tmp_path, config=config, mailer=mailer)
    await runner.collect_once()
    await runner.evaluate_once()
    assert await runner.notify_once()
    assert store.status()["mail_paused"] == 1
    assert Store(store.path).mail_paused()
    assert store.status()["mail_pending"] == 1
    assert store.status()["mail_retry"] == 1
    assert not await runner.notify_once() and len(mailer.messages) == 1
    store.resume_mail()
    mailer.failure = None
    assert await runner.notify_once()
    assert await runner.notify_once()
    assert store.status()["mail_accepted"] == 2
    assert not store.mail_paused()


async def test_repeated_manual_smtp_resume_still_respects_attempt_limit(tmp_path: Path) -> None:
    mailer = FakeMailer(ExternalError("authentication_failed", permanent=True))
    runner, store, _, _, _ = engine(tmp_path, mailer=mailer)
    await runner.collect_once()
    await runner.evaluate_once()
    for _ in range(runner.config.max_attempts):
        assert await runner.notify_once()
        assert store.mail_paused()
        assert not await runner.notify_once()
        store.resume_mail()
    assert not await runner.notify_once()
    assert store.status()["mail_failed"] == 1
    assert len(mailer.messages) == runner.config.max_attempts
