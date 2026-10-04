"""领域模型：全部不可变（frozen dataclass）。

任何历史版本一经建立都不可修改；迟到数据、车型撤销、规则勘误
只能产生新版本或调整单。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    """统一使用 UTC 感知时间戳。"""
    return datetime.now(timezone.utc)


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class FilingState(str, Enum):
    """企业年度申报单状态。"""

    DRAFT = "草稿"          # 可反复试算，输入可改
    CONFIRMED = "已确认"    # 输入快照已冻结，正式分录已生成
    FROZEN = "已封存"       # 年度封账，此后只允许调整单


class ModelStatus(str, Enum):
    """车型版本状态。"""

    ACTIVE = "有效"
    REVOKED = "撤销"


class AdjustmentKind(str, Enum):
    """调整单类型，覆盖四种只能事后处理的情形。"""

    LATE_DATA = "迟到数据"
    MODEL_REVOKED = "车型撤销"
    RULE_CORRECTION = "规则勘误"
    MANUAL = "人工更正"


# ---------------------------------------------------------------- 证据来源

@dataclass(frozen=True)
class Evidence:
    """证据来源：任何参数都必须可回溯到原始凭证。"""

    source: str               # 来源系统或文件，如 工信部批次文件-2025-R3
    batch: str                # 数据批次
    reference: str            # 原始凭证编号 / URL / 公文号
    submitted_at: datetime
    payload_hash: str         # 原始报文指纹

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "batch": self.batch,
            "reference": self.reference,
            "submitted_at": self.submitted_at.isoformat(),
            "payload_hash": self.payload_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        return cls(
            source=data["source"],
            batch=data["batch"],
            reference=data["reference"],
            submitted_at=parse_ts(data["submitted_at"]),
            payload_hash=data["payload_hash"],
        )


# ------------------------------------------------------- 车型参数（版本化）

@dataclass(frozen=True)
class VehicleModelVersion:
    """车型参数的一个不可变版本。

    volume 为年度产量；energy 为能耗参数（如 WLTC 油耗 L/100km，
    或纯电能耗 kWh/100km，由核算规则解释）；
    attrs 存放动力类型等额外结构化属性，参与快照指纹。
    """

    model_id: str
    version: int
    enterprise_id: str
    name: str
    year: int
    volume: int
    energy: Decimal
    attrs: dict[str, str] = field(default_factory=dict)
    status: ModelStatus = ModelStatus.ACTIVE
    evidence: Evidence | None = None
    supersedes: int | None = None
    created_at: datetime = field(default_factory=utcnow)
    content_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "version": self.version,
            "enterprise_id": self.enterprise_id,
            "name": self.name,
            "year": self.year,
            "volume": self.volume,
            "energy": str(self.energy),
            "attrs": dict(self.attrs),
            "status": self.status.value,
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "supersedes": self.supersedes,
            "created_at": self.created_at.isoformat(),
            "content_fingerprint": self.content_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VehicleModelVersion":
        return cls(
            model_id=data["model_id"],
            version=data["version"],
            enterprise_id=data["enterprise_id"],
            name=data["name"],
            year=data["year"],
            volume=int(data["volume"]),
            energy=Decimal(data["energy"]),
            attrs=dict(data.get("attrs") or {}),
            status=ModelStatus(data["status"]),
            evidence=Evidence.from_dict(data["evidence"]) if data.get("evidence") else None,
            supersedes=data.get("supersedes"),
            created_at=parse_ts(data["created_at"]),
            content_fingerprint=data.get("content_fingerprint", ""),
        )


# ------------------------------------------------------- 核算规则（版本化）

@dataclass(frozen=True)
class CalculationRule:
    """核算规则的一个不可变版本。

    采用可审计的线性模型：单车积分 = policy_coef - rate * energy + intercept，
    车型贡献 = 单车积分 * volume。
    规则勘误时发布新版本，旧版本永久保留，已确认分录不被静默改写。
    """

    year: int
    version: int
    policy_coef: Decimal
    rate: Decimal
    intercept: Decimal = Decimal("0")
    note: str = ""
    evidence: Evidence | None = None
    supersedes: int | None = None
    created_at: datetime = field(default_factory=utcnow)
    content_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "version": self.version,
            "policy_coef": str(self.policy_coef),
            "rate": str(self.rate),
            "intercept": str(self.intercept),
            "note": self.note,
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "supersedes": self.supersedes,
            "created_at": self.created_at.isoformat(),
            "content_fingerprint": self.content_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalculationRule":
        return cls(
            year=int(data["year"]),
            version=int(data["version"]),
            policy_coef=Decimal(data["policy_coef"]),
            rate=Decimal(data["rate"]),
            intercept=Decimal(data.get("intercept", "0")),
            note=data.get("note", ""),
            evidence=Evidence.from_dict(data["evidence"]) if data.get("evidence") else None,
            supersedes=data.get("supersedes"),
            created_at=parse_ts(data["created_at"]),
            content_fingerprint=data.get("content_fingerprint", ""),
        )
