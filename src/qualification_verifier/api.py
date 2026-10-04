"""JSON HTTP API（标准库实现，零第三方依赖）。

所有核验结论在响应体中给出：

* ``overall_verdict`` / 机构与人员分别的 verdict；
* ``findings[]``：每条规则的代码、标题、所属主体、允许/拒绝、人话理由与证据摘录；
* ``evaluation`` / ``evaluation_history``：评估快照编号，可逐条回放历史决定依据。
"""
from __future__ import annotations

import json
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .service import (
    ConflictError,
    DuplicateGrantError,
    NotFoundError,
    QualificationService,
)
from .storage import connect, init_db


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: Any) -> None:
    data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


# (方法, 正则, 处理函数名)
Route = tuple[str, re.Pattern[str], str]


def make_routes() -> list[Route]:
    inst = r"/api/institutions/(?P<inst_id>[^/]+)"
    person = r"/api/persons/(?P<person_id>[^/]+)"
    case = r"/api/cases/(?P<case_id>[^/]+)"
    return [
        ("GET", re.compile(r"^/health$"), "health"),
        ("POST", re.compile(r"^/api/institutions$"), "create_institution"),
        ("POST", re.compile(r"^/api/persons$"), "create_person"),
        ("POST", re.compile(r"^/api/categories$"), "register_category"),
        ("POST", re.compile(rf"^{inst}/sites$"), "register_site"),
        ("POST", re.compile(rf"^{inst}/licenses$"), "register_license"),
        ("GET", re.compile(rf"^{inst}/scope$"), "institution_scope"),
        ("POST", re.compile(rf"^{person}/certificates$"), "register_certificate"),
        ("POST", re.compile(rf"^{person}/certificates/suspend$"), "suspend_certificate"),
        ("POST", re.compile(rf"^{person}/certificates/resume$"), "resume_certificate"),
        ("POST", re.compile(rf"^{person}/attending-qualifications$"), "register_attending"),
        ("POST", re.compile(rf"^{person}/registrations/primary$"), "register_primary"),
        ("POST", re.compile(rf"^{person}/registrations/cross-institution$"), "register_filing"),
        ("GET", re.compile(rf"^{person}/scope$"), "person_scope"),
        ("POST", re.compile(r"^/api/cases$"), "create_case"),
        ("GET", re.compile(rf"^{case}$"), "get_case"),
        ("POST", re.compile(rf"^{case}/evidence$"), "add_evidence"),
        ("POST", re.compile(rf"^{case}/supplement$"), "supplement_evidence"),
        ("POST", re.compile(rf"^{case}/evaluations$"), "add_evaluation"),
        ("GET", re.compile(rf"^{case}/evaluations/(?P<evaluation_id>\d+)$"), "get_evaluation"),
        ("POST", re.compile(rf"^{case}/decisions$"), "decide"),
        ("POST", re.compile(rf"^{case}/revoke-grant$"), "revoke_grant"),
        ("POST", re.compile(rf"^{case}/archive$"), "archive_case"),
    ]


