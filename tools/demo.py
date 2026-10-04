"""端到端叙事演示：直接编排 Api 层，打印完整业务链路。

运行：python3 tools/demo.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from credit_accounting.api import Api
from credit_accounting.store import Store

YEAR = 2025
RULE = json.loads((ROOT / "domain" / "rule_example_2025.json").read_text(encoding="utf-8"))
POLICY = {"nev_multiplier": "1.0", "cafc_multiplier": "1.0"}


def show(title: str, payload) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    tmp = Path(tempfile.mkdtemp()) / "demo.db"
    store = Store(tmp)
    api = Api(store)
    call = api.dispatch  # (method, path, query, body, role)

    ent = call("POST", "/api/enterprises", {}, {"name": "某汽车集团"}, "核算专员")
    period = call("POST", f"/api/enterprises/{ent['id']}/periods", {}, {"year": YEAR}, "核算专员")
    eid, pid = ent["id"], period["id"]
    call("POST", "/api/rule-versions", {}, {"content": RULE,
         "evidence": {"source_type": "政策公告", "source_ref": "2025-积分办法"}}, "核算专员")
    call("POST", "/api/policy-versions", {}, {"year": YEAR, "content": POLICY,
         "evidence": {"source_type": "政策公告", "source_ref": "2025-系数公告"}}, "核算专员")

    def batch(code, vt, content, ref):
        return call("POST", f"/api/enterprises/{eid}/years/{YEAR}/model-versions", {},
                    {"model_code": code, "version_type": vt, "content": content,
                     "evidence": {"source_type": "批次申报", "source_ref": ref},
                     "batch_no": ref}, "企业申报员")

    batch("S1-BEV", "production", {"quantity": 1000}, "BATCH-01")
    batch("S1-BEV", "energy", {"energy_type": "BEV", "range_km": 400, "kwh_per_100km": "13.0"}, "BATCH-01")
    batch("S2-ICE", "production", {"quantity": 2000}, "BATCH-01")
    batch("S2-ICE", "energy", {"energy_type": "ICE", "fuel_l_per_100km": "7.2",
                               "target_l_per_100km": "6.9"}, "BATCH-01")

    show("① 可解释试算（未确认，不产生分录）", call("POST", f"/api/periods/{pid}/trials", {}, {}, "企业申报员"))
    call("POST", f"/api/periods/{pid}/submit", {}, {}, "企业申报员")
    call("POST", f"/api/periods/{pid}/confirm", {}, {}, "企业申报员")
    show("② 企业确认：输入冻结、正式分录入账", store.statement(eid, YEAR))

    late = batch("S1-BEV", "energy",
                 {"energy_type": "BEV", "range_km": 450, "kwh_per_100km": "12.5"}, "BATCH-LATE-09")
    show("③ 冻结后补报：版本留痕但不改变总分", late)
    adj = call("POST", f"/api/periods/{pid}/adjustments", {},
               {"adjustment_type": "late_data",
                "payload": {"model_changes": [{"model_code": "S1-BEV", "energy_version_id": late["id"]}]},
                "reason": "企业补报第9批实测能耗",
                "evidence": {"source_type": "批次申报", "source_ref": "BATCH-LATE-09"}}, "企业申报员")
    call("POST", f"/api/adjustments/{adj['id']}/apply", {}, {}, "核算专员")

    wd = call("POST", f"/api/periods/{pid}/adjustments", {},
              {"adjustment_type": "withdrawal", "payload": {"model_code": "S2-ICE"},
               "reason": "油耗公告撤销",
               "evidence": {"source_type": "撤销文件", "source_ref": "WD-2025-7"}}, "企业申报员")
    call("POST", f"/api/adjustments/{wd['id']}/apply", {}, {}, "核算专员")
    show("④ 迟到数据 + 车型撤销后的企业总额对账单", store.statement(eid, YEAR))
    show("⑤ 监管追溯：单车型贡献链与证据",
         call("GET", f"/api/enterprises/{eid}/years/{YEAR}/models/S1-BEV/trace", {}, {}, "监管审计员"))
    show("⑥ 相同输入稳定复算（哈希+逐行核对）",
         call("POST", f"/api/periods/{pid}/recompute", {}, {}, "监管审计员"))
    store.close()
    print(f"\n演示数据库保留在：{tmp}")


if __name__ == "__main__":
    main()
