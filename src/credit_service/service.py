"""应用服务层：编排用例，强制领域规则。

关键规则：
1. 草稿期可反复生成可解释试算；确认时把输入版本集合冻结进申报单，
   并生成只追加的原始正式分录。
2. 确认/封存后：迟到数据 → 新车型版本 + 调整单；车型撤销 → 撤销版本 +
   调整单；规则勘误 → 新规则版本 + 调整单；任何输入都不得就地改写。
3. 并发确认：整个“读-校验-写分录-改状态”在申报单聚合锁内完成，
   第二个确认请求只会拿到 ConflictError。
4. 复算：从冻结指纹重新取版本试算，结果必须与确认时逐位一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .canonical import content_hash
from .engine import (
    AdjustmentOrder,
    build_adjustment,
    build_adjustment_entries,
    build_original_entries,
    run_trial,
)
from .errors import ConflictError, ValidationError
from .models import (
    AdjustmentKind,
    CalculationRule,
    Evidence,
    FilingState,
    ModelStatus,
    VehicleModelVersion,
    utcnow,
)
from .storage import Filing, Store


@dataclass
class ConfirmResult:
    filing: Filing
    report: dict[str, Any]
    entry_count: int


class CreditService:
    def __init__(self, store: Store):
        self.store = store

    # --------------------------------------------------------- 输入侧登记

    def register_evidence(
        self, *, source: str, batch: str, reference: str, payload: Any
    ) -> Evidence:
        """把一条原始报文登记为证据（计算 payload 指纹）。"""
        return Evidence(
            source=source,
            batch=batch,
            reference=reference,
            submitted_at=utcnow(),
            payload_hash=content_hash(payload),
        )

    def add_model_version(
        self,
        *,
        model_id: str,
        enterprise_id: str,
        name: str,
        year: int,
        volume: int,
        energy: str | Decimal,
        evidence: Evidence,
        attrs: dict[str, str] | None = None,
        status: ModelStatus = ModelStatus.ACTIVE,
    ) -> VehicleModelVersion:
        return self.store.add_model_version(
            model_id=model_id,
            enterprise_id=enterprise_id,
            name=name,
            year=year,
            volume=int(volume),
            energy=Decimal(str(energy)),
            attrs=attrs,
            status=status,
            evidence=evidence,
        )

    def add_rule_version(
        self,
        *,
        year: int,
        policy_coef: str | Decimal,
        rate: str | Decimal,
        intercept: str | Decimal = "0",
        note: str = "",
        evidence: Evidence,
    ) -> CalculationRule:
        return self.store.add_rule_version(
            year=year,
            policy_coef=Decimal(str(policy_coef)),
            rate=Decimal(str(rate)),
            intercept=Decimal(str(intercept)),
            note=note,
            evidence=evidence,
        )

    # ------------------------------------------------------------- 申报单

    def open_filing(self, enterprise_id: str, year: int) -> Filing:
        return self.store.create_filing(enterprise_id, year)

    def _get(self, filing_id: str) -> Filing:
        return self.store.get_filing(filing_id)

    # ----------------------------------------------------------- 可解释试算

    def _assemble(
        self,
        filing: Filing,
        model_ids: list[str] | None,
        basis: dict[str, int] | None,
        rule_version: int | None,
    ) -> tuple[list[VehicleModelVersion], CalculationRule]:
        if model_ids is None:
            model_ids = sorted(basis.keys() if basis else filing.basis_model_versions.keys())
        if basis is None:
            basis = filing.basis_model_versions
        models: list[VehicleModelVersion] = []
        for mid in model_ids:
            version = basis.get(mid)
            if version is None:
                raise ValidationError(f"车型 {mid} 未指定参数版本")
            model = self.store.get_model_version(mid, version)
            if model.enterprise_id != filing.enterprise_id or model.year != filing.year:
                raise ValidationError(f"车型 {mid} v{version} 不属于该企业年度申报范围")
            models.append(model)
        rule = self.store.get_rule(filing.year, rule_version or filing.basis_rule_version)
        return models, rule

    def trial(
        self,
        filing_id: str,
        *,
        model_versions: dict[str, int],
        rule_version: int | None = None,
    ) -> dict[str, Any]:
        """草稿/已确认状态均可调用：给出显式版本集合即可“如果这样算”的试算。

        已确认后调用不会改动任何已冻结数据，只是预览新版本的影响。
        """
        filing = self._get(filing_id)
        models, rule = self._assemble(filing, sorted(model_versions), model_versions, rule_version)
        report = run_trial(filing.enterprise_id, filing.year, models, rule)
        if filing.state is FilingState.DRAFT:
            # 草稿期保存最近一次试算的输入基线，便于确认；不产生任何分录。
            filing.basis_model_versions = {m.model_id: m.version for m in models}
            filing.basis_rule_version = rule.version
            filing.draft_report = report.to_dict()
            self.store.save_filing(filing)
        return report.to_dict()

    # ------------------------------------------------------------- 确认冻结

    def confirm(self, filing_id: str, *, expected_rev: int | None = None) -> ConfirmResult:
        """企业确认：冻结输入、生成原始正式分录。

        全程持有申报单聚合锁；锁内重读状态，杜绝并发重复确认。
        """
        with self.store.filing_lock(filing_id):
            filing = self._get(filing_id)
            if filing.state is not FilingState.DRAFT:
                raise ConflictError(
                    f"申报单 {filing_id} 状态为 {filing.state.value}，不能重复确认")
            if not filing.basis_model_versions or filing.basis_rule_version is None:
                raise ValidationError("尚未生成试算，没有可确认的输入")
            models, rule = self._assemble(
                filing,
                sorted(filing.basis_model_versions),
                filing.basis_model_versions,
                filing.basis_rule_version,
            )
            report = run_trial(filing.enterprise_id, filing.year, models, rule)
            entries = build_original_entries(report, filing_id)

            filing.state = FilingState.CONFIRMED
            filing.confirmed_report = report.to_dict()
            filing.confirmed_input_fingerprint = report.input_fingerprint
            filing.confirmed_report_fingerprint = report.report_fingerprint
            filing.confirmed_total = report.total
            filing.confirmed_at = utcnow().isoformat()
            # save 与分录落盘在同一把锁内；先写分录再翻转状态，
            # 崩溃时最坏是多出尚未生效的分录，不会出现“已确认无分录”。
            self.store.append_entries(filing_id, entries)
            self.store.save_filing(filing, expected_rev=expected_rev)
            return ConfirmResult(filing=filing, report=report.to_dict(), entry_count=len(entries))

    def seal_filing(self, filing_id: str) -> Filing:
        """监管/运营封存年度账目；封存后仅允许调整单。"""
        with self.store.filing_lock(filing_id):
            filing = self._get(filing_id)
            if filing.state is FilingState.DRAFT:
                raise ConflictError("未确认的申报单不能封存")
            filing.state = FilingState.FROZEN
            self.store.save_filing(filing)
            return filing

    # --------------------------------------- 迟到数据 / 撤销 / 规则勘误

    def _post_adjustment(
        self,
        filing_id: str,
        *,
        kind: AdjustmentKind,
        reason: str,
        new_basis: dict[str, int],
        new_rule_version: int | None,
        evidence: Evidence,
    ) -> dict[str, Any]:
        with self.store.filing_lock(filing_id):
            filing = self._get(filing_id)
            if filing.state is FilingState.DRAFT:
                raise ConflictError("申报单尚未确认，直接修改草稿试算即可，无需调整单")
            if filing.confirmed_report is None:
                raise ConflictError("缺少确认基线，无法生成调整单")
            # 单调性：迟到数据/撤销只能把车型基线推进到更新版本，
            # 杜绝并发交错时旧版本把已过账的新版本“冲回去”。
            for mid, ver in new_basis.items():
                current_ver = filing.basis_model_versions.get(mid)
                if current_ver is not None and ver < current_ver:
                    raise ConflictError(
                        f"车型 {mid} 版本 v{ver} 早于当前基线 v{current_ver}，"
                        "调整单只能引用更新的版本")
            # 规则版本在锁内解析：迟到数据/撤销沿用当前基线规则，
            # 避免锁外读到的版本与并发规则勘误相互错位。
            if new_rule_version is None:
                new_rule_version = filing.basis_rule_version
            if new_rule_version < (filing.basis_rule_version or 0):
                raise ConflictError("不能回退到已勘误之前的规则版本")

            old_models, old_rule = self._assemble(
                filing,
                sorted(filing.basis_model_versions),
                filing.basis_model_versions,
                filing.basis_rule_version,
            )
            before = run_trial(filing.enterprise_id, filing.year, old_models, old_rule)

            merged = dict(filing.basis_model_versions)
            merged.update(new_basis)
            new_models, new_rule = self._assemble(
                filing, sorted(merged), merged, new_rule_version
            )
            after = run_trial(filing.enterprise_id, filing.year, new_models, new_rule)

            seq = max(filing.adjustment_seqs, default=0) + 1
            order = build_adjustment(
                filing_id=filing_id,
                enterprise_id=filing.enterprise_id,
                year=filing.year,
                kind=kind,
                reason=reason,
                seq=seq,
                before=before,
                after=after,
                basis_versions={m.model_id: m.version for m in new_models},
                evidence=evidence,
            )
            # 空操作判定按类型区分：迟到数据/撤销金额差额为 0 即无意义；
            # 规则勘误只要参数确实变化（登记时已拦截完全相同的规则），
            # 即使金额恰好轧平也允许留痕。
            noop = order.total_delta == 0 and kind in (
                AdjustmentKind.LATE_DATA, AdjustmentKind.MODEL_REVOKED, AdjustmentKind.MANUAL)
            if noop:
                raise ValidationError("新版本相对确认基线没有金额影响，无需调整单")
            entries = build_adjustment_entries(order, after)

            # 先写调整单与调整分录，再推进聚合基线。
            self.store.save_adjustment(order)
            self.store.append_entries(filing_id, entries)
            filing.basis_model_versions = {m.model_id: m.version for m in new_models}
            filing.basis_rule_version = new_rule.version
            filing.adjustment_seqs.append(seq)
            self.store.save_filing(filing)
            return {
                "adjustment": order.to_dict(),
                "adjustment_entries": [e.to_dict() for e in entries],
                "new_total_preview": str(after.total),
            }

    def post_late_data(
        self,
        filing_id: str,
        *,
        model_versions: dict[str, int],
        reason: str,
        evidence: Evidence,
    ) -> dict[str, Any]:
        """迟到批次数据：企业已先登记新车型版本，这里按新版本出调整单。"""
        filing = self._get(filing_id)
        rule_version = filing.basis_rule_version
        return self._post_adjustment(
            filing_id,
            kind=AdjustmentKind.LATE_DATA,
            reason=reason,
            new_basis=model_versions,
            new_rule_version=rule_version,
            evidence=evidence,
        )

    def revoke_model(
        self,
        filing_id: str,
        *,
        model_id: str,
        reason: str,
        evidence: Evidence,
        name: str | None = None,
        energy: str | Decimal = "0",
    ) -> dict[str, Any]:
        """车型撤销：发布“撤销”状态的新版本，再以调整单冲回全部贡献。"""
        filing = self._get(filing_id)
        if model_id not in filing.basis_model_versions:
            raise ValidationError("只能撤销已纳入确认基线的车型")
        current = self.store.get_model_version(model_id, filing.basis_model_versions.get(model_id))
        if current.status is ModelStatus.REVOKED:
            raise ValidationError(f"车型 {model_id} 已撤销，不能重复撤销")
        new_version = self.add_model_version(
            model_id=model_id,
            enterprise_id=filing.enterprise_id,
            name=name or current.name,
            year=filing.year,
            volume=current.volume,
            energy=current.energy if str(energy) == "0" else energy,
            attrs=dict(current.attrs),
            status=ModelStatus.REVOKED,
            evidence=evidence,
        )
        return self._post_adjustment(
            filing_id,
            kind=AdjustmentKind.MODEL_REVOKED,
            reason=reason,
            new_basis={model_id: new_version.version},
            new_rule_version=filing.basis_rule_version,
            evidence=evidence,
        )

    def correct_rule(
        self,
        filing_id: str,
        *,
        reason: str,
        evidence: Evidence,
        policy_coef: str | Decimal,
        rate: str | Decimal,
        intercept: str | Decimal = "0",
    ) -> dict[str, Any]:
        """规则勘误：发布新规则版本，对全部已确认车型重算并出调整单。"""
        filing = self._get(filing_id)
        latest = self.store.list_rules(filing.year)[-1:]
        if latest and (
            latest[0].policy_coef == Decimal(str(policy_coef))
            and latest[0].rate == Decimal(str(rate))
            and latest[0].intercept == Decimal(str(intercept))
        ):
            raise ValidationError("新规则参数与现行版本完全一致，不构成勘误")
        new_rule = self.add_rule_version(
            year=filing.year,
            policy_coef=policy_coef,
            rate=rate,
            intercept=intercept,
            note=f"规则勘误：{reason}",
            evidence=evidence,
        )
        return self._post_adjustment(
            filing_id,
            kind=AdjustmentKind.RULE_CORRECTION,
            reason=reason,
            new_basis={},
            new_rule_version=new_rule.version,
            evidence=evidence,
        )

    # ------------------------------------------------------------- 稳定复算

    def reverify(self, filing_id: str) -> dict[str, Any]:
        """从冻结输入重放：原始基线 + 逐张调整单，核对分录与指纹。

        任何一位不一致都会抛错；这是“相同输入稳定复算”的对外自检。
        """
        filing = self._get(filing_id)
        if filing.confirmed_report is None:
            raise ValidationError("申报单尚未确认")
        # 复算确认基线需要确认时的版本集合（而不是调整后的当前基线），
        # 它保存在 confirmed_report.lines 中。
        confirmed_basis = {
            line["model_id"]: line["model_version"]
            for line in filing.confirmed_report["lines"]
        }
        confirmed_rule_version = filing.confirmed_report["rule_version"]
        base_models, base_rule = self._assemble(
            filing, sorted(confirmed_basis), confirmed_basis, confirmed_rule_version
        )
        recomputed = run_trial(filing.enterprise_id, filing.year, base_models, base_rule)
        checks = {
            "confirmed_input_fingerprint_match":
                recomputed.input_fingerprint == filing.confirmed_input_fingerprint,
            "confirmed_report_fingerprint_match":
                recomputed.report_fingerprint == filing.confirmed_report_fingerprint,
            "confirmed_total_match": str(recomputed.total) == str(filing.confirmed_total),
        }
        if not all(checks.values()):
            raise ConflictError(f"复算结果与确认基线不一致：{checks}")

        # 重放调整单：按 seq 重建每一步的基数与规则，核对 delta。
        orders = self.store.list_adjustments(filing_id)
        running = {mid: ver for mid, ver in confirmed_basis.items()}
        rule_ver = confirmed_rule_version
        previous = recomputed
        replay: list[dict[str, Any]] = []
        for order in sorted(orders, key=lambda o: o.seq):
            running.update({line.model_id: line.to_version for line in order.lines})
            rule_ver = order.to_rule_version
            step_models, step_rule = self._assemble(
                filing, sorted(running), running, rule_ver
            )
            step_report = run_trial(filing.enterprise_id, filing.year, step_models, step_rule)
            expected_delta = step_report.total - previous.total
            match = str(expected_delta) == str(order.total_delta)
            replay.append({
                "seq": order.seq,
                "adjustment_id": order.adjustment_id,
                "stored_delta": str(order.total_delta),
                "replayed_delta": str(expected_delta),
                "match": match,
            })
            if not match:
                raise ConflictError(f"调整单 {order.adjustment_id} 复算差额不一致")
            previous = step_report

        # 分类账总额 = 原始分录 + 全部调整分录，必须等于最终重放总额。
        entries = self.store.list_entries(filing_id)
        ledger_total = sum((e.contribution for e in entries), Decimal("0"))
        ledger_match = str(ledger_total) == str(previous.total)
        return {
            "filing_id": filing_id,
            "state": filing.state.value,
            "checks": checks,
            "adjustment_replays": replay,
            "recomputed_total": str(previous.total),
            "ledger_total": str(ledger_total),
            "ledger_match": ledger_match,
            "entry_count": len(entries),
        }

    # --------------------------------------------------------- 监管追溯 API

    def regulator_trace(self, enterprise_id: str, year: int) -> dict[str, Any]:
        """从企业总额逐级追溯到单车型贡献，含版本、规则、证据链。"""
        filing_id = f"filing-{enterprise_id}-{year}"
        filing = self._get(filing_id)
        entries = self.store.list_entries(filing_id)
        orders = {o.adjustment_id: o for o in self.store.list_adjustments(filing_id)}

        per_model: dict[str, dict[str, Any]] = {}
        total = Decimal("0")
        for entry in sorted(entries, key=lambda e: (e.model_id, e.entry_id)):
            slot = per_model.setdefault(entry.model_id, {
                "model_id": entry.model_id,
                "current_version": entry.model_version,
                "rule_version": entry.rule_version,
                "original": None,
                "adjustments": [],
                "net_contribution": Decimal("0"),
                "evidence_chain": [],
            })
            slot["current_version"] = max(slot["current_version"], entry.model_version)
            slot["rule_version"] = max(slot["rule_version"], entry.rule_version)
            slot["net_contribution"] += entry.contribution
            if entry.evidence is not None:
                ev = entry.evidence.to_dict()
                if ev not in slot["evidence_chain"]:
                    slot["evidence_chain"].append(ev)
            if entry.kind == "原始":
                slot["original"] = str(entry.contribution)
            else:
                order = orders.get(entry.adjustment_id)
                slot["adjustments"].append({
                    "adjustment_id": entry.adjustment_id,
                    "kind": order.kind.value if order else None,
                    "reason": order.reason if order else None,
                    "seq": order.seq if order else None,
                    "model_version": entry.model_version,
                    "delta": str(entry.contribution),
                    "evidence": entry.evidence.to_dict() if entry.evidence is not None else None,
                })
            total += entry.contribution

        models_out = []
        for mid in sorted(per_model):
            slot = per_model[mid]
            model = self.store.get_model_version(mid, slot["current_version"])
            slot["name"] = model.name
            slot["status"] = model.status.value
            slot["net_contribution"] = str(slot["net_contribution"])
            models_out.append(slot)

        return {
            "enterprise_id": enterprise_id,
            "year": year,
            "filing_id": filing_id,
            "state": filing.state.value,
            "confirmed_total": str(filing.confirmed_total) if filing.confirmed_total is not None else None,
            "current_total": str(total),
            "input_fingerprint_at_confirm": filing.confirmed_input_fingerprint,
            "models": models_out,
            "adjustment_count": len(orders),
        }
