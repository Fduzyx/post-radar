"""Transactional queues. Only explicit user operations retry uncertain mail delivery."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from post_radar.config import RuleConfig, SourceConfig
from post_radar.models import FetchResult, MailMessage, MatchResult, Post, SourceState


def timestamp(now: datetime) -> float:
    if now.tzinfo is None:
        raise ValueError("utc_clock_required")
    return now.timestamp()


@dataclass(frozen=True)
class EvaluationClaim:
    id: str
    token: str
    post: Post
    rule: RuleConfig
    attempt: int


@dataclass(frozen=True)
class DeliveryClaim:
    id: str
    token: str
    rule_id: str
    message: MailMessage
    attempt: int


_APPLICATION_ID = 0x50524452
_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(
 id TEXT PRIMARY KEY, initialized INTEGER NOT NULL DEFAULT 0,
 etag TEXT, last_modified TEXT, next_at REAL NOT NULL DEFAULT 0, pause_code TEXT,
 failure_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS identities(
 source_id TEXT NOT NULL, external_id TEXT NOT NULL, first_seen REAL NOT NULL,
 PRIMARY KEY(source_id,external_id)
);
CREATE TABLE IF NOT EXISTS evaluations(
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, external_id TEXT NOT NULL,
 rule_id TEXT NOT NULL, revision TEXT NOT NULL, post_json TEXT NOT NULL,
 rule_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 attempt INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
 token TEXT, lease_until REAL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 result_json TEXT, error_code TEXT,
 UNIQUE(source_id,external_id,rule_id,revision)
);
CREATE INDEX IF NOT EXISTS evaluation_ready ON evaluations(state,next_at,created_at);
CREATE TABLE IF NOT EXISTS deliveries(
 id TEXT PRIMARY KEY, evaluation_id TEXT NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
 rule_id TEXT NOT NULL, recipient TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempt INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0, token TEXT, lease_until REAL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL, error_code TEXT,
 UNIQUE(evaluation_id,recipient)
);
CREATE INDEX IF NOT EXISTS delivery_ready ON deliveries(state,next_at,created_at);
CREATE TABLE IF NOT EXISTS budget_attempts(
 id TEXT PRIMARY KEY, evaluation_id TEXT NOT NULL, claim_token TEXT NOT NULL UNIQUE,
 day TEXT NOT NULL, reserved_micro INTEGER NOT NULL, charged_micro INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'reserved', created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS budget_day ON budget_attempts(day);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if self.path.exists() and self.path.stat().st_size:
            self._validate_existing_database()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                "BEGIN IMMEDIATE;"
                + _SCHEMA
                + "INSERT OR IGNORE INTO metadata VALUES ('application','post-radar');"
                + "INSERT OR IGNORE INTO metadata VALUES ('schema_version','1');"
                + f"PRAGMA application_id={_APPLICATION_ID}; COMMIT;",
            )

    def _validate_existing_database(self) -> None:
        """Reject unrelated or future databases before any writable SQLite connection."""
        try:
            db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                application_id = db.execute("PRAGMA application_id").fetchone()[0]
                if application_id != _APPLICATION_ID:
                    raise ValueError("unrecognized_state_database")
                metadata = dict(db.execute("SELECT key,value FROM metadata"))
                if metadata.get("application") != "post-radar":
                    raise ValueError("unrecognized_state_database")
                if metadata.get("schema_version") != "1":
                    raise ValueError("unsupported_state_version")
            finally:
                db.close()
        except sqlite3.Error:
            raise ValueError("unrecognized_state_database") from None

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def source_state(self, source_id: str) -> SourceState:
        with self._connection() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            return SourceState()
        return SourceState(bool(row["initialized"]), row["etag"], row["last_modified"])

    def source_due(self, source_id: str, now: datetime, *, force: bool = False) -> bool:
        with self._connection() as db:
            row = db.execute(
                "SELECT next_at,pause_code FROM sources WHERE id=?",
                (source_id,),
            ).fetchone()
        return row is None or (row[1] is None and (force or row[0] <= timestamp(now)))

    def source_failure(
        self,
        source_id: str,
        *,
        now: datetime,
        retry_after: float,
        pause_code: str | None,
    ) -> None:
        with self._connection(write=True) as db:
            row = db.execute(
                "SELECT failure_count FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            failures = int(row[0]) + 1 if row is not None else 1
            delay = max(retry_after, min(86400, 30 * 2 ** min(failures - 1, 12)))
            db.execute(
                "INSERT INTO sources(id,next_at,pause_code,failure_count) VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET next_at=excluded.next_at,"
                "pause_code=excluded.pause_code,failure_count=excluded.failure_count",
                (source_id, timestamp(now) + delay, pause_code, failures),
            )

    def resume_source(self, source_id: str) -> None:
        with self._connection(write=True) as db:
            db.execute(
                "UPDATE sources SET pause_code=NULL,next_at=0,failure_count=0 WHERE id=?",
                (source_id,),
            )

    def commit_fetch(
        self,
        source: SourceConfig,
        result: FetchResult,
        matches: Mapping[str, Sequence[RuleConfig]],
        *,
        now: datetime,
        retention_days: int,
    ) -> int:
        """Discovery, immutable snapshots, tasks and source state share a transaction."""
        if any(post.source_id != source.id for post in result.posts):
            raise ValueError("source_identity_mismatch")
        if result.not_modified and result.posts:
            raise ValueError("invalid_not_modified_response")
        now_value = timestamp(now)
        expires = timestamp(now + timedelta(days=retention_days))
        queued = 0
        with self._connection(write=True) as db:
            previous = db.execute(
                "SELECT initialized FROM sources WHERE id=?", (source.id,)
            ).fetchone()
            baseline = (previous is None or not previous[0]) and not source.backfill
            for post in result.posts:
                inserted = db.execute(
                    "INSERT OR IGNORE INTO identities VALUES (?,?,?)",
                    (source.id, post.external_id, now_value),
                ).rowcount
                if not inserted or baseline:
                    continue
                for rule in matches.get(post.external_id, ()):
                    if not rule.enabled or source.id not in rule.source_ids:
                        continue
                    db.execute(
                        "INSERT INTO evaluations(id,source_id,external_id,rule_id,revision,"
                        "post_json,rule_json,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            uuid.uuid4().hex,
                            source.id,
                            post.external_id,
                            rule.id,
                            rule.revision,
                            post.model_dump_json(),
                            rule.model_dump_json(),
                            now_value,
                            expires,
                        ),
                    )
                    queued += 1
            if result.not_modified:
                db.execute(
                    "INSERT INTO sources(id,next_at) VALUES (?,?) ON CONFLICT(id) "
                    "DO UPDATE SET next_at=excluded.next_at,pause_code=NULL,failure_count=0",
                    (source.id, now_value + source.poll_interval_seconds),
                )
            else:
                db.execute(
                    "INSERT INTO sources(id,initialized,etag,last_modified,next_at) "
                    "VALUES (?,1,?,?,?) ON CONFLICT(id) DO UPDATE SET initialized=1,"
                    "etag=excluded.etag,last_modified=excluded.last_modified,"
                    "next_at=excluded.next_at,pause_code=NULL,failure_count=0",
                    (
                        source.id,
                        result.etag,
                        result.last_modified,
                        now_value + source.poll_interval_seconds,
                    ),
                )
        return queued

    def claim_evaluation(
        self,
        *,
        now: datetime,
        lease_seconds: float,
        max_attempts: int,
    ) -> EvaluationClaim | None:
        moment = timestamp(now)
        with self._connection(write=True) as db:
            db.execute(
                "UPDATE evaluations SET state=CASE WHEN attempt>=? THEN 'failed' ELSE 'retry' END,"
                "next_at=?,token=NULL,lease_until=NULL,error_code='evaluation_lease_expired' "
                "WHERE state='evaluating' AND lease_until<=?",
                (max_attempts, moment, moment),
            )
            row = db.execute(
                "SELECT * FROM evaluations WHERE state IN ('pending','retry') AND next_at<=? "
                "AND expires_at>? AND attempt<? ORDER BY created_at,id LIMIT 1",
                (moment, moment, max_attempts),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE evaluations SET state='evaluating',attempt=attempt+1,token=?,"
                "lease_until=? WHERE id=?",
                (token, moment + lease_seconds, row["id"]),
            )
            return EvaluationClaim(
                row["id"],
                token,
                Post.model_validate_json(row["post_json"]),
                RuleConfig.model_validate_json(row["rule_json"]),
                int(row["attempt"]) + 1,
            )

    def defer_evaluation(
        self,
        claim: EvaluationClaim,
        *,
        now: datetime,
        delay: float,
    ) -> bool:
        with self._connection(write=True) as db:
            return bool(
                db.execute(
                    "UPDATE evaluations SET state='retry',attempt=MAX(0,attempt-1),"
                    "next_at=?,token=NULL,lease_until=NULL WHERE id=? AND token=? "
                    "AND state='evaluating' AND lease_until>?",
                    (timestamp(now) + delay, claim.id, claim.token, timestamp(now)),
                ).rowcount
            )

    def fail_evaluation(
        self,
        claim: EvaluationClaim,
        *,
        now: datetime,
        code: str,
        delay: float,
        permanent: bool,
        max_attempts: int,
    ) -> bool:
        state = "failed" if permanent or claim.attempt >= max_attempts else "retry"
        with self._connection(write=True) as db:
            return bool(
                db.execute(
                    "UPDATE evaluations SET state=?,error_code=?,next_at=?,token=NULL,"
                    "lease_until=NULL WHERE id=? AND token=? "
                    "AND state='evaluating' AND lease_until>?",
                    (state, code, timestamp(now) + delay, claim.id, claim.token, timestamp(now)),
                ).rowcount
            )

    def complete_evaluation(
        self,
        claim: EvaluationClaim,
        result: MatchResult,
        messages: Sequence[MailMessage],
        *,
        now: datetime,
    ) -> bool:
        if result.decision != "match" and messages:
            raise ValueError("unmatched_delivery_forbidden")
        if len({message.recipient for message in messages}) != len(messages):
            raise ValueError("duplicate_delivery_recipient")
        if any(message.recipient not in claim.rule.recipients for message in messages):
            raise ValueError("unconfigured_delivery_recipient")
        moment = timestamp(now)
        with self._connection(write=True) as db:
            row = db.execute(
                "SELECT expires_at FROM evaluations WHERE id=? AND token=? "
                "AND state='evaluating' AND lease_until>? AND expires_at>?",
                (claim.id, claim.token, moment, moment),
            ).fetchone()
            if row is None:
                return False
            db.execute(
                "UPDATE evaluations SET state='completed',result_json=?,token=NULL,"
                "lease_until=NULL,error_code=NULL WHERE id=?",
                (result.model_dump_json(), claim.id),
            )
            for message in messages:
                db.execute(
                    "INSERT INTO deliveries(id,evaluation_id,rule_id,recipient,subject,body,"
                    "created_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        message.delivery_id,
                        claim.id,
                        claim.rule.id,
                        message.recipient,
                        message.subject,
                        message.body,
                        moment,
                        row[0],
                    ),
                )
        return True

    def reserve_budget(
        self,
        claim: EvaluationClaim,
        *,
        now: datetime,
        amount_micro: int,
        limit_micro: int,
    ) -> str | None:
        if amount_micro <= 0 or limit_micro <= 0:
            return None
        day = now.astimezone(UTC).date().isoformat()
        moment = timestamp(now)
        with self._connection(write=True) as db:
            valid = db.execute(
                "SELECT 1 FROM evaluations WHERE id=? AND token=? AND state='evaluating' "
                "AND lease_until>? AND expires_at>?",
                (claim.id, claim.token, moment, moment),
            ).fetchone()
            if valid is None:
                return None
            previous = db.execute(
                "SELECT id FROM budget_attempts WHERE claim_token=?",
                (claim.token,),
            ).fetchone()
            if previous is not None:
                return str(previous[0])
            spent = int(
                db.execute(
                    "SELECT COALESCE(SUM(charged_micro),0) FROM budget_attempts WHERE day=?",
                    (day,),
                ).fetchone()[0]
            )
            if spent + amount_micro > limit_micro:
                return None
            identity = uuid.uuid4().hex
            db.execute(
                "INSERT INTO budget_attempts(id,evaluation_id,claim_token,day,reserved_micro,"
                "charged_micro,created_at) VALUES (?,?,?,?,?,?,?)",
                (identity, claim.id, claim.token, day, amount_micro, amount_micro, moment),
            )
            return identity

    def settle_budget(self, reservation_id: str, actual_micro: int | None) -> bool:
        if actual_micro is not None and (type(actual_micro) is not int or actual_micro < 0):
            raise ValueError("invalid_actual_cost")
        state = "uncertain" if actual_micro is None else "settled"
        with self._connection(write=True) as db:
            return bool(
                db.execute(
                    "UPDATE budget_attempts SET charged_micro=COALESCE(?,charged_micro),state=? "
                    "WHERE id=? AND state='reserved'",
                    (actual_micro, state, reservation_id),
                ).rowcount
            )

    def budget_spent(self, now: datetime) -> int:
        with self._connection() as db:
            return int(
                db.execute(
                    "SELECT COALESCE(SUM(charged_micro),0) FROM budget_attempts WHERE day=?",
                    (now.astimezone(UTC).date().isoformat(),),
                ).fetchone()[0]
            )

    def pause_api(self, code: str) -> None:
        with self._connection(write=True) as db:
            db.execute(
                "INSERT INTO metadata VALUES ('api_pause',?) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value",
                (code,),
            )

    def api_paused(self) -> bool:
        with self._connection() as db:
            return db.execute("SELECT 1 FROM metadata WHERE key='api_pause'").fetchone() is not None

    def resume_api(self) -> None:
        with self._connection(write=True) as db:
            db.execute("DELETE FROM metadata WHERE key='api_pause'")

    def pause_mail(self, code: str) -> None:
        with self._connection(write=True) as db:
            db.execute(
                "INSERT INTO metadata VALUES ('mail_pause',?) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value",
                (code,),
            )

    def mail_paused(self) -> bool:
        with self._connection() as db:
            return (
                db.execute("SELECT 1 FROM metadata WHERE key='mail_pause'").fetchone() is not None
            )

    def resume_mail(self) -> None:
        with self._connection(write=True) as db:
            db.execute("DELETE FROM metadata WHERE key='mail_pause'")
            db.execute(
                "UPDATE deliveries SET next_at=0 WHERE state='retry' AND error_code IN "
                "('authentication_failed','missing_credential','credential_store_unavailable')",
            )

    def claim_delivery(
        self,
        *,
        now: datetime,
        lease_seconds: float,
        max_attempts: int,
    ) -> DeliveryClaim | None:
        moment = timestamp(now)
        with self._connection(write=True) as db:
            db.execute(
                "UPDATE deliveries SET state='unknown',token=NULL,lease_until=NULL,"
                "error_code='delivery_lease_expired' WHERE state='delivering' AND lease_until<=?",
                (moment,),
            )
            row = db.execute(
                "SELECT * FROM deliveries WHERE state IN ('pending','retry') AND next_at<=? "
                "AND expires_at>? AND attempt<? ORDER BY created_at,id LIMIT 1",
                (moment, moment, max_attempts),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE deliveries SET state='delivering',attempt=attempt+1,token=?,"
                "lease_until=? WHERE id=?",
                (token, moment + lease_seconds, row["id"]),
            )
            message = MailMessage(row["id"], row["recipient"], row["subject"], row["body"])
            return DeliveryClaim(row["id"], token, row["rule_id"], message, row["attempt"] + 1)

    def finish_delivery(
        self,
        claim: DeliveryClaim,
        *,
        now: datetime,
        state: str,
        code: str | None = None,
        delay: float = 0,
    ) -> bool:
        if state not in {"accepted", "retry", "failed", "unknown"}:
            raise ValueError("invalid_delivery_state")
        with self._connection(write=True) as db:
            return bool(
                db.execute(
                    "UPDATE deliveries SET state=?,error_code=?,next_at=?,token=NULL,"
                    "lease_until=NULL WHERE id=? AND token=? "
                    "AND state='delivering' AND lease_until>?",
                    (state, code, timestamp(now) + delay, claim.id, claim.token, timestamp(now)),
                ).rowcount
            )

    def retry_unknown(self, delivery_id: str, *, now: datetime | None = None) -> bool:
        moment = timestamp(now or datetime.now(UTC))
        with self._connection(write=True) as db:
            return bool(
                db.execute(
                    "UPDATE deliveries SET state='retry',attempt=0,next_at=?,error_code=NULL "
                    "WHERE id=? AND state='unknown' AND expires_at>? AND next_at<=?",
                    (moment, delivery_id, moment, moment),
                ).rowcount
            )

    def unknown_deliveries(self) -> list[str]:
        with self._connection() as db:
            return [
                str(row[0])
                for row in db.execute(
                    "SELECT id FROM deliveries WHERE state='unknown' ORDER BY created_at,id",
                )
            ]

    def cleanup(self, *, now: datetime) -> int:
        """Remove retained content, never the minimal identities needed for deduplication."""
        with self._connection(write=True) as db:
            count = db.execute(
                "DELETE FROM evaluations WHERE expires_at<=?", (timestamp(now),)
            ).rowcount
        return count

    def status(self) -> dict[str, int]:
        with self._connection() as db:
            result = {
                "identities": int(db.execute("SELECT COUNT(*) FROM identities").fetchone()[0])
            }
            for table, prefix in (("evaluations", "evaluation"), ("deliveries", "mail")):
                for row in db.execute(f"SELECT state,COUNT(*) FROM {table} GROUP BY state"):
                    result[f"{prefix}_{row[0]}"] = int(row[1])
            result["api_paused"] = int(
                db.execute("SELECT 1 FROM metadata WHERE key='api_pause'").fetchone() is not None,
            )
            result["mail_paused"] = int(
                db.execute("SELECT 1 FROM metadata WHERE key='mail_pause'").fetchone() is not None,
            )
            result["sources_paused"] = int(
                db.execute(
                    "SELECT COUNT(*) FROM sources WHERE pause_code IS NOT NULL",
                ).fetchone()[0]
            )
        return result
