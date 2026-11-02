from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from post_radar.config import RuleConfig, SourceConfig
from post_radar.models import FetchResult, MailMessage, MatchResult, Post
from post_radar.store import EvaluationClaim, Store

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def source(identifier: str = "feed", *, backfill: bool = True) -> SourceConfig:
    return SourceConfig(id=identifier, url="https://example.invalid/feed.xml", backfill=backfill)


def rule(identifier: str = "reading", *, requirement: str = "Find a reading group.") -> RuleConfig:
    return RuleConfig(
        id=identifier,
        source_ids=("feed", "other"),
        keywords_any=("reading",),
        requirements=requirement,
        recipients=("reader@example.invalid", "friend@example.invalid"),
    )


def post(identifier: str = "one", *, source_id: str = "feed") -> Post:
    return Post(source_id=source_id, external_id=identifier, title="Reading", text="Reading group")


def ingest(store: Store, count: int = 1) -> None:
    posts = tuple(post(str(i)) for i in range(count))
    store.commit_fetch(
        source(),
        FetchResult(posts, etag="version-one"),
        {p.external_id: (rule(),) for p in posts},
        now=NOW,
        retention_days=7,
    )


def claim(store: Store, *, now: datetime = NOW, max_attempts: int = 3) -> EvaluationClaim:
    result = store.claim_evaluation(now=now, lease_seconds=60, max_attempts=max_attempts)
    assert result is not None
    return result


def complete(store: Store, work: EvaluationClaim, *, now: datetime = NOW) -> None:
    messages = tuple(
        MailMessage(f"delivery-{work.id}-{i}", recipient, "Reading", "Synthetic group announcement")
        for i, recipient in enumerate(work.rule.recipients)
    )
    assert store.complete_evaluation(
        work,
        MatchResult(
            decision="match",
            reason="A reading group",
            evidence=["Reading group"],
            missing_information=[],
        ),
        messages,
        now=now,
    )


