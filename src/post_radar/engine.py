"""Independent workers for discovery, bounded API evaluation and durable mail."""

from __future__ import annotations

import asyncio
import logging
import math
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Protocol

from post_radar.config import AppConfig, RuleConfig, SourceConfig
from post_radar.models import (
    EvaluationOutcome,
    ExternalError,
    FetchResult,
    MailMessage,
    MatchResult,
    Post,
    SourceState,
)
from post_radar.security import redact_text
from post_radar.store import EvaluationClaim, Store

logger = logging.getLogger("post_radar")
_AUTH_ERRORS = {"authentication_failed", "missing_credential", "credential_store_unavailable"}
_BEFORE_SEND_ERRORS = {"missing_credential", "credential_store_unavailable", "api_disabled"}


class SourceAdapter(Protocol):
    async def fetch(self, source: SourceConfig, state: SourceState) -> FetchResult: ...


class Evaluator(Protocol):
    def estimate_cost(self, post: Post, rule: RuleConfig) -> int: ...

    async def evaluate(self, post: Post, rule: RuleConfig) -> EvaluationOutcome: ...


class Mailer(Protocol):
    async def send(self, message: MailMessage) -> None: ...


def keyword_match(post: Post, rule: RuleConfig) -> bool:
    def normalize(value: str) -> str:
        return unicodedata.normalize("NFKC", value).casefold()

    text = normalize(post.title + "\n" + post.text)
    return (
        not any(normalize(term) in text for term in rule.exclude)
        and (not rule.keywords_any or any(normalize(term) in text for term in rule.keywords_any))
        and all(normalize(term) in text for term in rule.keywords_all)
    )


def _safe_post(post: Post) -> Post:
    return post.model_copy(
        update={"title": redact_text(post.title)[:1000], "text": redact_text(post.text)[:50000]}
    )


def _checked_result(post: Post, result: MatchResult) -> MatchResult:
    text = post.title + "\n" + post.text
    if result.decision == "match" and (
        result.missing_information
        or not result.evidence
        or any(not evidence.strip() or evidence not in text for evidence in result.evidence)
    ):
        return MatchResult(
            decision="uncertain",
            reason="The supplied evidence could not confirm a match.",
            evidence=[],
            missing_information=["Valid source evidence is required."],
        )
    return result.model_copy(
        update={
            "reason": redact_text(result.reason),
            "evidence": [redact_text(value) for value in result.evidence],
            "missing_information": [redact_text(value) for value in result.missing_information],
        }
    )


