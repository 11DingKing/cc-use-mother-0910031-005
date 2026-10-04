# 车型年度积分核算

面向汽车企业年度积分申报的完整 Python 服务端：把**产量、能耗参数、政策系数**建模为带证据的版本化输入；先做**可解释试算**，企业确认后**冻结输入并形成正式积分分录**；确认后的迟到数据、车型撤销、规则勘误和并发确认，只能通过**新版本 + 调整单**处理；监管方可从企业总额逐级追溯到单车型贡献；相同输入在任何时候**稳定复算**。

仅使用 Python 标准库（要求 Python ≥ 3.11），无第三方依赖。

## 领域规则如何落地

| 业务痛点 | 设计对策 |
| --- | --- |
| 不同批次数据（产量/能耗/系数）混在一起 | 每个车型、每条规则都是 append-only 版本链，版本间 `supersedes`，全部带 `Evidence`（来源/批次/公文号/报文指纹） |
| 已确认总分被补报反复改写 | 确认即把显式版本集合与规则版本**冻结进聚合**，原始分录只追加、永不修改 |
| 迟到数据 | 先登记车型参数新版本，再发 **迟到数据调整单**（逐车型差额，可负） |
| 车型撤销 | 发布「撤销」状态新版本，发 **撤销调整单** 冲回全部已确认贡献 |
| 规则勘误 | 发布规则新版本（旧版永久保留），按当前所有车型基线**全量重算**出差额调整单 |
| 并发确认 | 确认全流程在申报单聚合锁（`fcntl` 跨进程 + 线程锁）内完成，第二请求必拿 409 |
| 并发调整交错 | 调整引用版本必须**新于**当前基线（单调推进），杜绝旧版本把新版本冲回去 |
| 相同输入稳定复算 | Decimal 固定精度（`ROUND_HALF_UP`）、车型排序、规范 JSON+sha256 指纹、引擎版本号；`reverify` 重放全部调整单并核对分类账 |

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验（既有模块）。
- `src/credit_service/`：
  - `models.py` — 不可变领域模型（证据、车型版本、规则版本）。
  - `canonical.py` — 规范化序列化与内容指纹。
  - `engine.py` — 确定性核算引擎：试算报告、原始分录、调整单与调整分录。
  - `storage.py` — JSON append-only 存储、文件锁、申报聚合。
  - `service.py` — 用例编排与全部业务校验（核心）。
  - `api.py` — 标准库 HTTP/JSON API。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/demo.py`：全流程演示（试算→确认→迟到/撤销/勘误→复算→追溯）。
- `tests/`：契约、服务层（含并发）、HTTP 端到端回归测试。

## 快速开始

```bash
# 全流程演示，不用起服务
PYTHONPATH=src python3 tools/demo.py

# 启动 HTTP 服务（也可 pip install -e . 后用 credit-service）
PYTHONPATH=src python3 -m credit_service.api --port 8080 --data-dir ./data/credit
```

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/models/{id}/versions` | 登记车型参数新版本（迟到批次也走这里） |
| GET  | `/api/models/{id}` | 车型版本链（含每条证据与指纹） |
| POST | `/api/rules/{year}/versions` | 发布规则版本（勘误 = 新版本） |
| GET  | `/api/rules/{year}` | 规则版本链 |
| POST | `/api/enterprises/{eid}/years/{year}/filing` | 开启年度申报 |
| POST | `/api/filings/{fid}/trial` | 可解释试算（显式指定各车型版本） |
| POST | `/api/filings/{fid}/confirm` | 企业确认：冻结输入 + 原始正式分录 |
| POST | `/api/filings/{fid}/seal` | 年度封账（此后仍只接受调整单） |
| POST | `/api/filings/{fid}/adjustments/late-data` | 迟到数据调整单 |
| POST | `/api/filings/{fid}/adjustments/revoke` | 车型撤销调整单 |
| POST | `/api/filings/{fid}/adjustments/rule-correction` | 规则勘误调整单 |
| GET  | `/api/filings/{fid}` | 申报单状态与冻结基线 |
| GET  | `/api/filings/{fid}/ledger` | 正式积分分录（原始 + 调整，只追加） |
| GET  | `/api/filings/{fid}/adjustments` | 调整单列表 |
| GET  | `/api/filings/{fid}/reverify` | 稳定复算自检 |
| GET  | `/api/regulator/enterprises/{eid}/years/{year}/trace` | 监管追溯 |

写请求体均需携带证据，例如：

```json
{
  "enterprise_id": "E001",
  "name": "星驰600",
  "year": 2025,
  "volume": 1000,
  "energy": "6.0",
  "evidence": {
    "source": "工信部批次文件",
    "batch": "2025-B1",
    "reference": "公文-2025-B1",
    "payload": {"models": ["M-A"]}
  }
}
```

错误以 `400 validation_error` / `404 not_found` / `409 conflict` 返回。

## 核算公式与确定性

- 固定线性模型（由版本化规则解释）：
  `单车积分 = 政策系数 − 能耗系数 × 能耗 + 截距`，`车型贡献 = 单车积分 × 产量`。
- 精度：单车积分 4 位小数、分录金额 2 位，统一 `ROUND_HALF_UP`；企业总额 = 已舍入明细之和。
- 试算报告含逐车型 `formula`、所用参数版本、规则版本、证据与 `input_fingerprint`/`report_fingerprint`；指纹不含时间戳，只由引擎版本、规则版本和车型版本指纹决定。

## 验证

```bash
python3 -m unittest discover -s tests -v     # 19 个用例：生命周期/并发/复算/HTTP
python3 -m compileall -q src tools tests
PYTHONPATH=src python3 tools/check_contract.py domain/contract.json
```
