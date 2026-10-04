# 车型年度积分核算

年度申报窗口开启后，车型产量、能耗参数与政策系数来自不同批次，迟到补报会让已确认的企业总分反复变化。本服务用**版本化不可变输入 + 先试算后冻结 + 只追加调整单**解决该问题，并向监管侧提供从企业总额到单车型贡献的完整追溯与确定性复算。

仅依赖 Python 3.11+ 标准库（SQLite + http.server），无第三方依赖。

## 核心不变量（对应 `domain/contract.json`）

| 不变量 | 落地方式 |
| --- | --- |
| 车型参数版本 | 产量/能耗/政策系数/核算规则全部为只追加版本，携带证据（来源类型+文号）、批次号、适用年度与 `supersedes_id` 版本链 |
| 核算规则冻结 | 确认时把版本指针与每个版本的内容 SHA-256 固化为冻结快照；正式分录自存证据，之后任何输入都不可改写 |
| 积分调整分录 | 迟到数据、车型撤销、规则勘误一律生成 `adjustment` 差额分录并逐单成链；正式分录永远保持原值 |
| 贡献来源追溯 | 监管对账单：企业总额 = 正式分录 + 全部调整分录；单车型接口可下钻到每一步公式、版本与证据文号 |

确定性：核算引擎是纯函数，全程 `Decimal` + `ROUND_HALF_UP`，输出绑定 `engine_version` 与输入快照哈希；相同输入重放结果逐分一致，任何版本内容被篡改都会在复算时暴露为内容哈希不匹配。

## 状态机

```
草稿 ──企业提交──▶ 待核算 ──企业确认──▶ 已确认 ──运营过账──▶ 执行中 ──▶ 已封存
                     │  (并发确认仅一笔成功，落败方 409)
冻结后：迟到数据/车型撤销 → 企业申请调整单 → 核算专员审核 → 差额分录入账
        规则勘误       → 核算专员发布勘误版本 → 直发即生效
```

## 目录

- `domain/contract.json`：领域角色、状态、不变量与样例。
- `domain/rule_example_2025.json`：示例核算规则（阈值随规则版本演进）。
- `src/domain_contract/`：契约读取与校验。
- `src/credit_accounting/`：
  - `engine.py`：确定性核算引擎（纯函数、逐步解释、输入哈希）。
  - `store.py`：SQLite 存储（事务、条件更新、冻结快照、调整链、复算）。
  - `api.py` / `__main__.py`：HTTP API 与启动入口。
- `tools/check_contract.py`：契约命令行检查；`tools/demo.py`：端到端叙事演示。
- `tests/`：契约回归、业务全路径（含 10 线程并发确认）、HTTP 集成测试。

## 启动

```bash
python3 -m credit_accounting --db credit.db --port 8080   # 需把 src 加入 PYTHONPATH
PYTHONPATH=src python3 -m credit_accounting --port 8080
```

所有接口通过 `X-Actor-Role` 头标识角色：`enterprise`（企业申报员）、`accountant`（核算专员）、`operator`（交易运营员）、`auditor`（监管审计员）。

## API 一览

| 方法 路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /api/enterprises` / `POST /api/enterprises/{eid}/periods` | 核算专员 | 建档、开启年度窗口 |
| `POST /api/rule-versions` | 核算专员 | 发布规则版本（勘误证据须为「规则勘误」） |
| `POST /api/policy-versions` | 核算专员 | 发布年度政策系数版本 |
| `POST /api/enterprises/{eid}/years/{y}/model-versions` | 企业申报员 | 提交产量/能耗批次（草稿期自动入快照；冻结后仅留痕，标记 `effective_only_via=adjustment`） |
| `POST /api/periods/{pid}/trials` | 企业/核算 | **可解释试算**：返回逐步公式、版本指针、输入哈希，不落分录 |
| `POST /api/periods/{pid}/submit` · `/confirm` | 企业申报员 | 提交；确认即冻结并生成正式分录（乐观锁条件更新） |
| `POST /api/periods/{pid}/post` · `/seal` | 交易运营员 | 过账、封存 |
| `POST /api/periods/{pid}/adjustments` | 企业（迟到/撤销）· 核算专员（勘误） | 创建调整单 |
| `POST /api/adjustments/{aid}/apply` · `/reject` | 核算专员 | 审核调整单，应用时生成差额分录与新有效快照 |
| `GET /api/enterprises/{eid}/years/{y}/statement` | 监管审计员 | 企业年度总额对账单（正式+调整逐笔列示） |
| `GET /api/enterprises/{eid}/years/{y}/models/{code}/trace` | 监管审计员 | 单车型贡献链：正式贡献、历次差额、证据文号 |
| `POST /api/periods/{pid}/recompute` · `/api/adjustments/{aid}/recompute` | 监管/核算 | 稳定复算：内容哈希、输入哈希、总额、逐行四重核对 |
| `GET /api/entries/{eid}` · `/api/versions/...` | 按角色 | 分录明细（含 steps 与证据）、版本链查询 |

## 核算规则（随版本演进，引擎保持纯函数）

- BEV：`续航分=(续航-门槛)/100`；`能耗系数=clip(1+(参考能耗-实际)/参考×敏感度, 下限, 上限)`；单车积分 = 两者乘积。
- PHEV：`基础分 + (续航-门槛)/100`；FCV：规则固定单车积分；ICE：`(目标油耗-实际)/目标×CAFC权重`（超标为负）。
- 车型小计 × 年度政策系数（NEV/CAFC 分开）= 车型最终贡献。

## 验证

```bash
python3 -m unittest discover -s tests -v     # 18 个测试：契约 + 业务 + HTTP（含并发确认）
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
python3 tools/demo.py                        # 打印完整业务链路
```