class Engine:
    def __init__(
        self,
        config: AppConfig,
        store: Store,
        source: SourceAdapter,
        evaluator: Evaluator,
        mailer: Mailer,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.source = source
        self.evaluator = evaluator
        self.mailer = mailer
        self.clock = clock or (lambda: datetime.now(UTC))

    def _active_rule(self, rule_id: str) -> RuleConfig | None:
        return next((r for r in self.config.rules if r.id == rule_id and r.enabled), None)

    async def _collect_source(self, source: SourceConfig, *, force: bool = False) -> int:
        if not source.enabled or not await asyncio.to_thread(
            self.store.source_due,
            source.id,
            self.clock(),
            force=force,
        ):
            return 0
        state = await asyncio.to_thread(self.store.source_state, source.id)
        try:
            fetched = await self.source.fetch(source, state)
        except ExternalError as exc:
            await asyncio.to_thread(
                self.store.source_failure,
                source.id,
                now=self.clock(),
                retry_after=max(source.poll_interval_seconds, exc.retry_after),
                pause_code=exc.code if exc.permanent or exc.code in _AUTH_ERRORS else None,
            )
            logger.warning("source_failed source=%s code=%s", source.id, exc.code)
            return 0
        unique_posts: dict[str, Post] = {}
        for post in fetched.posts:
            unique_posts.setdefault(post.external_id, _safe_post(post))
        posts = tuple(unique_posts.values())
        result = FetchResult(posts, fetched.etag, fetched.last_modified, fetched.not_modified)
        matches = {
            post.external_id: tuple(
                rule
                for rule in self.config.rules
                if self.config.api.enabled
                and rule.enabled
                and source.id in rule.source_ids
                and keyword_match(post, rule)
            )
            for post in posts
        }
        return await asyncio.to_thread(
            self.store.commit_fetch,
            source,
            result,
            matches,
            now=self.clock(),
            retention_days=self.config.retention_days,
        )

    async def collect_once(self, source_id: str | None = None) -> int:
        sources = [source for source in self.config.sources if source_id in (None, source.id)]
        if source_id is not None and not sources:
            raise ValueError("unknown_source")
        return sum(
            await asyncio.gather(*(self._collect_source(source, force=True) for source in sources))
        )

    def _messages(self, claim: EvaluationClaim, result: MatchResult) -> tuple[MailMessage, ...]:
        if result.decision != "match" or not self.config.smtp.enabled:
            return ()
        active = self._active_rule(claim.rule.id)
        if active is None:
            return ()
        title = " ".join(claim.post.title.split())[:160]
        body = (
            f"Subscription: {claim.rule.id}\nTitle: {title}\n\n"
            f"Why it matched:\n{result.reason}\n\nEvidence:\n"
            + "\n".join(f"- {item}" for item in result.evidence)
        )
        if claim.post.url:
            body += "\n\nSource: " + claim.post.url
        return tuple(
            MailMessage(
                uuid.uuid5(uuid.NAMESPACE_URL, f"post-radar:{claim.id}:{recipient}").hex,
                recipient,
                f"[PostRadar: {claim.rule.id}] {title}",
                body,
            )
            for recipient in claim.rule.recipients
            if recipient in active.recipients
        )

    async def _evaluate_with_timeout(self, claim: EvaluationClaim) -> EvaluationOutcome:
        try:
            return await asyncio.wait_for(
                self.evaluator.evaluate(claim.post, claim.rule),
                timeout=self.config.api.timeout_seconds,
            )
        except TimeoutError:
            raise ExternalError("network_error", uncertain=True) from None

    async def evaluate_once(self) -> bool:
        if not self.config.api.enabled or await asyncio.to_thread(self.store.api_paused):
            return False
        claim = await asyncio.to_thread(
            self.store.claim_evaluation,
            now=self.clock(),
            lease_seconds=self.config.api.timeout_seconds + 30,
            max_attempts=self.config.max_attempts,
        )
        if claim is None:
            return False
        if self._active_rule(claim.rule.id) is None:
            await asyncio.to_thread(
                self.store.fail_evaluation,
                claim,
                now=self.clock(),
                code="rule_disabled",
                delay=0,
                permanent=True,
                max_attempts=self.config.max_attempts,
            )
            return True
        reservation: str | None = None
        try:
            amount = self.evaluator.estimate_cost(claim.post, claim.rule)
            reservation = await asyncio.to_thread(
                self.store.reserve_budget,
                claim,
                now=self.clock(),
                amount_micro=amount,
                limit_micro=math.floor(self.config.api.daily_budget_usd * 1_000_000),
            )
            if reservation is None:
                await asyncio.to_thread(
                    self.store.defer_evaluation,
                    claim,
                    now=self.clock(),
                    delay=60,
                )
                return False
            outcome = await self._evaluate_with_timeout(claim)
            await asyncio.to_thread(self.store.settle_budget, reservation, outcome.cost_micro)
            result = _checked_result(claim.post, outcome.result)
            await asyncio.to_thread(
                self.store.complete_evaluation,
                claim,
                result,
                self._messages(claim, result),
                now=self.clock(),
            )
            return True
        except ExternalError as exc:
            if reservation is not None:
                await asyncio.to_thread(
                    self.store.settle_budget,
                    reservation,
                    0 if exc.code in _BEFORE_SEND_ERRORS else None,
                )
            paused = exc.code in _AUTH_ERRORS
            if paused:
                await asyncio.to_thread(self.store.pause_api, exc.code)
                await asyncio.to_thread(
                    self.store.defer_evaluation,
                    claim,
                    now=self.clock(),
                    delay=60,
                )
            else:
                await asyncio.to_thread(
                    self.store.fail_evaluation,
                    claim,
                    now=self.clock(),
                    code=exc.code,
                    delay=max(exc.retry_after, min(3600, 30 * 2 ** (claim.attempt - 1))),
                    permanent=exc.permanent,
                    max_attempts=self.config.max_attempts,
                )
            logger.warning("evaluation_failed code=%s", exc.code)
            return True
        except BaseException:
            if reservation is not None:
                # Preserve uncertain charges even if shutdown interrupts a network call.
                await asyncio.shield(asyncio.to_thread(self.store.settle_budget, reservation, None))
            raise

    async def notify_once(self) -> bool:
        if (
            not self.config.api.enabled
            or not self.config.smtp.enabled
            or await asyncio.to_thread(self.store.mail_paused)
        ):
            return False
        claim = await asyncio.to_thread(
            self.store.claim_delivery,
            now=self.clock(),
            lease_seconds=self.config.smtp.timeout_seconds * 8 + 60,
            max_attempts=self.config.max_attempts,
        )
        if claim is None:
            return False
        rule = self._active_rule(claim.rule_id)
        if rule is None or claim.message.recipient not in rule.recipients:
            await asyncio.to_thread(
                self.store.finish_delivery,
                claim,
                now=self.clock(),
                state="failed",
                code="recipient_or_rule_disabled",
            )
            return True
        try:
            await self.mailer.send(claim.message)
        except ExternalError as exc:
            if exc.code in _AUTH_ERRORS:
                await asyncio.to_thread(self.store.pause_mail, exc.code)
                state = "retry" if claim.attempt < self.config.max_attempts else "failed"
            elif exc.uncertain or exc.code == "delivery_unknown":
                state = "unknown"
            elif exc.permanent or claim.attempt >= self.config.max_attempts:
                state = "failed"
            else:
                state = "retry"
            await asyncio.to_thread(
                self.store.finish_delivery,
                claim,
                now=self.clock(),
                state=state,
                code=exc.code,
                delay=(
                    self.config.smtp.timeout_seconds * 8 + 60
                    if state == "unknown"
                    else max(exc.retry_after, min(3600, 30 * 2 ** (claim.attempt - 1)))
                ),
            )
            logger.warning("mail_failed code=%s", exc.code)
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError from None
            return True
        except BaseException:
            await asyncio.shield(
                asyncio.to_thread(
                    self.store.finish_delivery,
                    claim,
                    now=self.clock(),
                    state="unknown",
                    code="delivery_interrupted",
                    delay=self.config.smtp.timeout_seconds * 8 + 60,
                )
            )
            raise
        await asyncio.to_thread(
            self.store.finish_delivery,
            claim,
            now=self.clock(),
            state="accepted",
        )
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        return True

    async def resume_api(self) -> None:
        await asyncio.to_thread(self.store.resume_api)

    async def _repeat(
        self,
        operation: Callable[[], Awaitable[int | bool]],
        stop: asyncio.Event,
        *,
        interval: float = 1,
    ) -> None:
        while not stop.is_set():
            await operation()
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)

    async def _cleanup(self) -> int:
        return await asyncio.to_thread(self.store.cleanup, now=self.clock())

    async def run(self, stop: asyncio.Event) -> None:
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(self._repeat(self.evaluate_once, stop)),
                group.create_task(self._repeat(self.notify_once, stop)),
                group.create_task(self._repeat(self._cleanup, stop, interval=60)),
            ]
            for source in self.config.sources:

                async def collect(config: SourceConfig = source) -> int:
                    return await self._collect_source(config)

                tasks.append(group.create_task(self._repeat(collect, stop)))
            await stop.wait()
            for task in tasks:
                task.cancel()
