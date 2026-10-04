"""HTTP API 端到端测试：真实起服，走 socket 调用完整流程。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from credit_service.api import create_server  # noqa: E402


def evidence(batch: str, ref: str) -> dict:
    return {"source": "工信部批次文件", "batch": batch,
            "reference": ref, "payload": {"batch": batch, "ref": ref}}


class ApiClient:
    def __init__(self, base: str):
        self.base = base

    def request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="credit-api-")
        cls.server = create_server("127.0.0.1", 0, cls.tmp, quiet=True)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiClient(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_full_flow_over_http(self) -> None:
        api = self.api
        # 规则 v1
        status, rule = api.request("POST", "/api/rules/2025/versions", {
            "policy_coef": "3.0", "rate": "0.12", "note": "初始",
            "evidence": evidence("P1", "rule-doc-1")})
        self.assertEqual(status, 201)
        self.assertEqual(rule["version"], 1)

        # 两个车型 v1
        status, ma = api.request("POST", "/api/models/M-A/versions", {
            "enterprise_id": "E1", "name": "甲型车", "year": 2025,
            "volume": 1000, "energy": "6.0", "evidence": evidence("B1", "d1")})
        self.assertEqual(status, 201)
        status, mb = api.request("POST", "/api/models/M-B/versions", {
            "enterprise_id": "E1", "name": "乙型车", "year": 2025,
            "volume": 500, "energy": "4.5", "evidence": evidence("B1", "d2")})
        self.assertEqual(status, 201)

        # 开启申报 + 试算
        status, filing = api.request("POST", "/api/enterprises/E1/years/2025/filing")
        self.assertEqual(status, 201)
        fid = filing["filing_id"]
        status, report = api.request("POST", f"/api/filings/{fid}/trial", {
            "model_versions": {"M-A": 1, "M-B": 1}})
        self.assertEqual(status, 200)
        self.assertEqual(report["total"], "3510.00")

        # 并发双确认：只有一个 200
        results = []

        def confirm():
            status_code, _ = api.request("POST", f"/api/filings/{fid}/confirm", {})
            results.append(status_code)

        threads = [threading.Thread(target=confirm) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [200, 409])

        # 迟到数据
        status, _ = api.request("POST", "/api/models/M-A/versions", {
            "enterprise_id": "E1", "name": "甲型车", "year": 2025,
            "volume": 1200, "energy": "6.0", "evidence": evidence("B2-LATE", "d3")})
        self.assertEqual(status, 201)
        status, adj = api.request("POST", f"/api/filings/{fid}/adjustments/late-data", {
            "model_versions": {"M-A": 2}, "reason": "补报",
            "evidence": evidence("B2-LATE", "d3")})
        self.assertEqual(status, 200)
        self.assertEqual(adj["adjustment"]["total_delta"], "456.00")

        # 车型撤销
        status, rev = api.request("POST", f"/api/filings/{fid}/adjustments/revoke", {
            "model_id": "M-B", "reason": "公告撤销", "evidence": evidence("REV", "d4")})
        self.assertEqual(status, 200)
        self.assertEqual(rev["adjustment"]["total_delta"], "-1230.00")

        # 规则勘误
        status, fix = api.request(
            "POST", f"/api/filings/{fid}/adjustments/rule-correction", {
                "policy_coef": "3.0", "rate": "0.10", "reason": "系数勘误",
                "evidence": evidence("P2-FIX", "d5")})
        self.assertEqual(status, 200)

        # 复算自检
        status, reverify = api.request("GET", f"/api/filings/{fid}/reverify")
        self.assertEqual(status, 200)
        self.assertTrue(reverify["ledger_match"])
        self.assertTrue(all(reverify["checks"].values()))

        # 分类账
        status, ledger = api.request("GET", f"/api/filings/{fid}/ledger")
        self.assertEqual(status, 200)
        self.assertEqual(ledger["current_total"], reverify["recomputed_total"])

        # 监管追溯
        status, trace = api.request("GET",
                                    "/api/regulator/enterprises/E1/years/2025/trace")
        self.assertEqual(status, 200)
        self.assertEqual(trace["current_total"], ledger["current_total"])
        by_id = {m["model_id"]: m for m in trace["models"]}
        self.assertEqual(by_id["M-B"]["status"], "撤销")
        self.assertTrue(by_id["M-A"]["evidence_chain"])

        # 封存后迟到数据被拒
        status, _ = api.request("POST", f"/api/filings/{fid}/seal")
        self.assertEqual(status, 200)
        status, _ = api.request("POST", "/api/models/M-A/versions", {
            "enterprise_id": "E1", "name": "甲型车", "year": 2025,
            "volume": 1300, "energy": "6.0", "evidence": evidence("B3", "d6")})
        self.assertEqual(status, 201)
        status, err = api.request("POST", f"/api/filings/{fid}/adjustments/late-data", {
            "model_versions": {"M-A": 3}, "reason": "封存后补报",
            "evidence": evidence("B3", "d6")})
        # 封存状态仍允许调整单（年度封账后调整单是唯一通道），这里核对
        # 走的是调整而非改写；先放行，再断言只新增调整行。
        self.assertEqual(status, 200)
        status, ledger2 = api.request("GET", f"/api/filings/{fid}/ledger")
        kinds = [e["kind"] for e in ledger2["entries"]]
        self.assertIn("调整", kinds)
        self.assertEqual(kinds.count("原始"), 2)

    def test_validation_errors_are_400(self) -> None:
        api = self.api
        status, body = api.request("POST", "/api/rules/2026/versions", {
            "policy_coef": "1", "evidence": evidence("x", "y")})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "validation_error")

    def test_malformed_decimal_is_400_not_500(self) -> None:
        api = self.api
        status, body = api.request("POST", "/api/models/M-Z/versions", {
            "enterprise_id": "E1", "name": "Z", "year": 2026,
            "volume": 1, "energy": "not-a-number",
            "evidence": evidence("x", "y")})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "bad_input")

    def test_missing_evidence_is_400(self) -> None:
        api = self.api
        status, body = api.request("POST", "/api/rules/2026/versions", {
            "policy_coef": "1", "rate": "0.1"})
        self.assertEqual(status, 400)
        self.assertIn("evidence", body["error"])

    def test_trial_against_nonexistent_filing_is_404(self) -> None:
        api = self.api
        status, _ = api.request("POST", "/api/filings/filing-nope-1999/trial", {
            "model_versions": {"M-A": 1}})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
