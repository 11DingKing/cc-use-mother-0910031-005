"""HTTP/JSON API（仅依赖标准库）。

路由总览：
  POST   /api/models/{model_id}/versions              登记车型参数新版本
  GET    /api/models/{model_id}                        车型版本历史与证据
  POST   /api/rules/{year}/versions                    发布核算规则版本（勘误即新版本）
  GET    /api/rules/{year}                             规则版本历史
  POST   /api/enterprises/{eid}/years/{year}/filing    开启年度申报
  POST   /api/filings/{fid}/trial                      可解释试算
  POST   /api/filings/{fid}/confirm                    企业确认：冻结输入+正式分录
  POST   /api/filings/{fid}/seal                       封存
  POST   /api/filings/{fid}/adjustments/late-data      迟到数据调整单
  POST   /api/filings/{fid}/adjustments/revoke         车型撤销调整单
  POST   /api/filings/{fid}/adjustments/rule-correction 规则勘误调整单
  GET    /api/filings/{fid}                            申报单状态
  GET    /api/filings/{fid}/ledger                     正式积分分录（只追加）
  GET    /api/filings/{fid}/adjustments                调整单列表
  GET    /api/filings/{fid}/reverify                   稳定复算自检
  GET    /api/regulator/enterprises/{eid}/years/{year}/trace 监管：总额→单车型追溯

所有写请求体中的 evidence 形如：
  {"source": "...", "batch": "...", "reference": "...", "payload": {...}}
"""
from __future__ import annotations

import decimal
import json
import re
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .errors import ConflictError, DomainError, NotFoundError, ValidationError
from .models import ModelStatus
from .service import CreditService
from .storage import Store


