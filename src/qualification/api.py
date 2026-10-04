"""HTTP API（标准库实现，零第三方依赖）。

每个案件结论都返回逐条规则的判定文字与证据引用；批准为单事务原子写入。
启动：``python -m qualification.api --db data/qual.db --port 8080``
"""
from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .models import (
    DuplicateGrantError,
    NotFoundError,
    QualificationError,
)
from .service import QualificationService
from .store import connect as connect_db

ROUTES: list[tuple[str, str, str]] = [
    # （方法, 路径模式, 处理器名）
    ("POST", "/api/institutions", "create_institution"),
    ("POST", "/api/personnel", "create_personnel"),
    ("POST", "/api/categories", "create_category"),

    ("POST", "/api/institutions/<id>/qualifications", "add_inst_qualification"),
    ("POST", "/api/institutions/<id>/qualification-status", "set_inst_qual_status"),
    ("GET", "/api/institutions/<id>/scope", "inst_scope"),

    ("POST", "/api/personnel/<id>/certs", "add_cert"),
    ("POST", "/api/personnel/<id>/cert-status", "set_cert_status"),
    ("POST", "/api/personnel/<id>/attending", "add_attending"),
    ("POST", "/api/personnel/<id>/attending-status", "set_attending_status"),
    ("POST", "/api/personnel/<id>/registrations", "add_registration"),
    ("GET", "/api/personnel/<id>/scope", "personnel_scope"),

    ("POST", "/api/registrations/<rid>/close", "close_registration"),
    ("POST", "/api/cross-filings", "create_filing"),
    ("POST", "/api/cross-filings/<rid>/close", "close_filing"),

    ("POST", "/api/cases", "open_case"),
    ("GET", "/api/cases/<id>", "get_case"),
    ("GET", "/api/cases/<id>/evaluation", "case_evaluation"),
    ("GET", "/api/cases/<id>/evaluations", "case_evaluations"),
    ("GET", "/api/cases/<id>/timeline", "case_timeline"),
    ("GET", "/api/cases/<id>/grants", "case_grants"),
    ("GET", "/api/grants", "list_grants"),
    ("POST", "/api/cases/<id>/supplement", "supplement"),
    ("POST", "/api/cases/<id>/approve", "approve"),
    ("POST", "/api/cases/<id>/reject", "reject"),
    ("POST", "/api/cases/<id>/request-materials", "request_materials"),
    ("POST", "/api/cases/<id>/archive", "archive"),

    ("GET", "/api/evidence/<eid>", "get_evidence"),
    ("GET", "/health", "health"),
    ("GET", "/", "index"),
]


def _match(pattern: str, path: str) -> dict[str, str] | None:
    parts_p, parts_v = pattern.strip("/").split("/"), path.strip("/").split("/")
    if len(parts_p) != len(parts_v):
        return None
    params: dict[str, str] = {}
    for token, value in zip(parts_p, parts_v):
        if token.startswith("<") and token.endswith(">"):
            params[token[1:-1]] = value
        elif token != value:
            return None
    return params