class ApiHandler(BaseHTTPRequestHandler):
    service: QualificationService  # 由 factory 注入到类上
    routes: list[Route]

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        return

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ConflictError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(body, dict):
            raise ConflictError("请求体必须是 JSON 对象")
        return body

    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        for verb, pattern, action in self.routes:
            if verb != method:
                continue
            match = pattern.match(path)
            if match:
                try:
                    body = self._read_body() if method == "POST" else {}
                    result = getattr(self, action)(match.groupdict(), body, query)
                    _json_response(self, 200, {"ok": True, "data": result})
                except NotFoundError as exc:
                    _json_response(self, 404, {"ok": False, "error": "not_found",
                                               "message": str(exc)})
                except DuplicateGrantError as exc:
                    _json_response(self, 409, {"ok": False, "error": "duplicate_grant",
                                               "message": str(exc)})
                except ConflictError as exc:
                    _json_response(self, 409, {"ok": False, "error": "conflict",
                                               "message": str(exc)})
                except (KeyError, TypeError, ValueError) as exc:
                    _json_response(self, 400, {"ok": False, "error": "bad_request",
                                               "message": str(exc)})
                except Exception as exc:  # pragma: no cover - 防御性
                    traceback.print_exc()
                    _json_response(self, 500, {"ok": False, "error": "internal",
                                               "message": str(exc)})
                return
        _json_response(self, 404, {"ok": False, "error": "not_found",
                                   "message": f"无此路由：{method} {path}"})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    # ---------- 端点 ----------

    def health(self, params: dict[str, str], body: dict[str, Any], query: dict[str, str]) -> Any:
        return {"status": "ok"}

    def create_institution(self, p, b, q) -> Any:
        return self.service.create_institution(b["institution_id"], b["name"])

    def create_person(self, p, b, q) -> Any:
        return self.service.create_person(b["person_id"], b["name"])

    def register_category(self, p, b, q) -> Any:
        return self.service.register_category(
            b["code"], b["name"], int(b["level"]),
            b.get("parent_code"), b.get("active", True))

    def register_site(self, p, b, q) -> Any:
        return self.service.register_site(
            p["inst_id"], b["address"], b["valid_from"], b.get("valid_to"),
            b.get("site_id"))

    def register_license(self, p, b, q) -> Any:
        return self.service.register_institution_license(
            p["inst_id"], b["license_no"], list(b["scope_categories"]),
            int(b["level"]), b["valid_from"], b["valid_to"], b.get("issued_at"))

    def institution_scope(self, p, b, q) -> Any:
        return self.service.institution_scope(p["inst_id"], q.get("at"))

    def register_certificate(self, p, b, q) -> Any:
        return self.service.register_certificate(
            p["person_id"], b["cert_no"], list(b["practice_scope"]),
            b["valid_from"], b["valid_to"], b.get("issued_at"))

    def suspend_certificate(self, p, b, q) -> Any:
        return self.service.suspend_certificate(
            p["person_id"], b["effective_from"], b.get("reason"))

    def resume_certificate(self, p, b, q) -> Any:
        return self.service.resume_certificate(
            p["person_id"], b["effective_from"], b.get("reason"))

    def register_attending(self, p, b, q) -> Any:
        return self.service.register_attending(
            p["person_id"], b["title"], list(b["scope_categories"]),
            int(b["level"]), b["valid_from"], b["valid_to"], b.get("issued_at"))

    def register_primary(self, p, b, q) -> Any:
        return self.service.register_primary_registration(
            p["person_id"], b["institution_id"], b["valid_from"], b.get("valid_to"))

    def register_filing(self, p, b, q) -> Any:
        return self.service.file_cross_institution(
            p["person_id"], b["institution_id"], b["valid_from"],
            b.get("valid_to"), b.get("registration_id"))

    def person_scope(self, p, b, q) -> Any:
        return self.service.person_scope(p["person_id"], q.get("at"))

    def create_case(self, p, b, q) -> Any:
        return self.service.create_case(
            b["case_id"], b["institution_id"], b["site_id"],
            b["person_id"], b["project_code"])

    def get_case(self, p, b, q) -> Any:
        return self.service.get_case(p["case_id"])

    def add_evidence(self, p, b, q) -> Any:
        return self.service.add_evidence(
            p["case_id"], b["filename"], b["payload"],
            b.get("kind", "申请材料"), b.get("submitted_at"), b.get("evidence_id"))

    def supplement_evidence(self, p, b, q) -> Any:
        return self.service.supplement_evidence(
            p["case_id"], b["filename"], b["payload"], b.get("submitted_at"))

    def add_evaluation(self, p, b, q) -> Any:
        return self.service.add_evaluation(
            p["case_id"], b.get("trigger", "人工复评"), b.get("at"))

    def get_evaluation(self, p, b, q) -> Any:
        return self.service.get_evaluation(p["case_id"], int(p["evaluation_id"]))

    def decide(self, p, b, q) -> Any:
        return self.service.decide(
            p["case_id"], b["result"], b["decided_by"], b["reason"],
            b["idempotency_key"], b.get("use_evaluation_id"))

    def revoke_grant(self, p, b, q) -> Any:
        return self.service.revoke_grant(p["case_id"], b["revoked_by"], b["reason"])

    def archive_case(self, p, b, q) -> Any:
        return self.service.archive_case(p["case_id"])


def create_server(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    conn = connect(db_path, check_same_thread=False)
    init_db(conn)
    handler = type("BoundApiHandler", (ApiHandler,), {
        "service": QualificationService(conn),
        "routes": make_routes(),
    })
    server = ThreadingHTTPServer((host, port), handler)
    return server


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    server = create_server(db_path, host, port)
    print(f"执业资质范围核验服务已启动：http://{host}:{port} （数据库 {db_path}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