class Handler(BaseHTTPRequestHandler):
    service: CreditService  # 由 server 注入（类属性在 factory 中设置）

    server_version = "CreditService/1.0"

    # ------------------------------------------------------------ 工具方法

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return value

    def _evidence(self, body: dict[str, Any]):
        raw = body.get("evidence")
        if not isinstance(raw, dict):
            raise ValidationError("缺少 evidence 证据来源")
        for key in ("source", "batch", "reference"):
            if not isinstance(raw.get(key), str) or not raw[key]:
                raise ValidationError(f"evidence.{key} 必须是非空字符串")
        if "payload" not in raw:
            raise ValidationError("evidence.payload 不能为空（用于证据指纹）")
        return self.service.register_evidence(
            source=raw["source"],
            batch=raw["batch"],
            reference=raw["reference"],
            payload=raw["payload"],
        )

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)

    # -------------------------------------------------------------- 路由

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            body = self._read_body() if method == "POST" else {}
            for pattern, verbs, handler in ROUTES:
                match = pattern.fullmatch(path)
                if match and method in verbs:
                    handler(self, body, **match.groupdict())
                    return
            self._send(HTTPStatus.NOT_FOUND, {"error": f"无此路由：{method} {path}",
                                             "code": "not_found"})
        except DomainError as exc:
            status = {
                ValidationError: HTTPStatus.BAD_REQUEST,
                NotFoundError: HTTPStatus.NOT_FOUND,
                ConflictError: HTTPStatus.CONFLICT,
            }.get(type(exc), HTTPStatus.UNPROCESSABLE_ENTITY)
            self._send(status, {"error": str(exc), "code": exc.code})
        except (ValueError, TypeError, decimal.InvalidOperation) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc), "code": "bad_input"})

    # ----------------------------------------------------------- 各端点

    def h_add_model_version(self, body: dict[str, Any], **kw: Any) -> None:
        for key in ("enterprise_id", "name", "year", "volume", "energy"):
            if key not in body:
                raise ValidationError(f"缺少字段：{key}")
        record = self.service.add_model_version(
            model_id=kw["model_id"],
            enterprise_id=str(body["enterprise_id"]),
            name=str(body["name"]),
            year=int(body["year"]),
            volume=int(body["volume"]),
            energy=Decimal(str(body["energy"])),
            attrs=body.get("attrs") or {},
            status=ModelStatus(body.get("status", ModelStatus.ACTIVE.value)),
            evidence=self._evidence(body),
        )
        self._send(HTTPStatus.CREATED, record.to_dict())

    def h_list_model_versions(self, body: dict[str, Any], **kw: Any) -> None:
        versions = self.service.store.list_model_versions(kw["model_id"])
        latest = versions[-1]
        self._send(HTTPStatus.OK, {
            "model_id": kw["model_id"],
            "latest_version": latest.version,
            "status": latest.status.value,
            "versions": [v.to_dict() for v in versions],
        })

    def h_add_rule_version(self, body: dict[str, Any], **kw: Any) -> None:
        for key in ("policy_coef", "rate"):
            if key not in body:
                raise ValidationError(f"缺少字段：{key}")
        record = self.service.add_rule_version(
            year=int(kw["year"]),
            policy_coef=Decimal(str(body["policy_coef"])),
            rate=Decimal(str(body["rate"])),
            intercept=Decimal(str(body.get("intercept", "0"))),
            note=str(body.get("note", "")),
            evidence=self._evidence(body),
        )
        self._send(HTTPStatus.CREATED, record.to_dict())

    def h_list_rules(self, body: dict[str, Any], **kw: Any) -> None:
        rules = self.service.store.list_rules(int(kw["year"]))
        self._send(HTTPStatus.OK, {
            "year": int(kw["year"]),
            "latest_version": rules[-1].version if rules else None,
            "rules": [r.to_dict() for r in rules],
        })

    def h_open_filing(self, body: dict[str, Any], **kw: Any) -> None:
        filing = self.service.open_filing(str(kw["eid"]), int(kw["year"]))
        self._send(HTTPStatus.CREATED, filing.to_dict())

    def h_trial(self, body: dict[str, Any], **kw: Any) -> None:
        model_versions = body.get("model_versions")
        if not isinstance(model_versions, dict) or not model_versions:
            raise ValidationError("model_versions 必须是非空对象 {车型: 版本号}")
        report = self.service.trial(
            kw["fid"],
            model_versions={str(k): int(v) for k, v in model_versions.items()},
            rule_version=body.get("rule_version"),
        )
        self._send(HTTPStatus.OK, report)

    def h_confirm(self, body: dict[str, Any], **kw: Any) -> None:
        result = self.service.confirm(
            kw["fid"], expected_rev=body.get("expected_rev"))
        self._send(HTTPStatus.OK, {
            "filing": result.filing.to_dict(),
            "report": result.report,
            "entry_count": result.entry_count,
        })

    def h_seal(self, body: dict[str, Any], **kw: Any) -> None:
        filing = self.service.seal_filing(kw["fid"])
        self._send(HTTPStatus.OK, filing.to_dict())

    def h_late_data(self, body: dict[str, Any], **kw: Any) -> None:
        model_versions = body.get("model_versions")
        if not isinstance(model_versions, dict) or not model_versions:
            raise ValidationError("model_versions 必须是非空对象 {车型: 新版本号}")
        result = self.service.post_late_data(
            kw["fid"],
            model_versions={str(k): int(v) for k, v in model_versions.items()},
            reason=str(body.get("reason", "迟到批次数据补报")),
            evidence=self._evidence(body),
        )
        self._send(HTTPStatus.OK, result)

    def h_revoke(self, body: dict[str, Any], **kw: Any) -> None:
        if "model_id" not in body:
            raise ValidationError("缺少字段：model_id")
        result = self.service.revoke_model(
            kw["fid"],
            model_id=str(body["model_id"]),
            reason=str(body.get("reason", "车型撤销")),
            evidence=self._evidence(body),
            name=body.get("name"),
            energy=str(body.get("energy", "0")),
        )
        self._send(HTTPStatus.OK, result)

    def h_rule_correction(self, body: dict[str, Any], **kw: Any) -> None:
        for key in ("policy_coef", "rate"):
            if key not in body:
                raise ValidationError(f"缺少字段：{key}")
        result = self.service.correct_rule(
            kw["fid"],
            reason=str(body.get("reason", "核算规则勘误")),
            evidence=self._evidence(body),
            policy_coef=Decimal(str(body["policy_coef"])),
            rate=Decimal(str(body["rate"])),
            intercept=Decimal(str(body.get("intercept", "0"))),
        )
        self._send(HTTPStatus.OK, result)

    def h_get_filing(self, body: dict[str, Any], **kw: Any) -> None:
        filing = self.service.store.get_filing(kw["fid"])
        self._send(HTTPStatus.OK, filing.to_dict())

    def h_ledger(self, body: dict[str, Any], **kw: Any) -> None:
        filing = self.service.store.get_filing(kw["fid"])
        entries = self.service.store.list_entries(kw["fid"])
        total = sum((e.contribution for e in entries), Decimal("0"))
        self._send(HTTPStatus.OK, {
            "filing_id": kw["fid"],
            "entry_count": len(entries),
            "current_total": str(total),
            "entries": [e.to_dict() for e in entries],
            "year": filing.year,
        })

    def h_adjustments(self, body: dict[str, Any], **kw: Any) -> None:
        orders = self.service.store.list_adjustments(kw["fid"])
        self._send(HTTPStatus.OK, {
            "filing_id": kw["fid"],
            "count": len(orders),
            "adjustments": [o.to_dict() for o in orders],
        })

    def h_reverify(self, body: dict[str, Any], **kw: Any) -> None:
        self._send(HTTPStatus.OK, self.service.reverify(kw["fid"]))

    def h_trace(self, body: dict[str, Any], **kw: Any) -> None:
        result = self.service.regulator_trace(str(kw["eid"]), int(kw["year"]))
        self._send(HTTPStatus.OK, result)


