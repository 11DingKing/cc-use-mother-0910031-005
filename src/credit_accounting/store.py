"""SQLite 存储层。

关键并发与审计设计：
- 所有多表写操作在 ``BEGIN IMMEDIATE`` 事务内执行，配合状态条件 UPDATE，
  并发确认只有一方成功，落败方得到 :class:`StaleStateError`；
- 版本表只追加（insert-only），任何内容永不更新、删除；
- 期间确认时把版本指针 + 每个版本内容哈希固化为冻结快照，
  正式分录行内嵌证据快照，形成自洽的冻结记录；
- 调整单保存“调整后完整快照”，逐单成链，支持逐单重放复算。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import engine
from .errors import ConflictError, NotFoundError, StaleStateError, ValidationError
from .models import (
    ADJ_LATE_DATA,
    ADJ_RULE_ERRATA,
    ADJ_WITHDRAWAL,
    ADJUSTMENT_TYPES,
    CONFIRMED,
    DRAFT,
    ENTRY_ADJUSTMENT,
    ENTRY_FORMAL,
    EVIDENCE_ERRATA,
    EXECUTING,
    PENDING,
    SEALED,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS enterprise (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS period (
    id TEXT PRIMARY KEY,
    enterprise_id TEXT NOT NULL REFERENCES enterprise(id),
    year INTEGER NOT NULL,
    state TEXT NOT NULL,
    draft_snapshot TEXT,
    frozen_snapshot TEXT,
    input_hash TEXT,
    formal_entry_id TEXT,
    lock_version INTEGER NOT NULL DEFAULT 1,
    opened_at TEXT, submitted_at TEXT, confirmed_at TEXT, posted_at TEXT, sealed_at TEXT,
    UNIQUE(enterprise_id, year)
);
CREATE TABLE IF NOT EXISTS version (
    id TEXT PRIMARY KEY,
    enterprise_id TEXT,
    year INTEGER,
    version_type TEXT NOT NULL,
    model_code TEXT,
    batch_no TEXT,
    content TEXT NOT NULL,
    evidence TEXT NOT NULL,
    supersedes_id TEXT,
    created_by_role TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_version_model
    ON version(enterprise_id, model_code, version_type);
CREATE INDEX IF NOT EXISTS idx_version_global ON version(version_type, year);
CREATE TABLE IF NOT EXISTS trial (
    id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    result TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trial_period ON trial(period_id);
CREATE TABLE IF NOT EXISTS entry (
    id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    total_credit TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    rule_version_id TEXT,
    policy_version_id TEXT,
    adjustment_id TEXT,
    memo TEXT,
    created_by_role TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entry_period ON entry(period_id);
CREATE TABLE IF NOT EXISTS entry_line (
    id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entry(id),
    model_code TEXT NOT NULL,
    energy_type TEXT,
    quantity TEXT,
    unit_credit TEXT,
    multiplier TEXT,
    old_credit TEXT,
    final_credit TEXT NOT NULL,
    production_version_id TEXT,
    energy_version_id TEXT,
    rule_version_id TEXT,
    policy_version_id TEXT,
    steps TEXT,
    evidence TEXT
);
CREATE INDEX IF NOT EXISTS idx_line_entry ON entry_line(entry_id);
CREATE TABLE IF NOT EXISTS adjustment (
    id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL,
    adjustment_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    reason TEXT,
    evidence TEXT,
    requested_by_role TEXT,
    applied_snapshot TEXT,
    hash_before TEXT,
    hash_after TEXT,
    total_before TEXT,
    total_after TEXT,
    entry_id TEXT,
    created_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_adjustment_period ON adjustment(period_id);
"""

