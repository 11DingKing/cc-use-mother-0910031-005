"""端到端业务测试：版本化、试算、冻结、调整单、追溯与确定性复算。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from credit_accounting.errors import ConflictError, StaleStateError, ValidationError
from credit_accounting.models import CONFIRMED, DRAFT, PENDING
from credit_accounting.store import Store

YEAR = 2025

RULE_V1 = {
    "engine": "1.0.0",
    "bev": {
        "range_threshold_km": 100,
        "energy_ref_kwh": "15.0",
        "energy_sensitivity": "0.5",
        "factor_min": "0.8",
        "factor_max": 1.5,
    },
    "phev": {"range_threshold_km": 50, "base_credit": "1.0"},
    "fcv": {"unit_credit": "4.0"},
    "ice": {"cafc_weight": "10.0"},
}
RULE_V2_ERRATA = {
    **RULE_V1,
    "bev": {**RULE_V1["bev"], "range_threshold_km": 80},
}
POLICY_V1 = {"nev_multiplier": "1.0", "cafc_multiplier": "1.0"}
POLICY_V2 = {"nev_multiplier": "1.2", "cafc_multiplier": "1.0"}

EV_BATCH = {"source_type": "批次申报", "source_ref": "BATCH-2025-01"}
EV_POLICY = {"source_type": "政策公告", "source_ref": "工信部公告2025-3号"}
EV_ERRATA = {"source_type": "规则勘误", "source_ref": "勘误2025-1号"}
EV_LATE = {"source_type": "批次申报", "source_ref": "BATCH-2025-补09", "note": "企业迟到补报"}
EV_WITHDRAW = {"source_type": "撤销文件", "source_ref": "撤销-M3-202510"}


def build_world(store: Store) -> dict:
    ent = store.create_enterprise("某汽车集团")
    eid = ent["id"]
    store.open_period(eid, YEAR)
    rule = store.publish_rule_version(content=RULE_V1, evidence=EV_POLICY, actor_role="核算专员")
    policy = store.publish_policy_version(year=YEAR, content=POLICY_V1, evidence=EV_POLICY,
                                          actor_role="核算专员")

    def submit_prod(code, qty):
        return store.submit_model_version(
            enterprise_id=eid, year=YEAR, model_code=code, version_type="production",
            content={"quantity": qty}, evidence=EV_BATCH, actor_role="企业申报员",
            batch_no="BATCH-01",
        )

    def submit_energy(code, payload):
        return store.submit_model_version(
            enterprise_id=eid, year=YEAR, model_code=code, version_type="energy",
            content=payload, evidence=EV_BATCH, actor_role="企业申报员",
            batch_no="BATCH-01",
        )

    m1p = submit_prod("M1-BEV", 1000)
    m1e = submit_energy("M1-BEV", {"energy_type": "BEV", "range_km": 400, "kwh_per_100km": "13.0"})
    m2p = submit_prod("M2-PHEV", 500)
    m2e = submit_energy("M2-PHEV", {"energy_type": "PHEV", "range_km": 80})
    m3p = submit_prod("M3-ICE", 2000)
    m3e = submit_energy("M3-ICE", {
        "energy_type": "ICE", "fuel_l_per_100km": "7.2", "target_l_per_100km": "6.9",
    })
    period = store.find_period(eid, YEAR)
    return {
        "eid": eid, "period_id": period["id"], "rule": rule["id"], "policy": policy["id"],
        "versions": {"m1p": m1p["id"], "m1e": m1e["id"], "m2p": m2p["id"], "m2e": m2e["id"],
                     "m3p": m3p["id"], "m3e": m3e["id"]},
    }


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.world = build_world(self.store)

    def tearDown(self):
        self.store.close()

    def test_trial_is_explainable_and_deterministic(self):
        w = self.world
        t1 = self.store.trial(w["period_id"])
        t2 = self.store.trial(w["period_id"])
        self.assertEqual(t1["input_hash"], t2["input_hash"])
        self.assertEqual(t1["total_credit"], t2["total_credit"])
        by_model = {ln["model_code"]: ln for ln in t1["lines"]}
        m1 = by_model["M1-BEV"]
        # 1000 辆 BEV：续航分 3.0；能耗因子 = 1 + (15-13)/15*0.5 ≈ 1.0667
        self.assertEqual(m1["unit_credit"], "3.20")  # 3.0 * 1.0667 = 3.20
        self.assertEqual(m1["final_credit"], "3200.00")
        step_names = [s["step"] for s in m1["steps"]]
        self.assertIn("续航里程分", step_names)
        self.assertIn("能耗调整系数", step_names)
        self.assertIn("年度政策系数", step_names)
        # ICE 油耗超标 → 负贡献
        m3 = by_model["M3-ICE"]
        self.assertTrue(Decimal(m3["final_credit"]) < 0)
        # 试算不落正式分录
        self.assertIsNone(self.store.get_period(w["period_id"])["formal_entry_id"])

    def test_same_inputs_replay_identically_in_new_store(self):
        w = self.world
        t = self.store.trial(w["period_id"])
        fresh = Store(":memory:")
        try:
            w2 = build_world(fresh)
            t2 = fresh.trial(w2["period_id"])
            # 版本 ID 是随机身份标识，输入哈希随身份不同；核算数值必须逐行一致
            self.assertEqual(t["total_credit"], t2["total_credit"])
            a = {ln["model_code"]: ln for ln in t["lines"]}
            b = {ln["model_code"]: ln for ln in t2["lines"]}
            self.assertEqual(set(a), set(b))
            for code in a:
                for key in ("unit_credit", "subtotal", "multiplier", "final_credit"):
                    self.assertEqual(a[code][key], b[code][key], f"{code}.{key}")
        finally:
            fresh.close()


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.world = build_world(self.store)
        self.eid = self.world["eid"]
        self.pid = self.world["period_id"]

    def tearDown(self):
        self.store.close()

    def _confirm(self):
        trial = self.store.trial(self.pid)
        self.store.submit(self.pid)
        self.store.confirm(self.pid, actor_role="企业申报员")
        return trial

    def test_confirm_freezes_inputs_and_creates_formal_entry(self):
        trial = self._confirm()
        period = self.store.get_period(self.pid)
        self.assertEqual(period["state"], CONFIRMED)
        self.assertEqual(period["input_hash"], trial["input_hash"])
        entry = self.store.entry_detail(period["formal_entry_id"])
        self.assertEqual(entry["kind"], "formal")
        self.assertEqual(entry["total_credit"], trial["total_credit"])
        self.assertEqual(len(entry["lines"]), 3)
        # 正式分录内嵌证据链：规则、政策、车型证据
        ev = entry["lines"][0]["evidence"]
        self.assertTrue(ev["rule_evidence"]["source_ref"])
        self.assertTrue(ev["policy_evidence"]["source_ref"])
        # 冻结后草稿期试算关闭
        with self.assertRaises(ConflictError):
            self.store.trial(self.pid)

    def test_concurrent_confirm_only_one_wins(self):
        self.store.trial(self.pid)
        self.store.submit(self.pid)
        winners, losers = [], []

        def attempt():
            try:
                self.store.confirm(self.pid, actor_role="企业申报员")
                winners.append(1)
            except StaleStateError:
                losers.append(1)

        threads = [threading.Thread(target=attempt) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 9)
        self.assertEqual(self.store.get_period(self.pid)["state"], CONFIRMED)

    def test_late_data_only_takes_effect_via_adjustment(self):
        trial = self._confirm()
        old_total = Decimal(trial["total_credit"])
        # 冻结后补报 M1 新能耗批次：版本留痕但不自动改动总分
        late = self.store.submit_model_version(
            enterprise_id=self.eid, year=YEAR, model_code="M1-BEV",
            version_type="energy",
            content={"energy_type": "BEV", "range_km": 450, "kwh_per_100km": "12.5"},
            evidence=EV_LATE, actor_role="企业申报员", batch_no="BATCH-LATE-09",
        )
        self.assertEqual(late["effective_only_via"], "adjustment")
        stmt0 = self.store.statement(self.eid, YEAR)
        self.assertEqual(Decimal(stmt0["current_total_credit"]), old_total)

        adj = self.store.create_adjustment(
            period_id=self.pid, adjustment_type="late_data",
            payload={"model_changes": [
                {"model_code": "M1-BEV", "energy_version_id": late["id"]},
            ]},
            reason="企业补报第9批能耗实测数据", evidence=EV_LATE,
            actor_role="企业申报员",
        )
        self.assertEqual(adj["status"], "requested")
        applied = self.store.apply_adjustment(adj["id"], actor_role="核算专员")
        self.assertEqual(applied["status"], "applied")
        self.assertNotEqual(applied["hash_before"], applied["hash_after"])

        stmt = self.store.statement(self.eid, YEAR)
        entries = {e["kind"]: e for e in stmt["entries"]}
        self.assertIn("formal", entries)
        self.assertIn("adjustment", entries)
        # 企业总额 = 正式分录 + 调整差额
        expect = Decimal(entries["formal"]["total_credit"]) + Decimal(entries["adjustment"]["total_credit"])
        self.assertEqual(Decimal(stmt["current_total_credit"]), expect)
        self.assertNotEqual(Decimal(stmt["current_total_credit"]), old_total)

        # 单车型追溯：正式贡献 + 调整链
        trace = self.store.model_trace(self.eid, YEAR, "M1-BEV")
        kinds = [c["entry_kind"] for c in trace["chain"]]
        self.assertEqual(kinds, ["formal", "adjustment"])
        self.assertFalse(trace["withdrawn"])
        net = sum((Decimal(c["final_credit"]) for c in trace["chain"]), Decimal("0"))
        self.assertEqual(Decimal(trace["net_contribution"]), net)

    def test_late_new_model_after_freeze(self):
        self._confirm()
        p = self.store.submit_model_version(
            enterprise_id=self.eid, year=YEAR, model_code="M4-FCV",
            version_type="production", content={"quantity": 300},
            evidence=EV_LATE, actor_role="企业申报员")
        e = self.store.submit_model_version(
            enterprise_id=self.eid, year=YEAR, model_code="M4-FCV",
            version_type="energy", content={"energy_type": "FCV"},
            evidence=EV_LATE, actor_role="企业申报员")
        adj = self.store.create_adjustment(
            period_id=self.pid, adjustment_type="late_data",
            payload={"model_changes": [{
                "model_code": "M4-FCV",
                "production_version_id": p["id"], "energy_version_id": e["id"],
            }]},
            reason="窗口关闭后补报新车型", evidence=EV_LATE, actor_role="企业申报员")
        self.store.apply_adjustment(adj["id"], actor_role="核算专员")
        trace = self.store.model_trace(self.eid, YEAR, "M4-FCV")
        self.assertEqual(Decimal(trace["net_contribution"]), Decimal("1200.00"))

    def test_withdrawal_reverses_model_via_adjustment(self):
        self._confirm()
        before = self.store.model_trace(self.eid, YEAR, "M2-PHEV")
        adj = self.store.create_adjustment(
            period_id=self.pid, adjustment_type="withdrawal",
            payload={"model_code": "M2-PHEV"}, reason="车型公告被撤销",
            evidence=EV_WITHDRAW, actor_role="企业申报员")
        self.store.apply_adjustment(adj["id"], actor_role="核算专员")
        after = self.store.model_trace(self.eid, YEAR, "M2-PHEV")
        self.assertTrue(after["withdrawn"])
        self.assertEqual(Decimal(after["net_contribution"]), Decimal("0.00"))
        delta = after["chain"][-1]
        self.assertEqual(Decimal(delta["final_credit"]), -Decimal(before["chain"][0]["final_credit"]))

    def test_rule_errata_is_published_as_new_version_and_applies_directly(self):
        trial = self._confirm()
        old_total = Decimal(trial["total_credit"])
        rv2 = self.store.publish_rule_version(
            content=RULE_V2_ERRATA, evidence=EV_ERRATA, actor_role="核算专员")
        adj = self.store.create_adjustment(
            period_id=self.pid, adjustment_type="rule_errata",
            payload={"rule_version_id": rv2["id"]},
            reason="BEV 续航门槛勘误：100 -> 80", evidence=EV_ERRATA,
            actor_role="核算专员")
        # 规则勘误由核算专员发起即生效，无需二次审核
        self.assertEqual(adj["status"], "applied")
        self.assertTrue(Decimal(adj["total_after"]) != old_total)
        detail = self.store.entry_detail(adj["entry_id"])
        self.assertEqual(detail["kind"], "adjustment")
        # 原正式分录纹丝不动
        period = self.store.get_period(self.pid)
        formal = self.store.entry_detail(period["formal_entry_id"])
        self.assertEqual(formal["total_credit"], trial["total_credit"])

    def test_policy_version_swap_before_confirm_is_allowed_but_frozen_after(self):
        # 窗口内发布新系数 → 试算立即采用最新版本
        self.store.publish_policy_version(year=YEAR, content=POLICY_V2, evidence=EV_POLICY,
                                          actor_role="核算专员")
        t = self.store.trial(self.pid)
        by_model = {ln["model_code"]: ln for ln in t["lines"]}
        self.assertEqual(by_model["M1-BEV"]["multiplier"], "1.2")
        self.store.submit(self.pid)
        self.store.confirm(self.pid, actor_role="企业申报员")
        # 再发新系数不影响已确认总额，只能走 late_data 调整单替换
        pv3 = self.store.publish_policy_version(
            year=YEAR, content={"nev_multiplier": "1.5", "cafc_multiplier": "1.0"},
            evidence=EV_POLICY, actor_role="核算专员")
        stmt = self.store.statement(self.eid, YEAR)
        self.assertEqual(len(stmt["entries"]), 1)
        adj = self.store.create_adjustment(
            period_id=self.pid, adjustment_type="late_data",
            payload={"model_changes": [], "policy_version_id": pv3["id"]},
            reason="年度政策系数追加公告", evidence=EV_POLICY, actor_role="企业申报员")
        self.store.apply_adjustment(adj["id"], actor_role="核算专员")
        self.assertEqual(len(self.store.statement(self.eid, YEAR)["entries"]), 2)


class RecomputeTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.world = build_world(self.store)
        self.pid = self.world["period_id"]
        self.eid = self.world["eid"]
        trial = self.store.trial(self.pid)
        self.store.submit(self.pid)
        self.store.confirm(self.pid, actor_role="企业申报员")
        self.total = trial["total_credit"]

    def tearDown(self):
        self.store.close()

    def test_recompute_verifies_identical(self):
        report = self.store.recompute_period(self.pid)
        self.assertTrue(report["verified"])
        self.assertEqual(report["recomputed_total"], self.total)
        self.assertEqual(report["content_hash_mismatches"], [])

    def test_tampering_with_version_content_is_detected(self):
        period = self.store.get_period(self.pid)
        frozen = period["frozen_snapshot"]
        victim = frozen["models"][0]["energy_version_id"]
        with self.store.tx():
            row = self.store.conn.execute("SELECT content FROM version WHERE id=?", (victim,)).fetchone()
            tampered = json.loads(row["content"])
            tampered["kwh_per_100km"] = "9.0"
            self.store.conn.execute(
                "UPDATE version SET content=? WHERE id=?",
                (json.dumps(tampered, ensure_ascii=False, sort_keys=True), victim))
        report = self.store.recompute_period(self.pid)
        self.assertFalse(report["verified"])
        self.assertIn(victim, report["content_hash_mismatches"])

    def test_recompute_adjustment_chain(self):
        rv2 = self.store.publish_rule_version(
            content=RULE_V2_ERRATA, evidence=EV_ERRATA, actor_role="核算专员")
        adj = self.store.create_adjustment(
            period_id=self.pid, adjustment_type="rule_errata",
            payload={"rule_version_id": rv2["id"]}, reason="勘误", evidence=EV_ERRATA,
            actor_role="核算专员")
        report = self.store.recompute_adjustment(adj["id"])
        self.assertTrue(report["verified"])
        self.assertEqual(report["recomputed_total"], adj["total_after"])
        self.assertEqual(report["stored_input_hash"], adj["hash_after"])


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.world = build_world(self.store)

    def tearDown(self):
        self.store.close()

    def test_missing_evidence_rejected(self):
        with self.assertRaises(ValidationError):
            self.store.submit_model_version(
                enterprise_id=self.world["eid"], year=YEAR, model_code="X",
                version_type="production", content={"quantity": 1},
                evidence={"source_type": "", "source_ref": ""}, actor_role="企业申报员")

    def test_submit_with_incomplete_models_rejected(self):
        store = Store(":memory:")
        try:
            ent = store.create_enterprise("空企业")
            store.open_period(ent["id"], YEAR)
            store.publish_rule_version(content=RULE_V1, evidence=EV_POLICY, actor_role="核算专员")
            store.publish_policy_version(year=YEAR, content=POLICY_V1, evidence=EV_POLICY,
                                         actor_role="核算专员")
            period = store.find_period(ent["id"], YEAR)
            with self.assertRaises(ConflictError):
                store.submit(period["id"])
        finally:
            store.close()

    def test_rule_engine_mismatch_rejected(self):
        bad = dict(RULE_V1)
        bad["engine"] = "9.9.9"
        with self.assertRaises(ValidationError):
            self.store.publish_rule_version(content=bad, evidence=EV_POLICY, actor_role="核算专员")

    def test_cannot_confirm_draft_directly(self):
        with self.assertRaises(StaleStateError):
            self.store.confirm(self.world["period_id"], actor_role="企业申报员")


if __name__ == "__main__":
    unittest.main()