def R(pattern: str, verbs: set[str], handler: Callable[..., Any]):
    return re.compile(pattern), verbs, handler


ROUTES = [
    R(r"^/api/models/(?P<model_id>[^/]+)/versions$", {"POST"}, Handler.h_add_model_version),
    R(r"^/api/models/(?P<model_id>[^/]+)$", {"GET"}, Handler.h_list_model_versions),
    R(r"^/api/rules/(?P<year>\d+)/versions$", {"POST"}, Handler.h_add_rule_version),
    R(r"^/api/rules/(?P<year>\d+)$", {"GET"}, Handler.h_list_rules),
    R(r"^/api/enterprises/(?P<eid>[^/]+)/years/(?P<year>\d+)/filing$",
      {"POST"}, Handler.h_open_filing),
    R(r"^/api/filings/(?P<fid>[^/]+)/trial$", {"POST"}, Handler.h_trial),
    R(r"^/api/filings/(?P<fid>[^/]+)/confirm$", {"POST"}, Handler.h_confirm),
    R(r"^/api/filings/(?P<fid>[^/]+)/seal$", {"POST"}, Handler.h_seal),
    R(r"^/api/filings/(?P<fid>[^/]+)/adjustments/late-data$",
      {"POST"}, Handler.h_late_data),
    R(r"^/api/filings/(?P<fid>[^/]+)/adjustments/revoke$",
      {"POST"}, Handler.h_revoke),
    R(r"^/api/filings/(?P<fid>[^/]+)/adjustments/rule-correction$",
      {"POST"}, Handler.h_rule_correction),
    R(r"^/api/filings/(?P<fid>[^/]+)/adjustments$", {"GET"}, Handler.h_adjustments),
    R(r"^/api/filings/(?P<fid>[^/]+)/ledger$", {"GET"}, Handler.h_ledger),
    R(r"^/api/filings/(?P<fid>[^/]+)/reverify$", {"GET"}, Handler.h_reverify),
    R(r"^/api/filings/(?P<fid>[^/]+)$", {"GET"}, Handler.h_get_filing),
    R(r"^/api/regulator/enterprises/(?P<eid>[^/]+)/years/(?P<year>\d+)/trace$",
      {"GET"}, Handler.h_trace),
]


def create_server(host: str, port: int, data_dir: str, *, quiet: bool = False) -> ThreadingHTTPServer:
    store = Store(data_dir)
    service = CreditService(store)

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = _Server((host, port), Handler)
    server.service = service          # type: ignore[attr-defined]
    server.quiet = quiet              # type: ignore[attr-defined]
    # Handler.service 作为回退类属性（实际通过 self.server.service 访问）
    Handler.service = service
    return server


def main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="车型年度积分核算服务")
    parser.add_argument("--host", default=os.environ.get("CREDIT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CREDIT_PORT", "8080")))
    parser.add_argument("--data-dir", default=os.environ.get("CREDIT_DATA", "./data/credit"))
    args = parser.parse_args()

    server = create_server(args.host, args.port, args.data_dir)
    print(f"积分核算服务监听 http://{args.host}:{args.port}，数据目录 {args.data_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