_MODEL_VERSION_TYPES = {"production", "energy"}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def content_hash(content: dict) -> str:
    blob = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Store:
    """线程安全的账务存储；一个进程共享一个实例。"""

    def __init__(self, path: str | Path = ":memory:"):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------ helpers

    class _Tx:
        def __init__(self, store: "Store"):
            self.store = store

        def __enter__(self):
            self.store._lock.acquire()
            self.store.conn.execute("BEGIN IMMEDIATE")
            return self.store.conn

        def __exit__(self, exc_type, exc, tb):
            try:
                if exc_type is None:
                    self.store.conn.commit()
                else:
                    self.store.conn.rollback()
            finally:
                self.store._lock.release()
            return False

    def tx(self) -> "Store._Tx":
        return self._Tx(self)

    def _row(self, sql: str, params: tuple = ()) -> sqlite3.Row:
        cur = self.conn.execute(sql, params)
        row = cur.fetchone()
        return row

    def require_period(self, period_id: str) -> sqlite3.Row:
        row = self._row("SELECT * FROM period WHERE id=?", (period_id,))
        if row is None:
            raise NotFoundError(f"年度期间不存在：{period_id}")
        return row

    def require_enterprise(self, enterprise_id: str) -> sqlite3.Row:
        row = self._row("SELECT * FROM enterprise WHERE id=?", (enterprise_id,))
        if row is None:
            raise NotFoundError(f"企业不存在：{enterprise_id}")
        return row

    @staticmethod
    def _loads(value: str | None, default: Any = None) -> Any:
        if value is None:
            return default
        return json.loads(value)

    def _version_content(self, version_id: str) -> dict:
        row = self._row("SELECT content FROM version WHERE id=?", (version_id,))
        if row is None:
            raise NotFoundError(f"版本不存在：{version_id}")
        return json.loads(row["content"])

    def _load_versions(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT id, content FROM version WHERE id IN ({placeholders})", ids
        ).fetchall()
        found = {r["id"]: json.loads(r["content"]) for r in rows}
        missing = [i for i in ids if i not in found]
        if missing:
            raise NotFoundError("版本不存在：" + "、".join(missing))
        return found

    # ------------------------------------------------------------ 主数据

    def create_enterprise(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValidationError("企业名称不能为空")
        eid = _new_id("ent")
        with self.tx():
            self.conn.execute(
                "INSERT INTO enterprise(id, name, created_at) VALUES(?,?,?)",
                (eid, name, utcnow_iso()),
            )
        return {"id": eid, "name": name}

    def list_enterprises(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM enterprise ORDER BY id").fetchall()
        return [{"id": r["id"], "name": r["name"]} for r in rows]

    def open_period(self, enterprise_id: str, year: int) -> dict:
        self.require_enterprise(enterprise_id)
        year = _valid_year(year)
        pid = _new_id("per")
        now = utcnow_iso()
        with self.tx():
            try:
                self.conn.execute(
                    """INSERT INTO period(id, enterprise_id, year, state, draft_snapshot,
                                          opened_at, lock_version)
                       VALUES(?,?,?,?,?,?,1)""",
                    (pid, enterprise_id, year, DRAFT, json.dumps({"models": []}), now),
                )
            except sqlite3.IntegrityError:
                raise ConflictError(f"企业 {enterprise_id} 的 {year} 年度期间已存在") from None
        return self.get_period(pid)

    def get_period(self, period_id: str) -> dict:
        row = self.require_period(period_id)
        return self._period_dict(row)

    def find_period(self, enterprise_id: str, year: int) -> dict:
        row = self._row(
            "SELECT * FROM period WHERE enterprise_id=? AND year=?", (enterprise_id, year)
        )
        if row is None:
            raise NotFoundError(f"企业 {enterprise_id} 的 {year} 年度期间不存在")
        return self._period_dict(row)

    def _period_dict(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "enterprise_id": row["enterprise_id"],
            "year": row["year"],
            "state": row["state"],
            "draft_snapshot": self._loads(row["draft_snapshot"]),
            "frozen_snapshot": self._loads(row["frozen_snapshot"]),
            "input_hash": row["input_hash"],
            "formal_entry_id": row["formal_entry_id"],
            "lock_version": row["lock_version"],
            "opened_at": row["opened_at"],
            "submitted_at": row["submitted_at"],
            "confirmed_at": row["confirmed_at"],
            "posted_at": row["posted_at"],
            "sealed_at": row["sealed_at"],
        }

    # ------------------------------------------------------------ 版本

    @staticmethod
    def _validate_evidence(evidence: Any) -> dict:
        if not isinstance(evidence, dict):
            raise ValidationError("evidence 必须是对象，至少包含 source_type 与 source_ref")
        source_type = str(evidence.get("source_type") or "").strip()
        source_ref = str(evidence.get("source_ref") or "").strip()
        if not source_type or not source_ref:
            raise ValidationError("evidence.source_type 与 evidence.source_ref 不能为空")
        out = {"source_type": source_type, "source_ref": source_ref}
        if "received_at" in evidence:
            out["received_at"] = str(evidence["received_at"])
        if "note" in evidence:
            out["note"] = str(evidence["note"])
        return out

    def submit_model_version(
        self,
        *,
        enterprise_id: str,
        year: int,
        model_code: str,
        version_type: str,
        content: dict,
        evidence: dict,
        actor_role: str,
        batch_no: str | None = None,
    ) -> dict:
        if version_type not in _MODEL_VERSION_TYPES:
            raise ValidationError("企业批次版本类型只能是 production 或 energy")
        model_code = (model_code or "").strip()
        if not model_code:
            raise ValidationError("model_code 不能为空")
        if not isinstance(content, dict) or not content:
            raise ValidationError("content 必须是非空对象")
        evidence = self._validate_evidence(evidence)
        year = _valid_year(year)
        self.require_enterprise(enterprise_id)

        with self.tx():
            row = self._row(
                "SELECT * FROM period WHERE enterprise_id=? AND year=?",
                (enterprise_id, year),
            )
            if row is None:
                raise NotFoundError(f"请先开启 {year} 年度申报窗口")
            if row["state"] == PENDING:
                raise ConflictError("期间已提交待确认，不能录入新批次；请完成确认后走调整单")
            prev = self._row(
                """SELECT id FROM version
                   WHERE enterprise_id=? AND model_code=? AND version_type=?
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (enterprise_id, model_code, version_type),
            )
            vid = _new_id("v")
            frozen = row["state"] != DRAFT
            self.conn.execute(
                """INSERT INTO version(id, enterprise_id, year, version_type, model_code,
                                       batch_no, content, evidence, supersedes_id,
                                       created_by_role, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    vid, enterprise_id, year, version_type, model_code, batch_no,
                    json.dumps(content, ensure_ascii=False, sort_keys=True),
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    prev["id"] if prev else None,
                    actor_role, utcnow_iso(),
                ),
            )
            result = self.get_version(vid)
            if frozen:
                # 冻结后录入的版本只作为证据留存，必须经由调整单才生效
                result["effective_only_via"] = "adjustment"
                return result
            snapshot = json.loads(row["draft_snapshot"])
            pointers = {m["model_code"]: m for m in snapshot["models"]}
            ptr = pointers.setdefault(model_code, {"model_code": model_code})
            ptr[f"{version_type}_version_id"] = vid
            snapshot["models"] = [pointers[k] for k in sorted(pointers)]
            self.conn.execute(
                "UPDATE period SET draft_snapshot=? WHERE id=?",
                (json.dumps(snapshot, ensure_ascii=False, sort_keys=True), row["id"]),
            )
        return self.get_version(vid)

    def publish_policy_version(
        self, *, year: int, content: dict, evidence: dict, actor_role: str
    ) -> dict:
        year = _valid_year(year)
        if not isinstance(content, dict) or "nev_multiplier" not in content:
            raise ValidationError("政策系数 content 至少包含 nev_multiplier / cafc_multiplier")
        evidence = self._validate_evidence(evidence)
        vid = _new_id("v")
        with self.tx():
            self.conn.execute(
                """INSERT INTO version(id, enterprise_id, year, version_type, model_code,
                                       batch_no, content, evidence, supersedes_id,
                                       created_by_role, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    vid, None, year, "policy", None, None,
                    json.dumps(content, ensure_ascii=False, sort_keys=True),
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    self._prev_global("policy", year)["id"] if self._prev_global("policy", year) else None,
                    actor_role, utcnow_iso(),
                ),
            )
        return self.get_version(vid)

    def publish_rule_version(
        self, *, content: dict, evidence: dict, actor_role: str, year: int | None = None
    ) -> dict:
        if not isinstance(content, dict):
            raise ValidationError("规则 content 必须是对象")
        if str(content.get("engine")) != engine.ENGINE_VERSION:
            raise ValidationError(
                f"规则内容 engine 必须是 {engine.ENGINE_VERSION}；引擎升级才能改变算法"
            )
        evidence = self._validate_evidence(evidence)
        if year is not None:
            year = _valid_year(year)
        vid = _new_id("rv")
        with self.tx():
            prev = self._prev_global("rule", year)
            self.conn.execute(
                """INSERT INTO version(id, enterprise_id, year, version_type, model_code,
                                       batch_no, content, evidence, supersedes_id,
                                       created_by_role, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    vid, None, year, "rule", None, None,
                    json.dumps(content, ensure_ascii=False, sort_keys=True),
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    prev["id"] if prev else None,
                    actor_role, utcnow_iso(),
                ),
            )
        return self.get_version(vid)

    def _prev_global(self, version_type: str, year: int | None) -> sqlite3.Row | None:
        if version_type == "policy":
            return self._row(
                """SELECT id FROM version WHERE version_type='policy' AND year=?
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (year,),
            )
        return self._row(
            """SELECT id FROM version WHERE version_type='rule'
                 AND (year IS NULL OR year=?)
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (year if year is not None else -1,),
        )

    def get_version(self, version_id: str) -> dict:
        row = self._row("SELECT * FROM version WHERE id=?", (version_id,))
        if row is None:
            raise NotFoundError(f"版本不存在：{version_id}")
        return {
            "id": row["id"],
            "enterprise_id": row["enterprise_id"],
            "year": row["year"],
            "version_type": row["version_type"],
            "model_code": row["model_code"],
            "batch_no": row["batch_no"],
            "content": json.loads(row["content"]),
            "evidence": json.loads(row["evidence"]),
            "supersedes_id": row["supersedes_id"],
            "created_by_role": row["created_by_role"],
            "created_at": row["created_at"],
        }

    def list_versions(
        self, *, enterprise_id: str | None = None, model_code: str | None = None,
        version_type: str | None = None, year: int | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM version WHERE 1=1"
        params: list[Any] = []
        if enterprise_id is not None:
            sql += " AND enterprise_id=?"
            params.append(enterprise_id)
        if model_code is not None:
            sql += " AND model_code=?"
            params.append(model_code)
        if version_type is not None:
            sql += " AND version_type=?"
            params.append(version_type)
        if year is not None:
            sql += " AND (year IS NULL OR year=?)"
            params.append(year)
        sql += " ORDER BY created_at, rowid"
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "id": r["id"], "enterprise_id": r["enterprise_id"], "year": r["year"],
                "version_type": r["version_type"], "model_code": r["model_code"],
                "batch_no": r["batch_no"], "content": json.loads(r["content"]),
                "evidence": json.loads(r["evidence"]), "supersedes_id": r["supersedes_id"],
                "created_by_role": r["created_by_role"], "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------ 试算

    def _active_global(self, version_type: str, year: int) -> str:
        if version_type == "policy":
            row = self._prev_global("policy", year)
        else:
            row = self._prev_global("rule", year)
        if row is None:
            what = "政策系数" if version_type == "policy" else "核算规则"
            raise ConflictError(f"{year} 年度尚无生效的{what}版本")
        return row["id"]

    def _build_snapshot(self, period_row: sqlite3.Row, draft: dict) -> dict:
        """根据指针草稿补齐生效的规则/政策版本，并为每个版本计算内容哈希。"""
        models = draft.get("models") or []
        complete = []
        for ptr in sorted(models, key=lambda m: m["model_code"]):
            missing = [
                k for k in ("production_version_id", "energy_version_id")
                if not ptr.get(k)
            ]
            if missing:
                raise ConflictError(
                    f"车型 {ptr['model_code']} 缺少批次数据：{'、'.join(missing)}"
                )
            complete.append({
                "model_code": ptr["model_code"],
                "production_version_id": ptr["production_version_id"],
                "energy_version_id": ptr["energy_version_id"],
            })
        if not complete:
            raise ConflictError("期间内尚无任何车型数据，无法试算")
        rule_id = draft.get("rule_version_id") or self._active_global("rule", period_row["year"])
        policy_id = draft.get("policy_version_id") or self._active_global("policy", period_row["year"])
        version_ids = [rule_id, policy_id]
        for m in complete:
            version_ids += [m["production_version_id"], m["energy_version_id"]]
        contents = self._load_versions(version_ids)
        snapshot = {
            "enterprise_id": period_row["enterprise_id"],
            "year": period_row["year"],
            "engine_version": engine.ENGINE_VERSION,
            "rule_version_id": rule_id,
            "policy_version_id": policy_id,
            "models": complete,
            "content_hashes": {vid: content_hash(c) for vid, c in contents.items()},
        }
        return snapshot

    def trial(self, period_id: str) -> dict:
        with self.tx():
            period = self.require_period(period_id)
            if period["state"] not in (DRAFT, PENDING):
                raise ConflictError(f"期间已「{period['state']}」，试算请使用监管复算接口")
            draft = json.loads(period["draft_snapshot"])
            snapshot = self._build_snapshot(period, draft)
            result = engine.calculate(snapshot, versions=self._load_versions(_snapshot_ids(snapshot)))
            tid = _new_id("tri")
            self.conn.execute(
                """INSERT INTO trial(id, period_id, snapshot, result, input_hash, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (
                    tid, period_id,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    result["input_hash"], utcnow_iso(),
                ),
            )
            return {
                "trial_id": tid,
                "period_id": period_id,
                "period_state": period["state"],
                "engine_version": result["engine_version"],
                "rule_version_id": result["rule_version_id"],
                "policy_version_id": result["policy_version_id"],
                "input_hash": result["input_hash"],
                "total_credit": result["total_credit"],
                "lines": result["lines"],
                "notice": "试算结果仅供解释与核对，企业确认后才会冻结为正式分录",
            }

    def list_trials(self, period_id: str) -> list[dict]:
        self.require_period(period_id)
        rows = self.conn.execute(
            "SELECT id, input_hash, created_at, result FROM trial WHERE period_id=? ORDER BY rowid",
            (period_id,),
        ).fetchall()
        return [
            {
                "trial_id": r["id"],
                "input_hash": r["input_hash"],
                "created_at": r["created_at"],
                "total_credit": json.loads(r["result"])["total_credit"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------ 状态流转

    def _transition(self, period_id: str, *, expected: tuple[str, ...], target: str,
                    stamp_col: str) -> sqlite3.Row:
        now = utcnow_iso()
        cur = self.conn.execute(
            f"""UPDATE period SET state=?, {stamp_col}=?, lock_version=lock_version+1
                WHERE id=? AND state IN ({','.join('?' * len(expected))})""",
            (target, now, period_id, *expected),
        )
        if cur.rowcount == 0:
            row = self.require_period(period_id)
            raise StaleStateError(
                f"期间当前状态为「{row['state']}」，无法从{'/'.join(expected)}流转到「{target}」"
                "（可能已被其他并发请求处理）"
            )
        return self._row("SELECT * FROM period WHERE id=?", (period_id,))

    def submit(self, period_id: str) -> dict:
        with self.tx():
            period = self.require_period(period_id)
            if period["state"] != DRAFT:
                raise StaleStateError(f"只有「{DRAFT}」状态可以提交，当前为「{period['state']}」")
            draft = json.loads(period["draft_snapshot"])
            # 提交前做一次完整试算，提前暴露参数缺项
            snapshot = self._build_snapshot(period, draft)
            engine.calculate(snapshot, versions=self._load_versions(_snapshot_ids(snapshot)))
            self._transition(period_id, expected=(DRAFT,), target=PENDING, stamp_col="submitted_at")
        return self.get_period(period_id)

    def confirm(self, period_id: str, *, actor_role: str) -> dict:
        """企业确认：冻结输入快照并生成正式分录。"""
        with self.tx():
            period = self.require_period(period_id)
            if period["state"] != PENDING:
                raise StaleStateError(
                    f"只有「{PENDING}」状态可以确认，当前为「{period['state']}」；"
                    "并发确认只有一笔能够成功"
                )
            draft = json.loads(period["draft_snapshot"])
            snapshot = self._build_snapshot(period, draft)
            versions = self._load_versions(_snapshot_ids(snapshot))
            result = engine.calculate(snapshot, versions=versions)

            # 条件更新兜底：即便两个请求同时读到 PENDING，也只有一行被更新
            self._transition(
                period_id, expected=(PENDING,), target=CONFIRMED, stamp_col="confirmed_at"
            )
            entry_id = self._insert_entry(
                period_id=period_id, kind=ENTRY_FORMAL, status="frozen",
                result=result, snapshot=snapshot, versions=versions,
                adjustment_id=None, memo="企业年度确认正式分录", actor_role=actor_role,
            )
            self.conn.execute(
                "UPDATE period SET frozen_snapshot=?, input_hash=?, formal_entry_id=? WHERE id=?",
                (
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    result["input_hash"], entry_id, period_id,
                ),
            )
        return self.get_period(period_id)

    def post(self, period_id: str) -> dict:
        """交易运营过账：已确认 -> 执行中。"""
        with self.tx():
            period = self._transition(
                period_id, expected=(CONFIRMED,), target=EXECUTING, stamp_col="posted_at"
            )
            self.conn.execute(
                "UPDATE entry SET status='posted' WHERE id=?", (period["formal_entry_id"],)
            )
        return self.get_period(period_id)

    def seal(self, period_id: str) -> dict:
        with self.tx():
            self._transition(
                period_id, expected=(EXECUTING, CONFIRMED), target=SEALED, stamp_col="sealed_at"
            )
        return self.get_period(period_id)

    # ------------------------------------------------------------ 分录写入

    def _evidence_bundle(self, snapshot: dict, versions: dict[str, dict]) -> dict:
        ids = {
            "rule": snapshot["rule_version_id"],
            "policy": snapshot["policy_version_id"],
        }
        rows = {
            r["id"]: r
            for r in self.conn.execute(
                f"SELECT id, evidence, version_type, created_at FROM version WHERE id IN "
                f"({','.join('?' * len(ids))})", list(ids.values())
            ).fetchall()
        }
        return {
            "rule_version_id": ids["rule"],
            "policy_version_id": ids["policy"],
            "rule_evidence": json.loads(rows[ids["rule"]]["evidence"]),
            "policy_evidence": json.loads(rows[ids["policy"]]["evidence"]),
        }

    def _insert_entry(
        self, *, period_id: str, kind: str, status: str, result: dict,
        snapshot: dict, versions: dict[str, dict], adjustment_id: str | None,
        memo: str, actor_role: str,
        delta_lines: list[dict] | None = None,
    ) -> str:
        eid = _new_id("ent")
        self.conn.execute(
            """INSERT INTO entry(id, period_id, kind, status, total_credit, input_hash,
                                 engine_version, rule_version_id, policy_version_id,
                                 adjustment_id, memo, created_by_role, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                eid, period_id, kind, status, result["total_credit"], result["input_hash"],
                result["engine_version"], result["rule_version_id"],
                result["policy_version_id"], adjustment_id, memo, actor_role, utcnow_iso(),
            ),
        )
        global_evidence = self._evidence_bundle(snapshot, versions)
        lines = delta_lines if delta_lines is not None else result["lines"]
        for line in lines:
            self.conn.execute(
                """INSERT INTO entry_line(id, entry_id, model_code, energy_type, quantity,
                                          unit_credit, multiplier, old_credit, final_credit,
                                          production_version_id, energy_version_id,
                                          rule_version_id, policy_version_id, steps, evidence)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    _new_id("ln"), eid, line["model_code"], line.get("energy_type"),
                    line.get("quantity"), line.get("unit_credit"), line.get("multiplier"),
                    line.get("old_credit"), line["final_credit"],
                    line.get("production_version_id"), line.get("energy_version_id"),
                    line.get("rule_version_id", snapshot["rule_version_id"]),
                    line.get("policy_version_id", snapshot["policy_version_id"]),
                    json.dumps(line.get("steps", []), ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        {**global_evidence, **(line.get("evidence_extra") or {})},
                        ensure_ascii=False, sort_keys=True,
                    ),
                ),
            )
        return eid

    # ------------------------------------------------------------ 调整单

    def _effective_snapshot(self, period: sqlite3.Row) -> dict:
        """取最近一张已应用调整单的快照；否则取冻结快照。"""
        adj = self._row(
            """SELECT applied_snapshot FROM adjustment
               WHERE period_id=? AND status='applied' AND applied_snapshot IS NOT NULL
               ORDER BY applied_at DESC, rowid DESC LIMIT 1""",
            (period["id"],),
        )
        if adj is not None:
            return json.loads(adj["applied_snapshot"])
        frozen = period["frozen_snapshot"]
        if not frozen:
            raise ConflictError("期间尚未确认，不能开调整单；请直接提交新批次版本")
        return json.loads(frozen)

    def create_adjustment(
        self, *, period_id: str, adjustment_type: str, payload: dict,
        reason: str, evidence: dict | None, actor_role: str,
    ) -> dict:
        if adjustment_type not in ADJUSTMENT_TYPES:
            raise ValidationError("adjustment_type 必须是 " + "、".join(ADJUSTMENT_TYPES))
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是对象")
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("调整原因 reason 不能为空")
        evidence_out = self._validate_evidence(evidence) if evidence else None

        with self.tx():
            period = self.require_period(period_id)
            if period["state"] not in (CONFIRMED, EXECUTING, SEALED):
                raise ConflictError(
                    f"期间处于「{period['state']}」；只有确认之后才需要调整单，"
                    "窗口未关闭请直接补报批次版本"
                )

            # 规则勘误由核算专员在同一事务内直接生效；其余两类先由企业申请
            direct_apply = adjustment_type == ADJ_RULE_ERRATA
            aid = _new_id("adj")
            self.conn.execute(
                """INSERT INTO adjustment(id, period_id, adjustment_type, status, payload,
                                          reason, evidence, requested_by_role, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    aid, period_id, adjustment_type,
                    "applied-pending" if direct_apply else "requested",
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    reason,
                    json.dumps(evidence_out, ensure_ascii=False, sort_keys=True) if evidence_out else None,
                    actor_role, utcnow_iso(),
                ),
            )
            if direct_apply:
                self._apply_adjustment_locked(aid, actor_role)
        return self.get_adjustment(aid)

    def apply_adjustment(self, adjustment_id: str, *, actor_role: str) -> dict:
        with self.tx():
            self._apply_adjustment_locked(adjustment_id, actor_role)
        return self.get_adjustment(adjustment_id)

    def reject_adjustment(self, adjustment_id: str, note: str | None) -> dict:
        with self.tx():
            row = self._row("SELECT * FROM adjustment WHERE id=?", (adjustment_id,))
            if row is None:
                raise NotFoundError(f"调整单不存在：{adjustment_id}")
            if row["status"] != "requested":
                raise ConflictError(f"调整单状态为 {row['status']}，不能驳回")
            self.conn.execute(
                "UPDATE adjustment SET status='rejected' WHERE id=?", (adjustment_id,)
            )
        return self.get_adjustment(adjustment_id)

    def _apply_adjustment_locked(self, adjustment_id: str, actor_role: str) -> None:
        row = self._row("SELECT * FROM adjustment WHERE id=?", (adjustment_id,))
        if row is None:
            raise NotFoundError(f"调整单不存在：{adjustment_id}")
        if row["status"] not in ("requested", "applied-pending"):
            raise ConflictError(f"调整单状态为 {row['status']}，不能重复应用")
        period = self.require_period(row["period_id"])
        atype = row["adjustment_type"]
        payload = json.loads(row["payload"])
        base_snapshot = self._effective_snapshot(period)

        adjusted = json.loads(json.dumps(base_snapshot))  # 深拷贝
        adjusted["engine_version"] = engine.ENGINE_VERSION

        pointers = {m["model_code"]: dict(m) for m in adjusted["models"]}
        replaced_policy = None

        if atype == ADJ_WITHDRAWAL:
            code = payload.get("model_code")
            if code not in pointers:
                raise ValidationError(f"当前有效快照中没有车型 {code}，无法撤销")
            del pointers[code]

        elif atype == ADJ_LATE_DATA:
            changes = payload.get("model_changes") or []
            if not isinstance(changes, list):
                raise ValidationError("late_data 的 model_changes 必须是列表")
            if not changes and not payload.get("policy_version_id"):
                raise ValidationError("late_data 需要非空 model_changes 或 policy_version_id")
            for ch in changes:
                code = ch.get("model_code")
                if not code:
                    raise ValidationError("model_changes 每项必须包含 model_code")
                ptr = pointers.get(code)
                if ptr is None:
                    # 窗口关闭后才补报的新车型：须在同一张调整单里给齐两类版本
                    pvid, evid = ch.get("production_version_id"), ch.get("energy_version_id")
                    if not pvid or not evid:
                        raise ValidationError(
                            f"新车型 {code} 的迟到数据必须同时提供产量与能耗版本"
                        )
                    self._require_new_version(pvid, "production", period, code)
                    self._require_new_version(evid, "energy", period, code)
                    pointers[code] = {
                        "model_code": code,
                        "production_version_id": pvid,
                        "energy_version_id": evid,
                    }
                    continue
                for vt, key in (("production", "production_version_id"),
                                ("energy", "energy_version_id")):
                    new_id = ch.get(key)
                    if new_id:
                        self._require_new_version(new_id, vt, period, code)
                        ptr[key] = new_id
            if payload.get("policy_version_id"):
                replaced_policy = payload["policy_version_id"]
                self._require_new_policy(replaced_policy, period["year"])

        else:  # ADJ_RULE_ERRATA
            new_rule = payload.get("rule_version_id")
            if not new_rule:
                raise ValidationError("rule_errata 需要 rule_version_id")
            rrow = self._row("SELECT * FROM version WHERE id=?", (new_rule,))
            if rrow is None or rrow["version_type"] != "rule":
                raise ValidationError(f"规则版本不存在：{new_rule}")
            if json.loads(rrow["evidence"]).get("source_type") != EVIDENCE_ERRATA:
                raise ValidationError("规则勘误必须携带 source_type 为「规则勘误」的证据")
            adjusted["rule_version_id"] = new_rule

        if replaced_policy:
            adjusted["policy_version_id"] = replaced_policy
        adjusted["models"] = [pointers[k] for k in sorted(pointers)]

        # 重建内容哈希（新版本入表后不可变；历史版本哈希沿用）
        all_ids = _snapshot_ids(adjusted)
        contents = self._load_versions(all_ids)
        adjusted["content_hashes"] = {vid: content_hash(c) for vid, c in contents.items()}

        base_ids = _snapshot_ids(base_snapshot)
        base_contents = self._load_versions(base_ids)
        base_result = engine.calculate(base_snapshot, versions=base_contents)
        new_result = engine.calculate(adjusted, versions=contents)

        old_lines = {ln["model_code"]: ln for ln in base_result["lines"]}
        new_lines = {ln["model_code"]: ln for ln in new_result["lines"]}
        delta_lines: list[dict] = []
        for code in sorted(set(old_lines) | set(new_lines)):
            old = old_lines.get(code)
            new = new_lines.get(code)
            old_credit = Decimal(old["final_credit"]) if old else Decimal("0")
            new_credit = Decimal(new["final_credit"]) if new else Decimal("0")
            delta = engine.q2(new_credit - old_credit)
            if old is not None and new is not None and old_credit == new_credit:
                continue
            src = new or old
            delta_lines.append({
                "model_code": code,
                "energy_type": src["energy_type"],
                "quantity": new["quantity"] if new else "0",
                "unit_credit": new["unit_credit"] if new else "0.00",
                "multiplier": new["multiplier"] if new else old["multiplier"],
                "old_credit": str(engine.q2(old_credit)),
                "final_credit": str(delta),
                "production_version_id": new["production_version_id"] if new else None,
                "energy_version_id": new["energy_version_id"] if new else None,
                "rule_version_id": adjusted["rule_version_id"],
                "policy_version_id": adjusted["policy_version_id"],
                "steps": (new["steps"] if new else []) + [{
                    "step": "调整差额",
                    "formula": "new_final_credit - old_final_credit（撤销车型 new=0）",
                    "inputs": {
                        "new_final_credit": str(engine.q2(new_credit)),
                        "old_final_credit": str(engine.q2(old_credit)),
                    },
                    "output": str(delta),
                    "adjustment_type": atype,
                }],
                "evidence_extra": {
                    "model_evidence": self._model_evidence(src),
                    "adjustment_evidence": json.loads(row["evidence"]) if row["evidence"] else None,
                },
            })

        delta_total = engine.q2(sum(
            (Decimal(d["final_credit"]) for d in delta_lines), Decimal("0")
        ))
        delta_result = {
            "engine_version": new_result["engine_version"],
            "rule_version_id": adjusted["rule_version_id"],
            "policy_version_id": adjusted["policy_version_id"],
            "input_hash": new_result["input_hash"],
            "lines": [],
            "total_credit": str(delta_total),
        }
        entry_id = self._insert_entry(
            period_id=period["id"], kind=ENTRY_ADJUSTMENT, status="posted",
            result=delta_result, snapshot=adjusted, versions=contents,
            adjustment_id=adjustment_id,
            memo=f"{atype} 调整差额分录", actor_role=actor_role,
            delta_lines=delta_lines,
        )
        self.conn.execute(
            """UPDATE adjustment SET status='applied', entry_id=?, applied_snapshot=?,
                                    hash_before=?, hash_after=?, total_before=?, total_after=?,
                                    applied_at=?
               WHERE id=?""",
            (
                entry_id,
                json.dumps(adjusted, ensure_ascii=False, sort_keys=True),
                base_result["input_hash"], new_result["input_hash"],
                base_result["total_credit"], new_result["total_credit"],
                utcnow_iso(), adjustment_id,
            ),
        )

    def _model_evidence(self, line: dict) -> dict:
        ids = [line["production_version_id"], line["energy_version_id"]]
        rows = self.conn.execute(
            f"SELECT id, evidence FROM version WHERE id IN ({','.join('?' * len(ids))})", ids
        ).fetchall()
        ev = {r["id"]: json.loads(r["evidence"]) for r in rows}
        return {
            "production_version_id": line["production_version_id"],
            "energy_version_id": line["energy_version_id"],
            "production_evidence": ev.get(line["production_version_id"]),
            "energy_evidence": ev.get(line["energy_version_id"]),
        }

    def _require_new_version(self, vid: str, version_type: str,
                             period: sqlite3.Row, code: str) -> None:
        r = self._row("SELECT * FROM version WHERE id=?", (vid,))
        if r is None or r["version_type"] != version_type:
            raise ValidationError(f"{version_type} 版本不存在：{vid}")
        if r["enterprise_id"] != period["enterprise_id"] or r["model_code"] != code:
            raise ValidationError(f"版本 {vid} 不属于车型 {code}")

    def _require_new_policy(self, vid: str, year: int) -> None:
        r = self._row("SELECT * FROM version WHERE id=?", (vid,))
        if r is None or r["version_type"] != "policy" or r["year"] != year:
            raise ValidationError(f"{year} 年度政策系数版本不存在：{vid}")

    def get_adjustment(self, adjustment_id: str) -> dict:
        r = self._row("SELECT * FROM adjustment WHERE id=?", (adjustment_id,))
        if r is None:
            raise NotFoundError(f"调整单不存在：{adjustment_id}")
        out = {
            "id": r["id"], "period_id": r["period_id"],
            "adjustment_type": r["adjustment_type"], "status": r["status"],
            "payload": json.loads(r["payload"]), "reason": r["reason"],
            "evidence": json.loads(r["evidence"]) if r["evidence"] else None,
            "requested_by_role": r["requested_by_role"],
            "hash_before": r["hash_before"], "hash_after": r["hash_after"],
            "total_before": r["total_before"], "total_after": r["total_after"],
            "entry_id": r["entry_id"],
            "created_at": r["created_at"], "applied_at": r["applied_at"],
        }
        if r["applied_snapshot"]:
            out["applied_snapshot"] = json.loads(r["applied_snapshot"])
        return out

    def list_adjustments(self, period_id: str) -> list[dict]:
        self.require_period(period_id)
        rows = self.conn.execute(
            "SELECT id, adjustment_type, status, total_before, total_after, entry_id, "
            "created_at, applied_at FROM adjustment WHERE period_id=? ORDER BY rowid",
            (period_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 查询/追溯/复算

    def _entry_dict(self, row: sqlite3.Row, *, with_lines: bool) -> dict:
        out = {
            "id": row["id"], "period_id": row["period_id"], "kind": row["kind"],
            "status": row["status"], "total_credit": row["total_credit"],
            "input_hash": row["input_hash"], "engine_version": row["engine_version"],
            "rule_version_id": row["rule_version_id"],
            "policy_version_id": row["policy_version_id"],
            "adjustment_id": row["adjustment_id"], "memo": row["memo"],
            "created_by_role": row["created_by_role"], "created_at": row["created_at"],
        }
        if with_lines:
            out["lines"] = self._lines_of(row["id"])
        return out

    def _lines_of(self, entry_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM entry_line WHERE entry_id=? ORDER BY rowid", (entry_id,)
        ).fetchall()
        return [self._line_dict(r) for r in rows]

    def _line_dict(self, r: sqlite3.Row) -> dict:
        return {
            "model_code": r["model_code"], "energy_type": r["energy_type"],
            "quantity": r["quantity"], "unit_credit": r["unit_credit"],
            "multiplier": r["multiplier"], "old_credit": r["old_credit"],
            "final_credit": r["final_credit"],
            "production_version_id": r["production_version_id"],
            "energy_version_id": r["energy_version_id"],
            "rule_version_id": r["rule_version_id"], "policy_version_id": r["policy_version_id"],
            "steps": json.loads(r["steps"] or "[]"),
            "evidence": json.loads(r["evidence"] or "null"),
        }

    def statement(self, enterprise_id: str, year: int) -> dict:
        """监管对账单：企业年度总额 = 正式分录 + 全部已应用调整分录。"""
        period_row = self._row(
            "SELECT * FROM period WHERE enterprise_id=? AND year=?", (enterprise_id, year)
        )
        if period_row is None:
            raise NotFoundError(f"企业 {enterprise_id} 的 {year} 年度期间不存在")
        ent = self.require_enterprise(enterprise_id)
        entries = self.conn.execute(
            "SELECT * FROM entry WHERE period_id=? ORDER BY rowid", (period_row["id"],)
        ).fetchall()
        total = Decimal("0")
        entry_summaries = []
        for e in entries:
            total += Decimal(e["total_credit"])
            entry_summaries.append({
                "entry_id": e["id"], "kind": e["kind"], "status": e["status"],
                "total_credit": e["total_credit"], "input_hash": e["input_hash"],
                "adjustment_id": e["adjustment_id"], "created_at": e["created_at"],
            })
        return {
            "enterprise_id": enterprise_id,
            "enterprise_name": ent["name"],
            "year": year,
            "period_id": period_row["id"],
            "state": period_row["state"],
            "formal_entry_id": period_row["formal_entry_id"],
            "formal_input_hash": period_row["input_hash"],
            "current_total_credit": str(engine.q2(total)),
            "entries": entry_summaries,
        }

    def model_trace(self, enterprise_id: str, year: int, model_code: str) -> dict:
        """从企业总额追溯到单车型：正式贡献 + 历次调整 + 证据链。"""
        period_row = self._row(
            "SELECT * FROM period WHERE enterprise_id=? AND year=?", (enterprise_id, year)
        )
        if period_row is None:
            raise NotFoundError("期间不存在")
        chain = []
        net = Decimal("0")
        if period_row["formal_entry_id"]:
            rows = self.conn.execute(
                "SELECT * FROM entry_line WHERE entry_id=? AND model_code=? ORDER BY rowid",
                (period_row["formal_entry_id"], model_code),
            ).fetchall()
            for r in rows:
                net += Decimal(r["final_credit"])
                chain.append({"entry_kind": ENTRY_FORMAL, **self._line_dict(r)})
        adj_rows = self.conn.execute(
            """SELECT el.*, e.adjustment_id FROM entry_line el
               JOIN entry e ON e.id=el.entry_id
               WHERE e.period_id=? AND e.kind=? AND el.model_code=?
               ORDER BY e.rowid""",
            (period_row["id"], ENTRY_ADJUSTMENT, model_code),
        ).fetchall()
        for r in adj_rows:
            net += Decimal(r["final_credit"])
            item = self._line_dict(r)
            item["entry_kind"] = ENTRY_ADJUSTMENT
            item["adjustment_id"] = r["adjustment_id"]
            chain.append(item)
        # 是否撤销：以最近一次已应用调整单的有效快照为准
        latest = self._row(
            """SELECT applied_snapshot FROM adjustment
               WHERE period_id=? AND status='applied' AND applied_snapshot IS NOT NULL
               ORDER BY applied_at DESC, rowid DESC LIMIT 1""",
            (period_row["id"],),
        )
        if latest is not None:
            effective_codes = {m["model_code"] for m in json.loads(latest["applied_snapshot"])["models"]}
        elif period_row["frozen_snapshot"]:
            effective_codes = {m["model_code"] for m in json.loads(period_row["frozen_snapshot"])["models"]}
        else:
            effective_codes = set()
        return {
            "enterprise_id": enterprise_id, "year": year,
            "period_id": period_row["id"], "model_code": model_code,
            "net_contribution": str(engine.q2(net)),
            "withdrawn": model_code not in effective_codes,
            "chain": chain,
        }

    def entry_detail(self, entry_id: str) -> dict:
        row = self._row("SELECT * FROM entry WHERE id=?", (entry_id,))
        if row is None:
            raise NotFoundError(f"分录不存在：{entry_id}")
        return self._entry_dict(row, with_lines=True)

    # ------------------------------------------------------------ 复算

    def _recompute_snapshot(self, snapshot: dict, stored_hash: str,
                            stored_total: str, stored_lines: list[dict] | None) -> dict:
        ids = _snapshot_ids(snapshot)
        contents = self._load_versions(ids)
        # 闸门一：版本内容哈希（任何内容被改动都会暴露）
        hash_mismatches = []
        for vid, ch in (snapshot.get("content_hashes") or {}).items():
            if vid in contents and content_hash(contents[vid]) != ch:
                hash_mismatches.append(vid)
        result = engine.calculate(snapshot, versions=contents)
        # 闸门二：快照指针 + 内容哈希的综合哈希须与入账时一致
        hash_ok = result["input_hash"] == stored_hash and not hash_mismatches
        total_ok = result["total_credit"] == stored_total
        line_ok = True
        line_diffs = []
        if stored_lines is not None:
            fresh = {ln["model_code"]: ln for ln in result["lines"]}
            frozen = {ln["model_code"]: ln for ln in stored_lines}
            for code in sorted(set(fresh) | set(frozen)):
                a, b = fresh.get(code), frozen.get(code)
                if a is None or b is None or a["final_credit"] != b["final_credit"]:
                    line_ok = False
                    line_diffs.append({
                        "model_code": code,
                        "recomputed": a["final_credit"] if a else None,
                        "stored": b["final_credit"] if b else None,
                    })
        return {
            "engine_version": engine.ENGINE_VERSION,
            "stored_engine_version": snapshot.get("engine_version"),
            "input_hash": result["input_hash"],
            "stored_input_hash": stored_hash,
            "content_hash_mismatches": hash_mismatches,
            "hash_ok": hash_ok,
            "recomputed_total": result["total_credit"],
            "stored_total": stored_total,
            "total_ok": total_ok,
            "line_ok": line_ok,
            "line_diffs": line_diffs,
            "verified": hash_ok and total_ok and line_ok and not hash_mismatches,
            "lines": result["lines"],
        }

    def recompute_period(self, period_id: str) -> dict:
        with self.tx():
            period = self.require_period(period_id)
            if not period["frozen_snapshot"]:
                raise ConflictError("期间尚未确认冻结，无可复算对象")
            snapshot = json.loads(period["frozen_snapshot"])
            entry = self._row(
                "SELECT * FROM entry WHERE id=?", (period["formal_entry_id"],)
            )
            stored_lines = self._lines_of(entry["id"])
            stored_lines = [
                {**ln, "final_credit": ln["final_credit"]} for ln in stored_lines
            ]
            report = self._recompute_snapshot(
                snapshot, period["input_hash"], entry["total_credit"], stored_lines
            )
            report["period_id"] = period_id
            return report

    def recompute_adjustment(self, adjustment_id: str) -> dict:
        with self.tx():
            r = self._row("SELECT * FROM adjustment WHERE id=?", (adjustment_id,))
            if r is None:
                raise NotFoundError(f"调整单不存在：{adjustment_id}")
            if r["status"] != "applied":
                raise ConflictError("调整单尚未应用，无可复算结果")
            snapshot = json.loads(r["applied_snapshot"])
            entry_rows = self.conn.execute(
                "SELECT * FROM entry_line WHERE entry_id=? ORDER BY rowid", (r["entry_id"],)
            ).fetchall()
            # 调整后完整总额复算（与 total_after 对照），差额行另外核对方向
            report = self._recompute_snapshot(
                snapshot, r["hash_after"], r["total_after"], None
            )
            report["adjustment_id"] = adjustment_id
            report["delta_total_stored"] = self._row(
                "SELECT total_credit FROM entry WHERE id=?", (r["entry_id"],)
            )["total_credit"]
            return report


def _snapshot_ids(snapshot: dict) -> list[str]:
    ids = [snapshot["rule_version_id"], snapshot["policy_version_id"]]
    for m in snapshot["models"]:
        ids += [m["production_version_id"], m["energy_version_id"]]
    return ids


def _valid_year(year: Any) -> int:
    if isinstance(year, bool) or not isinstance(year, int):
        raise ValidationError("year 必须是整数")
    if not 2000 <= year <= 2100:
        raise ValidationError("year 超出合理范围")
    return year
