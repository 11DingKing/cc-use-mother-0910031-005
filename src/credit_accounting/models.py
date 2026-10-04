"""领域常量：角色、期间状态、能源类型、调整单类型。

取值与 ``domain/contract.json`` 中的 actors / states 严格一致。
"""
from __future__ import annotations

# ---- 年度期间状态（契约 states） ----
DRAFT = "草稿"          # 窗口开启，可反复提交批次、试算
PENDING = "待核算"      # 企业提交，等待确认
CONFIRMED = "已确认"    # 企业确认，输入冻结，正式分录已生成
EXECUTING = "执行中"    # 交易运营已过账，进入结算执行
SEALED = "已封存"       # 年度封存，只允许追加调整单

PERIOD_STATES = (DRAFT, PENDING, CONFIRMED, EXECUTING, SEALED)

# ---- 角色（契约 actors） ----
ROLE_ENTERPRISE = "企业申报员"
ROLE_ACCOUNTANT = "核算专员"
ROLE_OPERATOR = "交易运营员"
ROLE_AUDITOR = "监管审计员"

# HTTP 请求头 X-Actor-Role 允许的代码与中文名双向映射
ROLE_CODES = {
    "enterprise": ROLE_ENTERPRISE,
    "accountant": ROLE_ACCOUNTANT,
    "operator": ROLE_OPERATOR,
    "auditor": ROLE_AUDITOR,
}
ROLE_NAMES = {name: name for name in ROLE_CODES.values()}

# ---- 车型能源类型 ----
ENERGY_BEV = "BEV"      # 纯电动
ENERGY_PHEV = "PHEV"    # 插电混动
ENERGY_FCV = "FCV"      # 燃料电池
ENERGY_ICE = "ICE"      # 传统燃油（产生 CAFC 负积分）
ENERGY_TYPES = (ENERGY_BEV, ENERGY_PHEV, ENERGY_FCV, ENERGY_ICE)
NEV_TYPES = (ENERGY_BEV, ENERGY_PHEV, ENERGY_FCV)

# ---- 证据来源类型 ----
EVIDENCE_BATCH = "批次申报"
EVIDENCE_POLICY = "政策公告"
EVIDENCE_ERRATA = "规则勘误"
EVIDENCE_REVOCATION = "撤销文件"

# ---- 调整单类型 ----
ADJ_LATE_DATA = "late_data"        # 迟到数据（新版本参数）
ADJ_WITHDRAWAL = "withdrawal"      # 车型撤销
ADJ_RULE_ERRATA = "rule_errata"    # 核算规则勘误
ADJUSTMENT_TYPES = (ADJ_LATE_DATA, ADJ_WITHDRAWAL, ADJ_RULE_ERRATA)

# ---- 分录类型 ----
ENTRY_FORMAL = "formal"
ENTRY_ADJUSTMENT = "adjustment"
