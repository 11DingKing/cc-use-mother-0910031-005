"""JSON 文件持久化 + 跨线程/跨进程文件锁。

设计要点：
- 车型版本、规则版本是只追加的不可变记录（append-only）；
- 每个企业年度申报单是一个聚合，确认时对聚合行加锁做状态检查，
  防止并发重复确认（第二次拿到锁后发现已确认即冲突）；
- 正式分录、调整单只追加，任何确认后的变更都以新行体现，
  历史行永不修改或删除。
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .canonical import content_hash
from .engine import LedgerEntry
from .errors import ConflictError, NotFoundError
from .models import (
    AdjustmentKind,
    CalculationRule,
    Evidence,
    FilingState,
    ModelStatus,
    VehicleModelVersion,
    parse_ts,
    utcnow,
)


class FileLock:
    """跨进程 fcntl 锁 + 进程内可重入互斥锁。"""

    def __init__(self, path: Path):
        self._path = path
        self._thread_lock = threading.RLock()
        self._fh: Any = None

    def __enter__(self) -> "FileLock":
        self._thread_lock.acquire()
        try:
            import fcntl

            self._fh = open(self._path, "a+")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            self._thread_lock.release()
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        import fcntl

        try:
            if self._fh is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
        finally:
            self._fh = None
            self._thread_lock.release()


@dataclass
class Filing:
    """企业 × 年度申报聚合（可变状态，仅在锁内修改）。"""

    filing_id: str
    enterprise_id: str
    year: int
    state: FilingState = FilingState.DRAFT
    basis_model_versions: dict[str, int] = field(default_factory=dict)
    basis_rule_version: int | None = None
    draft_report: dict[str, Any] | None = None
    confirmed_report: dict[str, Any] | None = None
    confirmed_input_fingerprint: str | None = None
    confirmed_report_fingerprint: str | None = None
    confirmed_at: str | None = None
    confirmed_total: Decimal | None = None
    adjustment_seqs: list[int] = field(default_factory=list)
    rev: int = 0          # 乐观锁版本号
    created_at: str = field(default_factory=lambda: utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "filing_id": self.filing_id,
            "enterprise_id": self.enterprise_id,
            "year": self.year,
            "state": self.state.value,
            "basis_model_versions": dict(self.basis_model_versions),
            "basis_rule_version": self.basis_rule_version,
            "draft_report": self.draft_report,
            "confirmed_report": self.confirmed_report,
            "confirmed_input_fingerprint": self.confirmed_input_fingerprint,
            "confirmed_report_fingerprint": self.confirmed_report_fingerprint,
            "confirmed_at": self.confirmed_at,
            "confirmed_total": str(self.confirmed_total) if self.confirmed_total is not None else None,
            "adjustment_seqs": list(self.adjustment_seqs),
            "rev": self.rev,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Filing":
        return cls(
            filing_id=data["filing_id"],
            enterprise_id=data["enterprise_id"],
            year=int(data["year"]),
            state=FilingState(data["state"]),
            basis_model_versions=dict(data.get("basis_model_versions") or {}),
            basis_rule_version=data.get("basis_rule_version"),
            draft_report=data.get("draft_report"),
            confirmed_report=data.get("confirmed_report"),
            confirmed_input_fingerprint=data.get("confirmed_input_fingerprint"),
            confirmed_report_fingerprint=data.get("confirmed_report_fingerprint"),
            confirmed_at=data.get("confirmed_at"),
            confirmed_total=Decimal(data["confirmed_total"]) if data.get("confirmed_total") is not None else None,
            adjustment_seqs=[int(x) for x in data.get("adjustment_seqs", [])],
            rev=int(data.get("rev", 0)),
            created_at=data.get("created_at", utcnow().isoformat()),
        )


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return default
    return json.loads(raw)


def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


class Store:
    """聚合所有持久化访问。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "filings").mkdir(parents=True, exist_ok=True)
        (self.root / "ledger").mkdir(parents=True, exist_ok=True)
        (self.root / "adjustments").mkdir(parents=True, exist_ok=True)
        self.models_path = self.root / "models.json"
        self.rules_path = self.root / "rules.json"
        self._master_lock = FileLock(self.root / ".master.lock")
        self._filing_locks: dict[str, FileLock] = {}

    # ----------------------------------------------------------- 底层锁

    @contextlib.contextmanager
    def master_lock(self) -> Iterator[None]:
        with self._master_lock:
            yield

    def filing_lock(self, filing_id: str) -> FileLock:
        # 字典本身只在持锁线程内改动；GIL 下 setdefault 原子。
        return self._filing_locks.setdefault(
            filing_id, FileLock(self.root / "filings" / f"{filing_id}.lock"))

    def _filing_path(self, filing_id: str) -> Path:
        return self.root / "filings" / f"{filing_id}.json"

    def _all_models(self) -> dict[str, list[dict[str, Any]]]:
        """每次读盘，保证多进程下能看到其他进程追加的新版本。"""
        return _read_json(self.models_path, {})

    def _all_rules(self) -> dict[str, list[dict[str, Any]]]:
        return _read_json(self.rules_path, {})

    # ------------------------------------------------------ 车型参数版本

    def add_model_version(
        self,
        *,
        model_id: str,
        enterprise_id: str,
        name: str,
        year: int,
        volume: int,
        energy: Decimal,
        attrs: dict[str, str] | None,
        status: ModelStatus,
        evidence: Evidence,
    ) -> VehicleModelVersion:
        if volume < 0:
            raise ValueError("产量不能为负")
        with self.master_lock():
            models = self._all_models()
            versions = models.setdefault(model_id, [])
            if any(v["year"] != year for v in versions):
                raise ValueError("同一车型的所有版本必须适用同一年度")
            version = len(versions) + 1
            supersedes = versions[-1]["version"] if versions else None
            fingerprint = content_hash({
                "model_id": model_id,
                "enterprise_id": enterprise_id,
                "name": name,
                "year": year,
                "volume": volume,
                "energy": str(energy.normalize()),
                "attrs": attrs or {},
                "status": status.value,
                "evidence": evidence.to_dict(),
            })
            record = VehicleModelVersion(
                model_id=model_id,
                version=version,
                enterprise_id=enterprise_id,
                name=name,
                year=year,
                volume=volume,
                energy=energy,
                attrs=dict(attrs or {}),
                status=status,
                evidence=evidence,
                supersedes=supersedes,
                created_at=utcnow(),
                content_fingerprint=fingerprint,
            )
            versions.append(record.to_dict())
            _write_json(self.models_path, models)
            return record

    def get_model_version(self, model_id: str, version: int | None = None) -> VehicleModelVersion:
        versions = self._all_models().get(model_id)
        if not versions:
            raise NotFoundError(f"车型不存在：{model_id}")
        if version is None:
            data = versions[-1]
        else:
            try:
                data = next(v for v in versions if v["version"] == version)
            except StopIteration:
                raise NotFoundError(f"车型 {model_id} 版本 {version} 不存在") from None
        return VehicleModelVersion.from_dict(data)

    def list_model_versions(self, model_id: str) -> list[VehicleModelVersion]:
        versions = self._all_models().get(model_id)
        if not versions:
            raise NotFoundError(f"车型不存在：{model_id}")
        return [VehicleModelVersion.from_dict(v) for v in versions]

    def latest_model_version_number(self, model_id: str) -> int:
        versions = self._all_models().get(model_id)
        if not versions:
            raise NotFoundError(f"车型不存在：{model_id}")
        return versions[-1]["version"]

    # -------------------------------------------------------- 核算规则版本

    def add_rule_version(
        self,
        *,
        year: int,
        policy_coef: Decimal,
        rate: Decimal,
        intercept: Decimal,
        note: str,
        evidence: Evidence,
    ) -> CalculationRule:
        with self.master_lock():
            rules = self._all_rules()
            versions = rules.setdefault(str(year), [])
            version = len(versions) + 1
            supersedes = versions[-1]["version"] if versions else None
            fingerprint = content_hash({
                "year": year,
                "policy_coef": str(policy_coef.normalize()),
                "rate": str(rate.normalize()),
                "intercept": str(intercept.normalize()),
                "note": note,
                "evidence": evidence.to_dict(),
            })
            record = CalculationRule(
                year=year,
                version=version,
                policy_coef=policy_coef,
                rate=rate,
                intercept=intercept,
                note=note,
                evidence=evidence,
                supersedes=supersedes,
                created_at=utcnow(),
                content_fingerprint=fingerprint,
            )
            versions.append(record.to_dict())
            _write_json(self.rules_path, rules)
            return record

    def get_rule(self, year: int, version: int | None = None) -> CalculationRule:
        versions = self._all_rules().get(str(year))
        if not versions:
            raise NotFoundError(f"年度 {year} 尚无核算规则")
        if version is None:
            data = versions[-1]
        else:
            try:
                data = next(v for v in versions if v["version"] == version)
            except StopIteration:
                raise NotFoundError(f"年度 {year} 规则版本 {version} 不存在") from None
        return CalculationRule.from_dict(data)

    def latest_rule_version_number(self, year: int) -> int:
        versions = self._all_rules().get(str(year))
        if not versions:
            raise NotFoundError(f"年度 {year} 尚无核算规则")
        return versions[-1]["version"]

    def list_rules(self, year: int) -> list[CalculationRule]:
        versions = self._all_rules().get(str(year)) or []
        return [CalculationRule.from_dict(v) for v in versions]

    # ------------------------------------------------------------- 申报单

    def create_filing(self, enterprise_id: str, year: int) -> Filing:
        filing_id = f"filing-{enterprise_id}-{year}"
        with self.filing_lock(filing_id):
            path = self._filing_path(filing_id)
            if path.exists():
                raise ConflictError(f"{year} 年度申报单已存在：{filing_id}")
            filing = Filing(filing_id=filing_id, enterprise_id=enterprise_id, year=year)
            _write_json(path, filing.to_dict())
            return filing

    def get_filing(self, filing_id: str) -> Filing:
        path = self._filing_path(filing_id)
        if not path.exists():
            raise NotFoundError(f"申报单不存在：{filing_id}")
        return Filing.from_dict(_read_json(path, {}))

    def save_filing(self, filing: Filing, *, expected_rev: int | None = None) -> None:
        """乐观保存：expected_rev 不匹配说明期间被并发修改。

        调用方在涉及状态流转时必须持有 filing_lock；本方法仅做落盘前的
        版本号复核，捕获“读-改-写”窗口内的并发修改。
        """
        path = self._filing_path(filing.filing_id)
        current = Filing.from_dict(_read_json(path, {})) if path.exists() else None
        if expected_rev is not None and current is not None and current.rev != expected_rev:
            raise ConflictError(
                f"申报单 {filing.filing_id} 已被并发修改（版本 {expected_rev}→{current.rev}）")
        filing.rev = (current.rev if current else 0) + 1
        _write_json(path, filing.to_dict())

    def list_filings(self, enterprise_id: str | None = None) -> list[Filing]:
        result = []
        for path in sorted((self.root / "filings").glob("filing-*.json")):
            filing = Filing.from_dict(_read_json(path, {}))
            if enterprise_id is None or filing.enterprise_id == enterprise_id:
                result.append(filing)
        return result

    # ------------------------------------------------------------- 正式分录

    def _ledger_path(self, filing_id: str) -> Path:
        return self.root / "ledger" / f"{filing_id}.json"

    def append_entries(self, filing_id: str, entries: list[LedgerEntry]) -> None:
        """追加正式分录（幂等）。调用方必须持有 filing_lock。"""
        if not entries:
            return
        path = self._ledger_path(filing_id)
        rows: list[dict[str, Any]] = _read_json(path, [])
        existing = {row["entry_id"] for row in rows}
        # 幂等：并发重试时同一分录不产生重复行。
        rows.extend(e.to_dict() for e in entries if e.entry_id not in existing)
        _write_json(path, rows)

    def list_entries(self, filing_id: str) -> list[LedgerEntry]:
        rows = _read_json(self._ledger_path(filing_id), [])
        return [LedgerEntry.from_dict(row) for row in rows]

    # --------------------------------------------------------------- 调整单

    def _adjustment_path(self, adjustment_id: str) -> Path:
        return self.root / "adjustments" / f"{adjustment_id}.json"

    def _adjustment_index_path(self, filing_id: str) -> Path:
        return self.root / "adjustments" / f"{filing_id}.index.json"

    def save_adjustment(self, order: Any) -> None:
        """持久化调整单（幂等）。调用方必须持有 filing_lock。"""
        index_path = self._adjustment_index_path(order.filing_id)
        index: list[str] = _read_json(index_path, [])
        if order.adjustment_id in index:
            return  # 幂等
        _write_json(self._adjustment_path(order.adjustment_id), order.to_dict())
        index.append(order.adjustment_id)
        _write_json(index_path, index)

    def list_adjustments(self, filing_id: str) -> list[Any]:
        index_path = self._adjustment_index_path(filing_id)
        ids: list[str] = _read_json(index_path, [])
        orders = []
        for adjustment_id in ids:
            data = _read_json(self._adjustment_path(adjustment_id), None)
            if data is not None:
                orders.append(load_adjustment_order(data))
        return orders


def load_adjustment_order(data: dict[str, Any]) -> Any:
    from .engine import AdjustmentOrder

    return AdjustmentOrder.from_dict(data)
