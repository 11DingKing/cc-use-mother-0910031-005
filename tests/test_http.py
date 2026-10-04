"""HTTP API 集成测试：真实起服、真实并发、角色边界。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from credit_accounting.api import make_server
from credit_accounting.store import Store
from test_service import (
    EV_BATCH, EV_ERRATA, EV_LATE, EV_POLICY, EV_WITHDRAW, POLICY_V1, RULE_V1, YEAR,
)


class HttpSession:
    def __init__(self, base: str, role: str):
        self.base, self.role = base, role

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", "X-Actor-Role": self.role},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        cls.httpd = make_server("127.0.0.1", 0, cls.store, quiet=True)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.close()

    def test_01_full_flow_and_concurrency(self):
        acc = HttpSession(self.base, "accountant")
        ent = HttpSession(self.base, "enterprise")
        opr = HttpSession(self.base, "operator")
        aud = HttpSession(self.base, "auditor")

        status, body = acc.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["engine_version"], "1.0.0")

        # 角色边界：企业不能建档
        status, _ = ent.call("POST", "/api/enterprises", {"name": "X"})
        self.assertEqual(status, 403)
        status, created = acc.call("POST", "/api/enterprises", {"name": "某汽车集团"})
        self.assertEqual(status, 200)
        eid = created["id"]

        status, period = acc.call("POST", f"/api/enterprises/{eid}/periods", {"year": YEAR})
        self.assertEqual(status, 200)
        pid = period["id"]

        status, rule = acc.call("POST", "/api/rule-versions", {"content": RULE_V1, "evidence": EV_POLICY})
        self.assertEqual(status, 200)
        status, policy = acc.call("POST", "/api/policy-versions",
                                  {"year": YEAR, "content": POLICY_V1, "evidence": EV_POLICY})
        self.assertEqual(status, 200)

        def model_version(code, vt, content):
            status, v = ent.call(
                "POST", f"/api/enterprises/{eid}/years/{YEAR}/model-versions",
                {"model_code": code, "version_type": vt, "content": content,
                 "evidence": EV_BATCH, "batch_no": "B1"},
            )
            self.assertEqual(status, 200, v)
            return v["id"]

        model_version("M1-BEV", "production", {"quantity": 1000})
        model_version("M1-BEV", "energy",
                      {"energy_type": "BEV", "range_km": 400, "kwh_per_100km": "13.0"})

        # 可解释试算
        status, trial = ent.call("POST", f"/api/periods/{pid}/trials")
        self.assertEqual(status, 200)
        self.assertEqual(trial["total_credit"], "3200.00")
        self.assertIn("steps", trial["lines"][0])

        # 提交 + 并发确认
        status, _ = ent.call("POST", f"/api/periods/{pid}/submit")
        self.assertEqual(status, 200)
        results = []

        def confirm():
            results.append(ent.call("POST", f"/api/periods/{pid}/confirm")[0])

        threads = [threading.Thread(target=confirm) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results).count(200), 1)
        self.assertEqual(sorted(results).count(409), 7)

        # 迟到数据调整
        status, late = ent.call(
            "POST", f"/api/enterprises/{eid}/years/{YEAR}/model-versions",
            {"model_code": "M1-BEV", "version_type": "energy",
             "content": {"energy_type": "BEV", "range_km": 450, "kwh_per_100km": "12.5"},
             "evidence": EV_LATE, "batch_no": "B-LATE"})
        self.assertEqual(status, 200)
        self.assertEqual(late["effective_only_via"], "adjustment")
        status, adj = ent.call(
            "POST", f"/api/periods/{pid}/adjustments",
            {"adjustment_type": "late_data",
             "payload": {"model_changes": [{"model_code": "M1-BEV", "energy_version_id": late["id"]}]},
             "reason": "补报实测能耗", "evidence": EV_LATE})
        self.assertEqual(status, 200)
        self.assertEqual(adj["status"], "requested")
        # 企业不能自审
        status, _ = ent.call("POST", f"/api/adjustments/{adj['id']}/apply")
        self.assertEqual(status, 403)
        status, applied = acc.call("POST", f"/api/adjustments/{adj['id']}/apply")
        self.assertEqual(status, 200)
        self.assertEqual(applied["status"], "applied")

        # 撤销需要先补一辆车
        p2 = model_version("M2-PHEV", "production", {"quantity": 500})
        e2 = model_version("M2-PHEV", "energy", {"energy_type": "PHEV", "range_km": 80})
        status, adj2 = ent.call(
            "POST", f"/api/periods/{pid}/adjustments",
            {"adjustment_type": "late_data",
             "payload": {"model_changes": [{"model_code": "M2-PHEV",
                                            "production_version_id": p2, "energy_version_id": e2}]},
             "reason": "补报车型", "evidence": EV_LATE})
        acc.call("POST", f"/api/adjustments/{adj2['id']}/apply")
        status, wd = ent.call(
            "POST", f"/api/periods/{pid}/adjustments",
            {"adjustment_type": "withdrawal", "payload": {"model_code": "M2-PHEV"},
             "reason": "公告撤销", "evidence": EV_WITHDRAW})
        acc.call("POST", f"/api/adjustments/{wd['id']}/apply")

        # 规则勘误：核算专员直发即生效
        rule_v2 = {**RULE_V1, "bev": {**RULE_V1["bev"], "range_threshold_km": 80}}
        status, rv2 = acc.call("POST", "/api/rule-versions",
                               {"content": rule_v2, "evidence": EV_ERRATA})
        self.assertEqual(status, 200)
        status, er = acc.call(
            "POST", f"/api/periods/{pid}/adjustments",
            {"adjustment_type": "rule_errata", "payload": {"rule_version_id": rv2["id"]},
             "reason": "续航门槛勘误", "evidence": EV_ERRATA})
        self.assertEqual(status, 200)
        self.assertEqual(er["status"], "applied")

        # 运营过账 → 封存
        status, _ = opr.call("POST", f"/api/periods/{pid}/post")
        self.assertEqual(status, 200)
        status, _ = opr.call("POST", f"/api/periods/{pid}/seal")
        self.assertEqual(status, 200)

        # 监管：企业总额对账单
        status, stmt = aud.call("GET", f"/api/enterprises/{eid}/years/{YEAR}/statement")
        self.assertEqual(status, 200)
        self.assertEqual(stmt["state"], "已封存")
        self.assertEqual(len(stmt["entries"]), 5)  # 正式 + 两次迟到 + 撤销 + 勘误

        # 监管：单车型追溯（含证据链）
        status, trace = aud.call(
            "GET", f"/api/enterprises/{eid}/years/{YEAR}/models/M1-BEV/trace")
        self.assertEqual(status, 200)
        self.assertTrue(trace["chain"][0]["evidence"]["rule_evidence"]["source_ref"])

        # 监管：稳定复算
        status, rec = aud.call("POST", f"/api/periods/{pid}/recompute")
        self.assertEqual(status, 200)
        self.assertTrue(rec["verified"])

        # 未认证请求被拒
        req = urllib.request.Request(self.base + f"/api/periods/{pid}")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

        # 路由不存在
        status, body = acc.call("GET", "/api/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