def test_first_fetch_baselines_but_explicit_backfill_enqueues(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    item = post()
    assert (
        store.commit_fetch(
            source(backfill=False),
            FetchResult((item,)),
            {item.external_id: (rule(),)},
            now=NOW,
            retention_days=7,
        )
        == 0
    )
    assert store.source_state("feed").initialized
    assert store.status() == {
        "identities": 1,
        "api_paused": 0,
        "mail_paused": 0,
        "sources_paused": 0,
    }
    newer = post("two")
    assert (
        store.commit_fetch(
            source(backfill=False),
            FetchResult((item, newer)),
            {"one": (rule(),), "two": (rule(),)},
            now=NOW + timedelta(minutes=2),
            retention_days=7,
        )
        == 1
    )
    other = post(source_id="other")
    assert (
        store.commit_fetch(
            source("other"),
            FetchResult((other,)),
            {"one": (rule(),)},
            now=NOW,
            retention_days=7,
        )
        == 1
    )
    assert store.status()["identities"] == 3


def test_snapshot_queue_identity_and_cursor_rollback_together(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TRIGGER injected_failure BEFORE INSERT ON evaluations "
            "BEGIN SELECT RAISE(ABORT,'synthetic_crash'); END",
        )
    with pytest.raises(sqlite3.IntegrityError):
        ingest(store)
    assert store.status()["identities"] == 0
    assert not store.source_state("feed").initialized
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER injected_failure")
    ingest(store)
    reopened = Store(path)
    assert reopened.source_state("feed").etag == "version-one"
    assert claim(reopened).post.text == "Reading group"


def test_rule_revisions_only_apply_to_new_discoveries(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    first, second = post(), post("two")
    old, updated = rule(), rule(requirement="Find a weekend reading group.")
    store.commit_fetch(source(), FetchResult((first,)), {"one": (old,)}, now=NOW, retention_days=7)
    assert (
        store.commit_fetch(
            source(),
            FetchResult((first, second)),
            {"one": (updated,), "two": (updated,)},
            now=NOW + timedelta(seconds=1),
            retention_days=7,
        )
        == 1
    )
    a, b = claim(store), claim(store)
    assert a.rule.revision == old.revision
    assert b.rule.revision == updated.revision
    assert a.post.external_id == "one" and b.post.external_id == "two"


def test_source_mismatch_cannot_advance_state(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    with pytest.raises(ValueError, match="source_identity_mismatch"):
        store.commit_fetch(
            source(),
            FetchResult((post(source_id="other"),)),
            {},
            now=NOW,
            retention_days=7,
        )
    assert store.status()["identities"] == 0
    assert not store.source_state("feed").initialized


def test_stale_evaluation_is_fenced_and_retries_are_bounded(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    ingest(store)
    old = claim(store, max_attempts=2)
    later = NOW + timedelta(seconds=61)
    new = claim(store, now=later, max_attempts=2)
    assert old.token != new.token and new.attempt == 2
    result = MatchResult(
        decision="no_match", reason="No match", evidence=[], missing_information=[]
    )
    assert not store.complete_evaluation(old, result, (), now=later)
    assert store.reserve_budget(old, now=later, amount_micro=1, limit_micro=10) is None
    assert (
        store.claim_evaluation(
            now=later + timedelta(seconds=61),
            lease_seconds=60,
            max_attempts=2,
        )
        is None
    )
    assert store.status()["evaluation_failed"] == 1


def test_evaluation_completion_and_recipient_jobs_are_atomic_and_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    ingest(store)
    work = claim(store)
    result = MatchResult(
        decision="match", reason="Relevant", evidence=["Reading"], missing_information=[]
    )
    with pytest.raises(ValueError, match="unconfigured_delivery_recipient"):
        store.complete_evaluation(
            work,
            result,
            (MailMessage("untrusted", "outside@example.invalid", "x", "x"),),
            now=NOW,
        )
    assert store.status()["evaluation_evaluating"] == 1
    complete(store, work)
    assert not store.complete_evaluation(work, result, (), now=NOW)
    assert store.status()["mail_pending"] == 2


def test_uncertain_mail_is_not_automatically_retried_and_old_claim_is_fenced(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.sqlite3")
    ingest(store)
    complete(store, claim(store))
    first = store.claim_delivery(now=NOW, lease_seconds=10, max_attempts=3)
    second = store.claim_delivery(now=NOW, lease_seconds=10, max_attempts=3)
    assert first is not None and second is not None
    assert first.message.recipient != second.message.recipient
    assert store.finish_delivery(second, now=NOW, state="accepted")
    later = NOW + timedelta(seconds=11)
    assert store.claim_delivery(now=later, lease_seconds=10, max_attempts=3) is None
    assert store.unknown_deliveries() == [first.id]
    assert not store.finish_delivery(first, now=later, state="accepted")
    assert store.retry_unknown(first.id, now=later)
    retry = store.claim_delivery(now=later, lease_seconds=10, max_attempts=3)
    assert retry is not None and retry.id == first.id and retry.token != first.token


def test_cleanup_keeps_dedupe_after_removing_all_content(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    ingest(store)
    complete(store, claim(store))
    assert store.cleanup(now=NOW + timedelta(days=8)) == 1
    assert store.status() == {
        "identities": 1,
        "api_paused": 0,
        "mail_paused": 0,
        "sources_paused": 0,
    }
    ingest(store)
    assert store.status()["identities"] == 1
    assert store.claim_evaluation(now=NOW, lease_seconds=60, max_attempts=3) is None


def test_budget_admission_is_atomic_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    ingest(store, count=8)
    work = [claim(store) for _ in range(8)]

    def reserve(item: EvaluationClaim) -> str | None:
        return Store(path).reserve_budget(item, now=NOW, amount_micro=60, limit_micro=100)

    with ThreadPoolExecutor(max_workers=8) as pool:
        reservations = list(pool.map(reserve, work))
    assert sum(value is not None for value in reservations) == 1
    assert store.budget_spent(NOW) == 60


def test_unknown_budget_survives_restart_and_settlement_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    ingest(store, count=3)
    a, b, c = claim(store), claim(store), claim(store)
    first = store.reserve_budget(a, now=NOW, amount_micro=60, limit_micro=100)
    assert first is not None
    assert store.reserve_budget(a, now=NOW, amount_micro=60, limit_micro=100) == first
    assert store.settle_budget(first, None)
    assert not store.settle_budget(first, 0)
    assert Store(path).budget_spent(NOW) == 60
    assert store.reserve_budget(b, now=NOW, amount_micro=50, limit_micro=100) is None
    second = store.reserve_budget(b, now=NOW, amount_micro=40, limit_micro=100)
    assert second is not None and store.settle_budget(second, 0)
    third = store.reserve_budget(c, now=NOW, amount_micro=40, limit_micro=100)
    assert third is not None and store.settle_budget(third, 15)
    assert store.budget_spent(NOW) == 75
    assert store.budget_spent(NOW + timedelta(days=1)) == 0


def test_pause_and_resume_persist_without_content_in_status(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    store.pause_api("authentication_failed")
    store.source_failure("feed", now=NOW, retry_after=30, pause_code="authentication_failed")
    reopened = Store(path)
    assert reopened.api_paused()
    assert not reopened.source_due("feed", NOW + timedelta(days=1), force=True)
    reopened.resume_api()
    reopened.resume_source("feed")
    assert not reopened.api_paused()
    assert reopened.source_due("feed", NOW)


def test_unrelated_database_is_rejected_without_modifying_any_bytes(tmp_path: Path) -> None:
    path = tmp_path / "unrelated.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE unrelated(value TEXT)")
    db.execute("INSERT INTO unrelated VALUES ('synthetic unrelated data')")
    db.commit()
    db.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="unrecognized_state_database"):
        Store(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not path.with_name(path.name + "-wal").exists()


def test_future_schema_is_rejected_before_writable_connection(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    Store(path)
    db = sqlite3.connect(path)
    db.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
    db.commit()
    db.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="unsupported_state_version"):
        Store(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_source_backoff_grows_and_success_resets_it(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    for _ in range(4):
        store.source_failure("feed", now=NOW, retry_after=60, pause_code=None)
    assert not store.source_due("feed", NOW + timedelta(seconds=239))
    assert store.source_due("feed", NOW + timedelta(seconds=240))
    store.commit_fetch(source(), FetchResult(()), {}, now=NOW, retention_days=7)
    store.source_failure("feed", now=NOW, retry_after=60, pause_code=None)
    assert store.source_due("feed", NOW + timedelta(seconds=60))
