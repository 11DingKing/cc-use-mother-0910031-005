"""服务层全生命周期测试：版本冻结、调整单、并发确认、稳定复算、监管追溯。"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from credit_service import (  # noqa: E402
    AdjustmentKind,
    ConflictError,
    CreditService,
    Store,
    ValidationError,
)


def make_service() -> tuple[CreditService, str]:
    tmp = tempfile.mkdtemp(prefix="credit-")
    return CreditService(Store(tmp)), tmp


def ev(service: CreditService, batch: str, ref: str):
    return service.register_evidence(
        source="工信部批次文件",
        batch=batch,
        reference=ref,
        payload={"batch": batch, "ref": ref},
    )


def seed(service: CreditService, year: int = 2025):
    """两个车型 + 一条规则。"""
    service.add_rule_version(
        year=year, policy_coef="3.0", rate="0.12", note="初始规则",
        evidence=ev(service, "POLICY-INIT", "公告2025-0号"),
    )
    service.add_model_version(
        model_id="M-A", enterprise_id="E1", name="甲型车", year=year,
        volume=1000, energy="6.0",
        evidence=ev(service, "B1", "doc-b1"),
    )
    service.add_model_version(
        model_id="M-B", enterprise_id="E1", name="乙型车", year=year,
        volume=500, energy="4.5",
        evidence=ev(service, "B1", "doc-b1"),
    )


def open_and_trial(service: CreditService, year: int = 2025):
    filing = service.open_filing("E1", year)
    report = service.trial(
        filing.filing_id, model_versions={"M-A": 1, "M-B": 1})
    return filing, report


class LifecycleTest(unittest.TestCase):
    def test_trial_is_explainable(self) -> None:
        service, _ = make_service()
        seed(service)
        _, report = open_and_trial(service)
        # 单车积分 = 3.0 - 0.12*6.0 = 2.28；贡献 = 2.28*1000 = 2280.00
        lines = {line["model_id"]: line for line in report["lines"]}
        self.assertEqual(lines["M-A"]["unit_credit"], "2.2800")
        self.assertEqual(lines["M-A"]["contribution"], "2280.00")
        # M-B: 3.0 - 0.54 = 2.46；*500 = 1230.00
        self.assertEqual(lines["M-B"]["contribution"], "1230.00")
        self.assertEqual(report["total"], "3510.00")
        self.assertIn("formula", lines["M-A"])
        self.assertIsNotNone(lines["M-A"]["evidence"])
        self.assertTrue(report["input_fingerprint"])
        self.assertTrue(report["report_fingerprint"])

    def test_confirm_freezes_input_and_writes_original_entries(self) -> None:
        service, _ = make_service()
        seed(service)
        filing, report = open_and_trial(service)
        result = service.confirm(filing.filing_id)
        self.assertEqual(result.entry_count, 2)
        self.assertEqual(result.filing.state.value, "已确认")

        stored = service.store.get_filing(filing.filing_id)
        self.assertEqual(stored.confirmed_total, Decimal("3510.00"))
        self.assertEqual(stored.confirmed_input_fingerprint, report["input_fingerprint"])

        entries = service.store.list_entries(filing.filing_id)
        self.assertEqual({e.kind for e in entries}, {"原始"})
        self.assertEqual(sum((e.contribution for e in entries), Decimal("0")),
                         Decimal("3510.00"))

    def test_double_confirm_rejected(self) -> None:
        service, _ = make_service()
        seed(service)
        filing, _ = open_and_trial(service)
        service.confirm(filing.filing_id)
        with self.assertRaises(ConflictError):
            service.confirm(filing.filing_id)

    def test_late_data_creates_new_version_and_adjustment(self) -> None:
        service, _ = make_service()
        seed(service)
        filing, _ = open_and_trial(service)
        service.confirm(filing.filing_id)

        # 迟到批次：M-A 实际产量为 1200（补报），登记 v2
        service.add_model_version(
            model_id="M-A", enterprise_id="E1", name="甲型车", year=2025,
            volume=1200, energy="6.0",
            evidence=ev(service, "B2-LATE", "doc-b2"),
        )
        result = service.post_late_data(
            filing.filing_id, model_versions={"M-A": 2},
            reason="第三批产量补报", evidence=ev(service, "B2-LATE", "doc-b2"),
        )
        order = result["adjustment"]
        self.assertEqual(order["kind"], "迟到数据")
        # 差额 = 2.28*200 = 456.00
        self.assertEqual(order["total_delta"], "456.00")
        line = next(l for l in order["lines"] if l["model_id"] == "M-A")
        self.assertEqual(line["from_version"], 1)
        self.assertEqual(line["to_version"], 2)

        entries = service.store.list_entries(filing.filing_id)
        self.assertEqual(len(entries), 3)  # 2 原始 + 1 调整
        self.assertEqual(sum((e.contribution for e in entries), Decimal("0")),
                         Decimal("3966.00"))
        # 原始分录金额永不被改写
        original_ma = next(e for e in entries if e.model_id == "M-A" and e.kind == "原始")
        self.assertEqual(original_ma.contribution, Decimal("2280.00"))
        self.assertEqual(original_ma.model_version, 1)

    def test_revoked_model_contribution_is_reversed(self) -> None:
        service, _ = make_service()
        seed(service)
        filing, _ = open_and_trial(service)
        service.confirm(filing.filing_id)

        result = service.revoke_model(
            filing.filing_id, model_id="M-B",
            reason="公告撤销该车型", evidence=ev(service, "REVOKE-1", "doc-rev"))
        order = result["adjustment"]
        self.assertEqual(order["kind"], "车型撤销")
        self.assertEqual(order["total_delta"], "-1230.00")
        entries = service.store.list_entries(filing.filing_id)
        self.assertEqual(sum((e.contribution for e in entries), Decimal("0")),
                         Decimal("2280.00"))
        latest = service.store.get_model_version("M-B")
        self.assertEqual(latest.status.value, "撤销")
        self.assertEqual(latest.version, 2)

    def test_revoke_unknown_or_double_rejected(self) -> None:
        service, _ = make_service()
        seed(service)
        filing, _ = open_and_trial(service)
        service.confirm(filing.filing_id)
        with self.assertRaises(ValidationError):
            service.revoke_model(
                filing.filing_id, model_id="M-NOPE",
                reason="不在基线内", evidence=ev(service, "X", "x"))
        service.revoke_model(
            filing.filing_id, model_id="M-B",
            reason="撤销", evidence=ev(service, "R1", "r"))
        with self.assertRaises(ValidationError):
            service.revoke_model(
                filing.filing_id, model_id="M-B",
                reason="重复撤销", evidence=ev(service, "R2", "r2"))

    def test_rule_correction_recomputes_all_models(self) -> None:
        service, _ = make_service()
        seed(service)
        filing, _ = open_and_trial(service)
        service.confirm(filing.filing_id)

        # 勘误：rate 0.12 → 0.10
        result = service.correct_rule(
            filing.filing_id, reason="能耗系数勘误",
            policy_coef="3.0", rate="0.10",
            evidence=ev(service, "POLICY-FIX", "公告2025-1号"),
        )
        order = result["adjustment"]
        self.assertEqual(order["kind"], "规则勘误")
        self.assertEqual(order["from_rule_version"], 1)
        self.assertEqual(order["to_rule_version"], 2)
        # M-A: (3.0-0.60)*1000 = 2400 (Δ +120)
        # M-B: (3.0-0.45)*500 = 1275 (Δ +45)
        self.assertEqual(order["total_delta"], "165.00")
        entries = service.store.list_entries(filing.filing_id)
        self.assertEqual(sum((e.contribution for e in entries), Decimal("0")),
                         Decimal("3675.00"))

    def test_noop_rule_correction_rejected(self) -> None:
        service, _ = make_service()
        seed(service)
        filing, _ = open_and_trial(service)
        service.confirm(filing.filing_id)
        with self.assertRaises(ValidationError):
            service.correct_rule(
                filing.filing_id, reason="重复提交",
                policy_coef="3.0", rate="0.12",
                evidence=ev(service, "POLICY-DUP", "x"),
            )

    def test_noop_late_data_rejected(self) -> None:
        service, _ = make_service()
        seed(service)
        filing, _ = open_and_trial(service)
        service.confirm(filing.filing_id)
        service.add_model_version(
            model_id="M-A", enterprise_id="E1", name="甲型车", year=2025,
            volume=1000, energy="6.0",
            evidence=ev(service, "B2", "doc-b2"),
        )
        with self.assertRaises(ValidationError):
            service.post_late_data(
                filing.filing_id, model_versions={"M-A": 2},
                reason="参数无变化", evidence=ev(service, "B2", "doc-b2"),
            )

    def test_adjustment_not_allowed_before_confirm(self) -> None:
        service, _ = make_service()
        seed(service)
        filing, _ = open_and_trial(service)
        service.add_model_version(
            model_id="M-A", enterprise_id="E1", name="甲型车", year=2025,
            volume=1100, energy="6.0", evidence=ev(service, "B2", "x"),
        )
        with self.assertRaises(ConflictError):
            service.post_late_data(
                filing.filing_id, model_versions={"M-A": 2},
                reason="草稿期不应有调整单", evidence=ev(service, "B2", "x"),
            )

    def test_model_versions_are_append_only_history(self) -> None:
        service, _ = make_service()
        seed(service)
        service.add_model_version(
            model_id="M-A", enterprise_id="E1", name="甲型车", year=2025,
            volume=1200, energy="6.0", evidence=ev(service, "B2", "x"),
        )
        versions = service.store.list_model_versions("M-A")
        self.assertEqual([v.version for v in versions], [1, 2])
        self.assertEqual(versions[0].volume, 1000)   # 历史版本原样保留
        self.assertEqual(versions[1].supersedes, 1)
        self.assertEqual(versions[0].content_fingerprint, versions[0].content_fingerprint)
        self.assertNotEqual(versions[0].content_fingerprint, versions[1].content_fingerprint)


class ReverifyTest(unittest.TestCase):
    def _matured_filing(self, service: CreditService):
        seed(service)
        filing, _ = open_and_trial(service)
        service.confirm(filing.filing_id)
        service.add_model_version(
            model_id="M-A", enterprise_id="E1", name="甲型车", year=2025,
            volume=1200, energy="6.0", evidence=ev(service, "B2", "x"),
        )
        service.post_late_data(
            filing.filing_id, model_versions={"M-A": 2},
            reason="补报", evidence=ev(service, "B2", "x"),
        )
        service.correct_rule(
            filing.filing_id, reason="勘误", policy_coef="3.0", rate="0.10",
            evidence=ev(service, "FIX", "y"),
        )
        return filing

    def test_reverify_replays_all_adjustments(self) -> None:
        service, _ = make_service()
        filing = self._matured_filing(service)
        result = service.reverify(filing.filing_id)
        self.assertTrue(all(result["checks"].values()))
        self.assertTrue(all(step["match"] for step in result["adjustment_replays"]))
        self.assertTrue(result["ledger_match"])
        # 确认 3510；迟到补报 +456（旧率，产量 1000→1200）⇒ 3966；
        # 规则勘误按补报后基数重算：M-A 2.40*1200=2880、M-B 2.55*500=1275 ⇒ 4155
        deltas = [step["stored_delta"] for step in result["adjustment_replays"]]
        self.assertEqual(deltas, ["456.00", "189.00"])
        self.assertEqual(result["recomputed_total"], "4155.00")

    def test_same_inputs_recompute_identically_in_new_process(self) -> None:
        service, data_dir = make_service()
        filing = self._matured_filing(service)
        first = service.reverify(filing.filing_id)

        # 用同一数据目录重建 Store/Service，模拟另一个进程/后续批次复算。
        reopened = CreditService(Store(data_dir))
        second = reopened.reverify(filing.filing_id)
        self.assertEqual(first["recomputed_total"], second["recomputed_total"])
        self.assertEqual(first["checks"], second["checks"])
        confirmed = reopened.store.get_filing(filing.filing_id)
        # 再跑一次试算：相同版本输入指纹一致
        models = [reopened.store.get_model_version(mid, ver)
                  for mid, ver in confirmed.basis_model_versions.items()]
        rule = reopened.store.get_rule(2025, confirmed.basis_rule_version)
        from credit_service import run_trial
        report = run_trial("E1", 2025, models, rule)
        self.assertEqual(str(report.total), second["recomputed_total"])

    def test_reverify_stable_under_repeated_calls(self) -> None:
        service, _ = make_service()
        filing = self._matured_filing(service)
        r1 = service.reverify(filing.filing_id)
        r2 = service.reverify(filing.filing_id)
        self.assertEqual(r1, r2)


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_confirm_only_one_succeeds(self) -> None:
        service, _ = make_service()
        seed(service)
        filing, _ = open_and_trial(service)
        fid = filing.filing_id
        outcomes: list[str] = []
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            try:
                service.confirm(fid)
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")
            except Exception as exc:  # pragma: no cover
                outcomes.append(f"error:{exc}")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("conflict"), 7)
        entries = service.store.list_entries(fid)
        # 绝不能出现重复原始分录
        originals = [e for e in entries if e.kind == "原始"]
        self.assertEqual(len(originals), 2)
        self.assertEqual(len({e.entry_id for e in originals}), 2)

    def test_concurrent_late_data_adjustments_serialize(self) -> None:
        service, _ = make_service()
        seed(service)
        filing, _ = open_and_trial(service)
        service.confirm(filing.filing_id)
        # v2、v3 两次补报
        service.add_model_version(
            model_id="M-A", enterprise_id="E1", name="甲型车", year=2025,
            volume=1100, energy="6.0", evidence=ev(service, "B2", "x"))
        service.add_model_version(
            model_id="M-A", enterprise_id="E1", name="甲型车", year=2025,
            volume=1200, energy="6.0", evidence=ev(service, "B3", "x"))

        results: list[str] = []

        def w1() -> None:
            try:
                service.post_late_data(
                    filing.filing_id, model_versions={"M-A": 2},
                    reason="补报1", evidence=ev(service, "B2", "x"))
                results.append("ok")
            except ConflictError:
                results.append("conflict")

        def w3() -> None:
            try:
                service.post_late_data(
                    filing.filing_id, model_versions={"M-A": 3},
                    reason="补报2", evidence=ev(service, "B3", "x"))
                results.append("ok")
            except ConflictError:
                results.append("conflict")

        t1 = threading.Thread(target=w1)
        t3 = threading.Thread(target=w3)
        t1.start(); t3.start()
        t1.join(); t3.join()
        # 两张调整单在聚合锁内串行化：
        #  - v2 先过账 ⇒ v3 继续推进，两张都成功，序号 1、2；
        #  - v3 先过账 ⇒ v2 因“版本早于基线”被拒，一张冲突。
        # 无论哪种交错，最终基线都必须是最新的 v3，不得被旧版本冲回。
        self.assertGreaterEqual(results.count("ok"), 1)
        self.assertIn(sorted(results), [["ok", "ok"], ["conflict", "ok"]])
        stored = service.store.get_filing(filing.filing_id)
        self.assertEqual(stored.basis_model_versions["M-A"], 3)
        self.assertEqual(stored.adjustment_seqs, list(range(1, len(stored.adjustment_seqs) + 1)))
        # 最终：M-A 2.28*1200 = 2736，M-B 1230 ⇒ 3966
        entries = service.store.list_entries(filing.filing_id)
        self.assertEqual(sum((e.contribution for e in entries), Decimal("0")),
                         Decimal("3966.00"))
        check = service.reverify(filing.filing_id)
        self.assertTrue(check["ledger_match"])


class RegulatorTraceTest(unittest.TestCase):
    def test_trace_from_enterprise_total_to_model_contributions(self) -> None:
        service, _ = make_service()
        seed(service)
        filing, _ = open_and_trial(service)
        service.confirm(filing.filing_id)
        service.revoke_model(
            filing.filing_id, model_id="M-B",
            reason="撤销", evidence=ev(service, "R1", "r"))

        trace = service.regulator_trace("E1", 2025)
        self.assertEqual(trace["current_total"], "2280.00")
        self.assertEqual(trace["confirmed_total"], "3510.00")
        by_id = {m["model_id"]: m for m in trace["models"]}
        self.assertEqual(by_id["M-A"]["net_contribution"], "2280.00")
        self.assertEqual(by_id["M-A"]["original"], "2280.00")
        self.assertEqual(by_id["M-B"]["net_contribution"], "0.00")
        self.assertEqual(by_id["M-B"]["status"], "撤销")
        adj = by_id["M-B"]["adjustments"]
        self.assertEqual(adj[0]["kind"], "车型撤销")
        self.assertEqual(adj[0]["delta"], "-1230.00")
        # 证据链完整：原始 + 撤销批次
        refs = {e["reference"] for e in by_id["M-B"]["evidence_chain"]}
        self.assertIn("doc-b1", refs)
        self.assertIn("r", refs)
        # 企业总额 = 各车型净贡献之和
        self.assertEqual(
            sum((Decimal(m["net_contribution"]) for m in trace["models"]), Decimal("0")),
            Decimal(trace["current_total"]),
        )


if __name__ == "__main__":
    unittest.main()
