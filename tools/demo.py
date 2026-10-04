"""端到端演示：试算 → 确认冻结 → 迟到数据/撤销/规则勘误 → 复算 → 监管追溯。

用法：python3 tools/demo.py [数据目录]
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from credit_service import CreditService, Store  # noqa: E402


def show(title: str, payload) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    data_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="credit-demo-")

    svc = CreditService(Store(data_dir))

    ev_init = svc.register_evidence(
        source="工信部批次文件", batch="POLICY-2025-0", reference="公告2025-0号",
        payload={"doc": "初始政策系数"})
    svc.add_rule_version(year=2025, policy_coef="3.0", rate="0.12",
                         note="2025 年度核算规则初版", evidence=ev_init)
    ev_b1 = svc.register_evidence(
        source="工信部批次文件", batch="2025-B1", reference="公文-2025-B1",
        payload={"models": ["M-A", "M-B"]})
    svc.add_model_version(model_id="M-A", enterprise_id="E001", name="星驰 600",
                          year=2025, volume=1000, energy="6.0", evidence=ev_b1)
    svc.add_model_version(model_id="M-B", enterprise_id="E001", name="星澜 450",
                          year=2025, volume=500, energy="4.5", evidence=ev_b1)

    svc.open_filing("E001", 2025)
    fid = "filing-E001-2025"
    trial = svc.trial(fid, model_versions={"M-A": 1, "M-B": 1})
    show("① 可解释试算（草稿，未冻结）", {
        "total": trial["total"],
        "lines": [{k: l[k] for k in
                   ("model_id", "model_version", "volume", "energy",
                    "unit_credit", "contribution", "formula", "reason")}
                  for l in trial["lines"]],
        "input_fingerprint": trial["input_fingerprint"],
    })

    confirmed = svc.confirm(fid)
    show("② 企业确认：输入冻结，原始正式分录入账", {
        "state": confirmed.filing.state.value,
        "total": confirmed.report["total"],
        "entry_count": confirmed.entry_count,
        "confirmed_at": confirmed.filing.confirmed_at,
    })

    ev_b2 = svc.register_evidence(
        source="工信部批次文件", batch="2025-B2-LATE", reference="公文-2025-B2",
        payload={"model": "M-A", "corrected_volume": 1200})
    svc.add_model_version(model_id="M-A", enterprise_id="E001", name="星驰 600",
                          year=2025, volume=1200, energy="6.0", evidence=ev_b2)
    late = svc.post_late_data(fid, model_versions={"M-A": 2},
                              reason="第三批产量数据迟到补报", evidence=ev_b2)
    show("③ 迟到数据 → 新版本 + 调整单（原始分录不动）", {
        "adjustment_id": late["adjustment"]["adjustment_id"],
        "kind": late["adjustment"]["kind"],
        "total_delta": late["adjustment"]["total_delta"],
        "new_total_preview": late["new_total_preview"],
    })

    ev_rev = svc.register_evidence(
        source="市场监管总局召回平台", batch="REVOKE-2025-9", reference="撤销令-9号",
        payload={"model": "M-B"})
    revoked = svc.revoke_model(fid, model_id="M-B", reason="车型资质撤销", evidence=ev_rev)
    show("④ 车型撤销 → 撤销版本 + 冲回调整单", {
        "kind": revoked["adjustment"]["kind"],
        "total_delta": revoked["adjustment"]["total_delta"],
    })

    ev_fix = svc.register_evidence(
        source="工信部政策勘误", batch="POLICY-2025-1", reference="公告2025-1号",
        payload={"rate": "0.10"})
    fixed = svc.correct_rule(fid, reason="能耗系数 0.12 勘误为 0.10",
                             policy_coef="3.0", rate="0.10", evidence=ev_fix)
    show("⑤ 规则勘误 → 新规则版本 + 全量重算调整单", {
        "from_rule_version": fixed["adjustment"]["from_rule_version"],
        "to_rule_version": fixed["adjustment"]["to_rule_version"],
        "total_delta": fixed["adjustment"]["total_delta"],
    })

    reverify = svc.reverify(fid)
    show("⑥ 相同输入稳定复算（基线指纹 + 调整单重放 + 分类账核对）", reverify)

    trace = svc.regulator_trace("E001", 2025)
    show("⑦ 监管追溯：企业总额 → 单车型贡献 → 证据链", {
        "confirmed_total": trace["confirmed_total"],
        "current_total": trace["current_total"],
        "models": [{
            "model_id": m["model_id"],
            "name": m["name"],
            "status": m["status"],
            "original": m["original"],
            "net_contribution": m["net_contribution"],
            "adjustments": m["adjustments"],
            "evidence_refs": [e["reference"] for e in m["evidence_chain"]],
        } for m in trace["models"]],
    })

    print(f"\n数据目录：{data_dir}")


if __name__ == "__main__":
    main()
