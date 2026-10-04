"""HTTP API（仅依赖标准库）。

角色通过请求头 ``X-Actor-Role`` 传递（enterprise/accountant/operator/auditor），
服务端按领域契约强制角色边界。所有写接口均为 JSON。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .errors import AppError, NotFoundError, PermissionError
from .models import (
    ADJ_LATE_DATA,
    ADJ_RULE_ERRATA,
    ADJ_WITHDRAWAL,
    ROLE_ACCOUNTANT,
    ROLE_AUDITOR,
    ROLE_CODES,
    ROLE_ENTERPRISE,
    ROLE_OPERATOR,
)
from .store import Store


class Api:
    """路由与处理器的薄编排层，便于在测试中直接调用。"""

    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------ 路由

    def dispatch(self, method: str, path: str, query: dict, body: dict, role: str | None):
        rules = [
            ("GET", r"^/api/health$", self.health),
            ("POST", r"^/api/enterprises$", self.create_enterprise),
            ("GET", r"^/api/enterprises$", self.list_enterprises),
            ("POST", r"^/api/enterprises/(?P<eid>[^/]+)/periods$", self.open_period),
            ("GET", r"^/api/enterprises/(?P<eid>[^/]+)/periods/(?P<year>\d{4})$", self.find_period),
            ("GET", r"^/api/enterprises/(?P<eid>[^/]+)/years/(?P<year>\d{4})/statement$", self.statement),
            ("GET", r"^/api/enterprises/(?P<eid>[^/]+)/years/(?P<year>\d{4})/models/(?P<code>[^/]+)/trace$", self.model_trace),
            ("POST", r"^/api/enterprises/(?P<eid>[^/]+)/years/(?P<year>\d{4})/model-versions$", self.submit_model_version),
            ("POST", r"^/api/policy-versions$", self.publish_policy),
            ("POST", r"^/api/rule-versions$", self.publish_rule),
            ("GET", r"^/api/versions$", self.list_versions),
            ("GET", r"^/api/versions/(?P<vid>[^/]+)$", self.get_version),
            ("GET", r"^/api/periods/(?P<pid>[^/]+)$", self.get_period),
            ("POST", r"^/api/periods/(?P<pid>[^/]+)/trials$", self.trial),
            ("GET", r"^/api/periods/(?P<pid>[^/]+)/trials$", self.list_trials),
            ("POST", r"^/api/periods/(?P<pid>[^/]+)/submit$", self.submit),
            ("POST", r"^/api/periods/(?P<pid>[^/]+)/confirm$", self.confirm),
            ("POST", r"^/api/periods/(?P<pid>[^/]+)/post$", self.post),
            ("POST", r"^/api/periods/(?P<pid>[^/]+)/seal$", self.seal),
            ("POST", r"^/api/periods/(?P<pid>[^/]+)/recompute$", self.recompute_period),
            ("GET", r"^/api/periods/(?P<pid>[^/]+)/adjustments$", self.list_adjustments),
            ("POST", r"^/api/periods/(?P<pid>[^/]+)/adjustments$", self.create_adjustment),
            ("POST", r"^/api/adjustments/(?P<aid>[^/]+)/apply$", self.apply_adjustment),
            ("POST", r"^/api/adjustments/(?P<aid>[^/]+)/reject$", self.reject_adjustment),
            ("POST", r"^/api/adjustments/(?P<aid>[^/]+)/recompute$", self.recompute_adjustment),
            ("GET", r"^/api/adjustments/(?P<aid>[^/]+)$", self.get_adjustment),
            ("GET", r"^/api/entries/(?P<eid>[^/]+)$", self.entry_detail),
        ]
        for m, pattern, handler in rules:
            if m != method:
                continue
            match = re.match(pattern, path)
            if match:
                return handler(body, query, role, **match.groupdict())
        raise NotFoundError(f"接口不存在：{method} {path}", code="route_not_found", status=404)

    # ------------------------------------------------------------ 工具

    @staticmethod
    def require_role(role: str | None, allowed: tuple[str, ...]) -> str:
        if role is None:
            raise PermissionError("缺少 X-Actor-Role 请求头")
        if role not in allowed:
            raise PermissionError(f"当前角色「{role}」无权执行该操作，允许：{'、'.join(allowed)}")
        return role

    @staticmethod
    def require_fields(body: dict, fields: tuple[str, ...]) -> None:
        missing = [f for f in fields if body.get(f) is None]
        if missing:
            raise AppError("缺少必填字段：" + "、".join(missing), code="missing_fields", status=422)

    # ------------------------------------------------------------ handlers

    def health(self, body, query, role):
        from . import ENGINE_VERSION
        return {"status": "ok", "engine_version": ENGINE_VERSION}

    def create_enterprise(self, body, query, role):
        self.require_role(role, (ROLE_ACCOUNTANT,))
        self.require_fields(body, ("name",))
        return self.store.create_enterprise(body["name"])

    def list_enterprises(self, body, query, role):
        self.require_role(role, (ROLE_ACCOUNTANT, ROLE_AUDITOR, ROLE_OPERATOR))
        return {"enterprises": self.store.list_enterprises()}

    def open_period(self, body, query, role, eid):
        self.require_role(role, (ROLE_ACCOUNTANT,))
        self.require_fields(body, ("year",))
        return self.store.open_period(eid, int(body["year"]))

    def find_period(self, body, query, role, eid, year):
        self.require_role(role, (ROLE_ENTERPRISE, ROLE_ACCOUNTANT, ROLE_AUDITOR, ROLE_OPERATOR))
        return self.store.find_period(eid, int(year))

    def get_period(self, body, query, role, pid):
        self.require_role(role, (ROLE_ENTERPRISE, ROLE_ACCOUNTANT, ROLE_AUDITOR, ROLE_OPERATOR))
        return self.store.get_period(pid)

    def submit_model_version(self, body, query, role, eid, year):
        self.require_role(role, (ROLE_ENTERPRISE,))
        self.require_fields(body, ("model_code", "version_type", "content", "evidence"))
        return self.store.submit_model_version(
            enterprise_id=eid, year=int(year),
            model_code=body["model_code"], version_type=body["version_type"],
            content=body["content"], evidence=body["evidence"],
            actor_role=role, batch_no=body.get("batch_no"),
        )

    def publish_policy(self, body, query, role):
        self.require_role(role, (ROLE_ACCOUNTANT,))
        self.require_fields(body, ("year", "content", "evidence"))
        return self.store.publish_policy_version(
            year=int(body["year"]), content=body["content"],
            evidence=body["evidence"], actor_role=role,
        )

    def publish_rule(self, body, query, role):
        self.require_role(role, (ROLE_ACCOUNTANT,))
        self.require_fields(body, ("content", "evidence"))
        return self.store.publish_rule_version(
            content=body["content"], evidence=body["evidence"],
            actor_role=role,
            year=int(body["year"]) if body.get("year") is not None else None,
        )

    def list_versions(self, body, query, role):
        self.require_role(role, (ROLE_ENTERPRISE, ROLE_ACCOUNTANT, ROLE_AUDITOR))
        kwargs = {}
        if "enterprise_id" in query:
            kwargs["enterprise_id"] = query["enterprise_id"][0]
        if "model_code" in query:
            kwargs["model_code"] = query["model_code"][0]
        if "version_type" in query:
            kwargs["version_type"] = query["version_type"][0]
        if "year" in query:
            kwargs["year"] = int(query["year"][0])
        return {"versions": self.store.list_versions(**kwargs)}

    def get_version(self, body, query, role, vid):
        self.require_role(role, (ROLE_ENTERPRISE, ROLE_ACCOUNTANT, ROLE_AUDITOR))
        return self.store.get_version(vid)

    def trial(self, body, query, role, pid):
        self.require_role(role, (ROLE_ENTERPRISE, ROLE_ACCOUNTANT))
        return self.store.trial(pid)

    def list_trials(self, body, query, role, pid):
        self.require_role(role, (ROLE_ENTERPRISE, ROLE_ACCOUNTANT, ROLE_AUDITOR))
        return {"trials": self.store.list_trials(pid)}

    def submit(self, body, query, role, pid):
        self.require_role(role, (ROLE_ENTERPRISE,))
        return self.store.submit(pid)

    def confirm(self, body, query, role, pid):
        self.require_role(role, (ROLE_ENTERPRISE,))
        return self.store.confirm(pid, actor_role=role)

    def post(self, body, query, role, pid):
        self.require_role(role, (ROLE_OPERATOR,))
        return self.store.post(pid)

    def seal(self, body, query, role, pid):
        self.require_role(role, (ROLE_OPERATOR, ROLE_ACCOUNTANT))
        return self.store.seal(pid)

    def create_adjustment(self, body, query, role, pid):
        self.require_fields(body, ("adjustment_type", "payload", "reason"))
        atype = body["adjustment_type"]
        if atype == ADJ_RULE_ERRATA:
            self.require_role(role, (ROLE_ACCOUNTANT,))
        elif atype in (ADJ_LATE_DATA, ADJ_WITHDRAWAL):
            self.require_role(role, (ROLE_ENTERPRISE,))
        return self.store.create_adjustment(
            period_id=pid, adjustment_type=atype, payload=body["payload"],
            reason=body["reason"], evidence=body.get("evidence"), actor_role=role,
        )

    def list_adjustments(self, body, query, role, pid):
        self.require_role(role, (ROLE_ENTERPRISE, ROLE_ACCOUNTANT, ROLE_AUDITOR, ROLE_OPERATOR))
        return {"adjustments": self.store.list_adjustments(pid)}

    def apply_adjustment(self, body, query, role, aid):
        # 企业申请（迟到数据/撤销）须由核算专员审核应用；规则勘误创建即生效
        self.require_role(role, (ROLE_ACCOUNTANT,))
        return self.store.apply_adjustment(aid, actor_role=role)

    def reject_adjustment(self, body, query, role, aid):
        self.require_role(role, (ROLE_ACCOUNTANT,))
        return self.store.reject_adjustment(aid, body.get("note"))

    def get_adjustment(self, body, query, role, aid):
        self.require_role(role, (ROLE_ENTERPRISE, ROLE_ACCOUNTANT, ROLE_AUDITOR, ROLE_OPERATOR))
        return self.store.get_adjustment(aid)

    def statement(self, body, query, role, eid, year):
        self.require_role(role, (ROLE_AUDITOR,))
        return self.store.statement(eid, int(year))

    def model_trace(self, body, query, role, eid, year, code):
        self.require_role(role, (ROLE_AUDITOR,))
        return self.store.model_trace(eid, int(year), code)

    def entry_detail(self, body, query, role, eid):
        self.require_role(role, (ROLE_AUDITOR, ROLE_ACCOUNTANT, ROLE_OPERATOR, ROLE_ENTERPRISE))
        return self.store.entry_detail(eid)

    def recompute_period(self, body, query, role, pid):
        self.require_role(role, (ROLE_AUDITOR, ROLE_ACCOUNTANT))
        return self.store.recompute_period(pid)

    def recompute_adjustment(self, body, query, role, aid):
        self.require_role(role, (ROLE_AUDITOR, ROLE_ACCOUNTANT))
        return self.store.recompute_adjustment(aid)


# ---------------------------------------------------------------- HTTP 包装

class _Handler(BaseHTTPRequestHandler):
    server_version = "CreditAccounting/0.1"
    api: Api = None  # 由 make_server 注入

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str) -> None:
        parts = urlsplit(self.path)
        try:
            role_code = self.headers.get("X-Actor-Role")
            role = ROLE_CODES.get(role_code) if role_code else None
            if role_code and role is None:
                raise PermissionError(f"未知角色代码：{role_code}")
            length = int(self.headers.get("Content-Length") or 0)
            body: dict = {}
            if length:
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise AppError("请求体不是合法 JSON", code="invalid_json", status=400)
                if not isinstance(body, dict):
                    raise AppError("请求体必须是 JSON 对象", code="invalid_body", status=400)
            query = parse_qs(parts.query, keep_blank_values=True)
            # 进程内串行化对同一 SQLite 连接的访问；跨进程仍由 BEGIN IMMEDIATE 保证
            with self.api.store._lock:
                result = self.api.dispatch(method, parts.path, query, body, role)
            self._send(200, result if isinstance(result, dict) else {"data": result})
        except AppError as exc:
            self._send(exc.status, exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - 兜底，避免连接挂死
            self._send(500, {"error": "internal_error", "message": str(exc)})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def log_message(self, fmt: str, *args) -> None:
        if self.server is not None and getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)


def make_server(host: str, port: int, store: Store, *, quiet: bool = False) -> ThreadingHTTPServer:
    api = Api(store)
    handler = type("_BoundHandler", (_Handler,), {"api": api})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.quiet = quiet
    httpd.api = api
    return httpd