class ApiApp:
    """持有数据库路径；每个请求使用独立短连接（WAL 下并发安全）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        conn = connect_db(db_path)  # 启动时初始化 schema
        conn.close()

    def handle(self, method: str, path: str, query: dict[str, list[str]],
               body: bytes) -> tuple[int, dict[str, Any]]:
        params: dict[str, str] = {}
        handler_name = None
        for route_method, pattern, name in ROUTES:
            if route_method == method:
                matched = _match(pattern, path)
                if matched is not None:
                    params, handler_name = matched, name
                    break
        if handler_name is None:
            return 404, {"error": "未找到接口", "path": path}

        payload: dict[str, Any] = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                return 400, {"error": "请求体不是合法 JSON"}
            if not isinstance(payload, dict):
                return 400, {"error": "请求体必须是 JSON 对象"}

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            service = QualificationService(conn)
            # 各写方法在服务层自行开启事务；此处只负责连接生命周期与异常映射
            result = getattr(self, handler_name)(service, params, query, payload)
            return 200, result
        except NotFoundError as exc:
            return 404, {"error": str(exc)}
        except DuplicateGrantError as exc:
            return 409, {"error": str(exc), "rule": "防重复授权"}
        except QualificationError as exc:
            return 422, {"error": str(exc)}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 登记类接口
    # ------------------------------------------------------------------
    def create_institution(self, svc, p, q, body) -> dict:
        return svc.register_institution(body["id"], body["name"], at=body.get("at"))

    def create_personnel(self, svc, p, q, body) -> dict:
        return svc.register_personnel(body["id"], body["name"], at=body.get("at"))

    def create_category(self, svc, p, q, body) -> dict:
        return svc.register_category(body["code"], body["name"], body["max_grade"])

    def add_inst_qualification(self, svc, p, q, body) -> dict:
        version = svc.register_institution_qualification(
            p["id"], body["scope"], body["valid_from"], body["valid_to"],
            note=body.get("note", ""), at=body.get("at"),
        )
        return {"institution_id": p["id"], "version_no": version, "hint": "续期请再次调用，生成新版本"}

    def set_inst_qual_status(self, svc, p, q, body) -> dict:
        version = svc.set_institution_qualification_status(
            p["id"], body["status"], at=body.get("at"))
        return {"institution_id": p["id"], "version_no": version, "status": body["status"]}

    def inst_scope(self, svc, p, q, body) -> dict:
        return svc.institution_scope(p["id"], as_of=q.get("as_of", [None])[0])

    def add_cert(self, svc, p, q, body) -> dict:
        version = svc.register_cert(
            p["id"], body["license_no"], body["scope"],
            body["valid_from"], body["valid_to"],
            note=body.get("note", ""), at=body.get("at"),
        )
        return {"personnel_id": p["id"], "version_no": version, "hint": "续期请再次调用，生成新版本"}

    def set_cert_status(self, svc, p, q, body) -> dict:
        version = svc.set_cert_status(p["id"], body["status"], at=body.get("at"))
        return {"personnel_id": p["id"], "version_no": version, "status": body["status"]}

    def add_attending(self, svc, p, q, body) -> dict:
        version = svc.register_attending_qualification(
            p["id"], body["category_code"], body["max_grade"],
            body["valid_from"], body["valid_to"],
            note=body.get("note", ""), at=body.get("at"),
        )
        return {"personnel_id": p["id"], "category_code": body["category_code"],
                "version_no": version}

    def set_attending_status(self, svc, p, q, body) -> dict:
        version = svc.set_attending_status(
            p["id"], body["category_code"], body["status"], at=body.get("at"))
        return {"personnel_id": p["id"], "category_code": body["category_code"],
                "version_no": version, "status": body["status"]}

    def add_registration(self, svc, p, q, body) -> dict:
        rid = svc.register_practice_site(
            p["id"], body["institution_id"], body["valid_from"],
            valid_to=body.get("valid_to"), at=body.get("at"))
        return {"registration_id": rid}

    def close_registration(self, svc, p, q, body) -> dict:
        svc.close_registration(int(p["rid"]), body["valid_to"])
        return {"registration_id": int(p["rid"]), "closed_at": body["valid_to"]}

    def create_filing(self, svc, p, q, body) -> dict:
        fid = svc.file_cross_institution(
            body["personnel_id"], body["home_institution_id"], body["host_institution_id"],
            body["category_code"], body["max_grade"], body["valid_from"],
            valid_to=body.get("valid_to"), at=body.get("at"),
        )
        return {"cross_filing_id": fid}

    def close_filing(self, svc, p, q, body) -> dict:
        svc.close_cross_filing(int(p["rid"]), body["valid_to"])
        return {"cross_filing_id": int(p["rid"]), "closed_at": body["valid_to"]}

    def personnel_scope(self, svc, p, q, body) -> dict:
        institution_id = q.get("institution_id", [None])[0]
        if not institution_id:
            raise QualificationError("查询参数 institution_id 必填")
        return svc.personnel_scope(p["id"], institution_id, as_of=q.get("as_of", [None])[0])

    # ------------------------------------------------------------------
    # 案件接口
    # ------------------------------------------------------------------
    def open_case(self, svc, p, q, body) -> dict:
        case_id = svc.open_case(
            body["institution_id"], body["personnel_id"],
            body["category_code"], body["grade"],
            body.get("materials", []),
            case_id=body.get("case_id"), at=body.get("at"),
            actor=body.get("actor", "机构合规员"),
        )
        return {"case_id": case_id, "evaluation": svc.evaluate(case_id, as_of=body.get("at"))}

    def get_case(self, svc, p, q, body) -> dict:
        row = svc.conn.execute("SELECT * FROM cases WHERE id = ?", (p["id"],)).fetchone()
        if row is None:
            raise NotFoundError(f"案件 {p['id']} 不存在")
        data = dict(row)
        latest = svc.conn.execute(
            "SELECT decision, summary, as_of FROM case_evaluations WHERE case_id = ? "
            "ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
        data["latest_evaluation"] = None if latest is None else dict(latest)
        return data

    def case_evaluation(self, svc, p, q, body) -> dict:
        return svc.evaluate(p["id"], as_of=q.get("as_of", [None])[0])

    def case_evaluations(self, svc, p, q, body) -> dict:
        svc._require_case(p["id"])
        rows = svc.conn.execute(
            "SELECT id, as_of, decision, summary, rules_json, snapshot_json, created_at "
            "FROM case_evaluations WHERE case_id = ? ORDER BY id", (p["id"],)).fetchall()
        return {"case_id": p["id"], "evaluations": [
            {**{k: row[k] for k in ("id", "as_of", "decision", "summary", "created_at")},
             "rules": json.loads(row["rules_json"]),
             "snapshot": json.loads(row["snapshot_json"])}
            for row in rows]}

    def case_timeline(self, svc, p, q, body) -> dict:
        return {"case_id": p["id"], "timeline": svc.case_timeline(p["id"])}

    def case_grants(self, svc, p, q, body) -> dict:
        return {"case_id": p["id"], "grants": svc.list_grants(p["id"])}

    def list_grants(self, svc, p, q, body) -> dict:
        """授权台账查询：可按机构/人员/状态过滤，默认只返回有效授权。"""
        sql = "SELECT * FROM grants WHERE 1=1"
        params: list[str] = []
        if q.get("institution_id"):
            sql += " AND institution_id = ?"
            params.append(q["institution_id"][0])
        if q.get("personnel_id"):
            sql += " AND personnel_id = ?"
            params.append(q["personnel_id"][0])
        status = q.get("status", ["有效"])[0]
        if status != "全部":
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id"
        return {"grants": [dict(row) for row in svc.conn.execute(sql, params).fetchall()]}

    def supplement(self, svc, p, q, body) -> dict:
        return svc.supplement_material(
            p["id"], body["materials"], at=body.get("at"),
            actor=body.get("actor", "机构合规员"))

    def approve(self, svc, p, q, body) -> dict:
        return svc.approve(p["id"], as_of=body.get("at"), actor=body.get("actor", "监管人员"))

    def reject(self, svc, p, q, body) -> dict:
        return svc.reject(p["id"], reason=body.get("reason", ""),
                          as_of=body.get("at"), actor=body.get("actor", "监管人员"))

    def request_materials(self, svc, p, q, body) -> dict:
        return svc.request_materials(p["id"], as_of=body.get("at"),
                                     actor=body.get("actor", "监管人员"))

    def archive(self, svc, p, q, body) -> dict:
        return svc.archive_case(p["id"], at=body.get("at"),
                                actor=body.get("actor", "监管人员"))

    def get_evidence(self, svc, p, q, body) -> dict:
        row = svc.conn.execute("SELECT * FROM evidence WHERE id = ?", (p["eid"],)).fetchone()
        if row is None:
            raise NotFoundError(f"证据 {p['eid']} 不存在")
        return {"id": row["id"], "type": row["evidence_type"], "title": row["title"],
                "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]}

    def health(self, svc, p, q, body) -> dict:
        return {"status": "ok"}

    def index(self, svc, p, q, body) -> dict:
        return {"service": "执业资质范围核验", "endpoints": [
            {"method": method, "path": path} for method, path, _ in ROUTES]}


class _Handler(BaseHTTPRequestHandler):
    app: ApiApp = None  # 由 make_server 注入

    def _send(self, status: int, data: dict[str, Any]) -> None:
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            status, data = self.app.handle(method, parsed.path, parse_qs(parsed.query), body)
        except Exception as exc:  # pragma: no cover - 兜底
            status, data = 500, {"error": f"服务器内部错误：{exc}"}
        self._send(status, data)

    def log_message(self, fmt, *args) -> None:  # 安静日志
        return


def make_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    app = ApiApp(db_path)

    class BoundHandler(_Handler):
        pass

    BoundHandler.app = app
    return ThreadingHTTPServer((host, port), BoundHandler)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="执业资质范围核验后端")
    parser.add_argument("--db", default="data/qual.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = make_server(args.host, args.port, args.db)
    print(f"执业资质范围核验后端已启动：http://{args.host}:{args.port}  数据库：{args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
