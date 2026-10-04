"""确定性核算引擎。

所有金额使用 Decimal，舍入规则固定（ROUND_HALF_UP），车型按 model_id
排序；计算结果只取决于显式传入的车型版本与规则版本，与时间、调用顺序、
字典迭代顺序无关 —— 相同输入必然得到相同报告与相同指纹。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from .canonical import canonical, content_hash
from .models import (
    AdjustmentKind,
    CalculationRule,
    Evidence,
    ModelStatus,
    VehicleModelVersion,
)

ENGINE_VERSION = "1.0.0"

UNIT_Q = Decimal("0.0001")   # 单车积分精度
CREDIT_Q = Decimal("0.01")   # 积分分录精度


def qunit(value: Decimal) -> Decimal:
    return value.quantize(UNIT_Q, rounding=ROUND_HALF_UP)


def qcredit(value: Decimal) -> Decimal:
    return value.quantize(CREDIT_Q, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class TrialLine:
    """单车型试算明细，含可解释公式与证据指针。"""

    model_id: str
    name: str
    model_version: int
    volume: int
    energy: Decimal
    unit_credit: Decimal
    contribution: Decimal
    included: bool
    reason: str
    model_fingerprint: str
    evidence: Evidence | None
    formula: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "name": self.name,
            "model_version": self.model_version,
            "volume": self.volume,
            "energy": str(self.energy),
            "unit_credit": str(self.unit_credit),
            "contribution": str(self.contribution),
            "included": self.included,
            "reason": self.reason,
            "formula": self.formula,
            "model_fingerprint": self.model_fingerprint,
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }


@dataclass(frozen=True)
class TrialReport:
    """可解释试算报告。指纹只覆盖输入与结果，不覆盖生成时间。"""

    enterprise_id: str
    year: int
    rule_version: int
    rule_fingerprint: str
    engine_version: str
    lines: tuple[TrialLine, ...]
    total: Decimal
    input_fingerprint: str
    report_fingerprint: str

    def contribution_by_model(self) -> dict[str, Decimal]:
        return {line.model_id: line.contribution for line in self.lines}

    def to_dict(self) -> dict[str, Any]:
        return {
            "enterprise_id": self.enterprise_id,
            "year": self.year,
            "rule_version": self.rule_version,
            "rule_fingerprint": self.rule_fingerprint,
            "engine_version": self.engine_version,
            "total": str(self.total),
            "input_fingerprint": self.input_fingerprint,
            "report_fingerprint": self.report_fingerprint,
            "lines": [line.to_dict() for line in self.lines],
        }


def _line_for(model: VehicleModelVersion, rule: CalculationRule) -> TrialLine:
    formula_revoked = (
        f"车型已撤销（参数版本 v{model.version}）：贡献计 0；"
        f"原计算口径 单车积分 = {rule.policy_coef} − {rule.rate} × {model.energy}"
        + (f" + {rule.intercept}" if rule.intercept else "")
    )
    if model.status is ModelStatus.REVOKED:
        return TrialLine(
            model_id=model.model_id,
            name=model.name,
            model_version=model.version,
            volume=model.volume,
            energy=model.energy,
            unit_credit=Decimal("0.0000"),
            contribution=Decimal("0.00"),
            included=False,
            reason="车型已撤销，贡献计 0",
            model_fingerprint=model.content_fingerprint,
            evidence=model.evidence,
            formula=formula_revoked,
        )
    unit = qunit(rule.policy_coef - rule.rate * model.energy + rule.intercept)
    contribution = qcredit(unit * model.volume)
    formula = (
        f"单车积分 = {rule.policy_coef} − {rule.rate} × {model.energy}"
        + (f" + {rule.intercept}" if rule.intercept else "")
        + f" = {unit}（规则 v{rule.version}）；"
        f"车型贡献 = {unit} × 产量 {model.volume} = {contribution}"
    )
    return TrialLine(
        model_id=model.model_id,
        name=model.name,
        model_version=model.version,
        volume=model.volume,
        energy=model.energy,
        unit_credit=unit,
        contribution=contribution,
        included=True,
        reason="正常计入",
        model_fingerprint=model.content_fingerprint,
        evidence=model.evidence,
        formula=formula,
    )


def run_trial(
    enterprise_id: str,
    year: int,
    models: Iterable[VehicleModelVersion],
    rule: CalculationRule,
) -> TrialReport:
    """对一组显式指定版本的车型与规则执行试算。"""
    lines = tuple(sorted((_line_for(m, rule) for m in models), key=lambda x: x.model_id))
    # 总额由已舍入的明细求和，保证“总额 = 明细分录之和”。
    total = qcredit(sum((line.contribution for line in lines), Decimal("0")))

    input_material = {
        "enterprise_id": enterprise_id,
        "year": year,
        "engine_version": ENGINE_VERSION,
        "rule": {
            "version": rule.version,
            "fingerprint": rule.content_fingerprint,
        },
        "models": [
            {"model_id": line.model_id, "version": line.model_version,
             "fingerprint": line.model_fingerprint}
            for line in lines
        ],
    }
    input_fingerprint = content_hash(input_material)
    report_material = {
        "input": input_material,
        "lines": [
            {"model_id": line.model_id, "unit_credit": str(line.unit_credit),
             "contribution": str(line.contribution), "included": line.included}
            for line in lines
        ],
        "total": str(total),
    }
    return TrialReport(
        enterprise_id=enterprise_id,
        year=year,
        rule_version=rule.version,
        rule_fingerprint=rule.content_fingerprint,
        engine_version=ENGINE_VERSION,
        lines=lines,
        total=total,
        input_fingerprint=input_fingerprint,
        report_fingerprint=content_hash(report_material),
    )


# ------------------------------------------------------------- 正式积分分录

@dataclass(frozen=True)
class LedgerEntry:
    """一条正式积分分录。原始分录与调整分录同构，金额带符号。"""

    entry_id: str
    enterprise_id: str
    year: int
    filing_id: str
    model_id: str
    model_version: int
    rule_version: int
    unit_credit: Decimal
    volume: int
    contribution: Decimal          # 调整分录为差额（可负）
    kind: str                      # 原始 / 调整
    adjustment_id: str | None
    evidence: Evidence | None
    content_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "enterprise_id": self.enterprise_id,
            "year": self.year,
            "filing_id": self.filing_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "rule_version": self.rule_version,
            "unit_credit": str(self.unit_credit),
            "volume": self.volume,
            "contribution": str(self.contribution),
            "kind": self.kind,
            "adjustment_id": self.adjustment_id,
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "content_fingerprint": self.content_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LedgerEntry":
        return cls(
            entry_id=data["entry_id"],
            enterprise_id=data["enterprise_id"],
            year=int(data["year"]),
            filing_id=data["filing_id"],
            model_id=data["model_id"],
            model_version=int(data["model_version"]),
            rule_version=int(data["rule_version"]),
            unit_credit=Decimal(data["unit_credit"]),
            volume=int(data["volume"]),
            contribution=Decimal(data["contribution"]),
            kind=data["kind"],
            adjustment_id=data.get("adjustment_id"),
            evidence=Evidence.from_dict(data["evidence"]) if data.get("evidence") else None,
            content_fingerprint=data["content_fingerprint"],
        )


def _entry_id(filing_id: str, model_id: str, kind: str, adjustment_id: str | None) -> str:
    raw = canonical({
        "filing": filing_id,
        "model": model_id,
        "kind": kind,
        "adjustment": adjustment_id,
    })
    import hashlib
    return "ent-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_original_entries(report: TrialReport, filing_id: str) -> list[LedgerEntry]:
    """确认时由试算报告生成原始正式分录（撤销车型贡献为 0 也留痕）。"""
    entries: list[LedgerEntry] = []
    for line in report.lines:
        contribution = line.contribution
        fingerprint = content_hash({
            "filing": filing_id,
            "model_id": line.model_id,
            "model_version": line.model_version,
            "rule_version": report.rule_version,
            "unit_credit": str(line.unit_credit),
            "volume": line.volume,
            "contribution": str(contribution),
            "kind": "原始",
        })
        entries.append(LedgerEntry(
            entry_id=_entry_id(filing_id, line.model_id, "原始", None),
            enterprise_id=report.enterprise_id,
            year=report.year,
            filing_id=filing_id,
            model_id=line.model_id,
            model_version=line.model_version,
            rule_version=report.rule_version,
            unit_credit=line.unit_credit,
            volume=line.volume,
            contribution=contribution,
            kind="原始",
            adjustment_id=None,
            evidence=line.evidence,
            content_fingerprint=fingerprint,
        ))
    return entries


# --------------------------------------------------------------- 调整单

@dataclass(frozen=True)
class AdjustmentLine:
    model_id: str
    from_version: int
    to_version: int
    before: Decimal
    after: Decimal
    delta: Decimal
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "before": str(self.before),
            "after": str(self.after),
            "delta": str(self.delta),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AdjustmentLine":
        return cls(
            model_id=data["model_id"],
            from_version=int(data["from_version"]),
            to_version=int(data["to_version"]),
            before=Decimal(data["before"]),
            after=Decimal(data["after"]),
            delta=Decimal(data["delta"]),
            reason=data["reason"],
        )


@dataclass(frozen=True)
class AdjustmentOrder:
    """调整单：迟到数据 / 车型撤销 / 规则勘误 / 人工更正的唯一记账载体。"""

    adjustment_id: str
    filing_id: str
    enterprise_id: str
    year: int
    kind: AdjustmentKind
    reason: str
    from_rule_version: int
    to_rule_version: int
    seq: int
    lines: tuple[AdjustmentLine, ...]
    total_delta: Decimal
    evidence: Evidence | None
    content_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "adjustment_id": self.adjustment_id,
            "filing_id": self.filing_id,
            "enterprise_id": self.enterprise_id,
            "year": self.year,
            "kind": self.kind.value,
            "reason": self.reason,
            "from_rule_version": self.from_rule_version,
            "to_rule_version": self.to_rule_version,
            "seq": self.seq,
            "total_delta": str(self.total_delta),
            "lines": [line.to_dict() for line in self.lines],
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "content_fingerprint": self.content_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AdjustmentOrder":
        return cls(
            adjustment_id=data["adjustment_id"],
            filing_id=data["filing_id"],
            enterprise_id=data["enterprise_id"],
            year=int(data["year"]),
            kind=AdjustmentKind(data["kind"]),
            reason=data["reason"],
            from_rule_version=int(data["from_rule_version"]),
            to_rule_version=int(data["to_rule_version"]),
            seq=int(data["seq"]),
            lines=tuple(AdjustmentLine.from_dict(x) for x in data["lines"]),
            total_delta=Decimal(data["total_delta"]),
            evidence=Evidence.from_dict(data["evidence"]) if data.get("evidence") else None,
            content_fingerprint=data["content_fingerprint"],
        )


def build_adjustment(
    *,
    filing_id: str,
    enterprise_id: str,
    year: int,
    kind: AdjustmentKind,
    reason: str,
    seq: int,
    before: TrialReport,
    after: TrialReport,
    basis_versions: dict[str, int],
    evidence: Evidence | None,
) -> AdjustmentOrder:
    """对比两次试算，逐车型生成差额行（delta=0 也保留以完整留痕）。"""
    old = before.contribution_by_model()
    new = after.contribution_by_model()
    model_ids = sorted(set(old) | set(new))
    lines: list[AdjustmentLine] = []
    for mid in model_ids:
        before_value = old.get(mid, Decimal("0.00"))
        after_value = new.get(mid, Decimal("0.00"))
        delta = qcredit(after_value - before_value)
        if kind is AdjustmentKind.MODEL_REVOKED and after_value == 0:
            line_reason = "车型撤销，冲回已确认贡献"
        elif kind is AdjustmentKind.RULE_CORRECTION:
            line_reason = f"规则勘误 v{before.rule_version}→v{after.rule_version} 重算"
        elif kind is AdjustmentKind.LATE_DATA:
            line_reason = "迟到批次数据补报，按新版本重算"
        else:
            line_reason = reason
        lines.append(AdjustmentLine(
            model_id=mid,
            from_version=next(
                (ln.model_version for ln in before.lines if ln.model_id == mid), 0),
            to_version=basis_versions.get(mid, next(
                (ln.model_version for ln in after.lines if ln.model_id == mid), 0)),
            before=before_value,
            after=after_value,
            delta=delta,
            reason=line_reason,
        ))
    total_delta = qcredit(sum((line.delta for line in lines), Decimal("0")))
    fingerprint = content_hash({
        "filing": filing_id,
        "seq": seq,
        "kind": kind.value,
        "from_rule_version": before.rule_version,
        "to_rule_version": after.rule_version,
        "lines": [line.to_dict() for line in lines],
        "total_delta": str(total_delta),
    })
    import hashlib
    adjustment_id = (
        "adj-"
        + hashlib.sha256(f"{filing_id}:{seq}:{fingerprint}".encode("utf-8")).hexdigest()[:12]
    )
    return AdjustmentOrder(
        adjustment_id=adjustment_id,
        filing_id=filing_id,
        enterprise_id=enterprise_id,
        year=year,
        kind=kind,
        reason=reason,
        from_rule_version=before.rule_version,
        to_rule_version=after.rule_version,
        seq=seq,
        lines=tuple(lines),
        total_delta=total_delta,
        evidence=evidence,
        content_fingerprint=fingerprint,
    )


def build_adjustment_entries(
    order: AdjustmentOrder,
    after: TrialReport,
) -> list[LedgerEntry]:
    """调整单过账：为每个有差额的车型生成一条带符号调整分录。"""
    after_lines = {line.model_id: line for line in after.lines}
    entries: list[LedgerEntry] = []
    for line in order.lines:
        if line.delta == 0:
            continue
        source = after_lines.get(line.model_id)
        fingerprint = content_hash({
            "filing": order.filing_id,
            "adjustment": order.adjustment_id,
            "model_id": line.model_id,
            "to_version": line.to_version,
            "rule_version": order.to_rule_version,
            "delta": str(line.delta),
            "kind": "调整",
        })
        entries.append(LedgerEntry(
            entry_id=_entry_id(order.filing_id, line.model_id, "调整", order.adjustment_id),
            enterprise_id=order.enterprise_id,
            year=order.year,
            filing_id=order.filing_id,
            model_id=line.model_id,
            model_version=line.to_version,
            rule_version=order.to_rule_version,
            unit_credit=source.unit_credit if source else Decimal("0.0000"),
            volume=source.volume if source else 0,
            contribution=line.delta,
            kind="调整",
            adjustment_id=order.adjustment_id,
            evidence=order.evidence,
            content_fingerprint=fingerprint,
        ))
    return entries
