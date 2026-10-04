"""HTTP API 端到端集成测试（标准库 urllib，零依赖）。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification_verifier.api import create_server
from qualification_verifier.seed import seed_demo
from qualification_verifier.service import QualificationService
from qualification_verifier.storage import connect, init_db


class ApiIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "api.db")
        conn = connect(self.db_path)
        init_db(conn)
        seed_demo(QualificationService(conn))
        conn.close()
        self.server = create_server(self.db_path, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _req(self, method: str, path: str, body: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_health(self) -> None:
        status, body = self._req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_scope_deny_case_explains_rule_and_evidence(self) -> None:
        status, body = self._req("GET", "/api/cases/CASE-SCOPE-DENY")
        self.assertEqual(status, 200)
        ev = body["data"]["latest_evaluation"]
        self.assertEqual(ev["overall_verdict"], "拒绝")
        p002 = next(f for f in ev["findings"] if f["rule"] == "P-002")
        self.assertEqual(p002["verdict"], "拒绝")
        self.assertTrue(p002["reason"])
        self.assertTrue(p002["evidence"])

    def test_approve_flow_and_duplicate_conflict(self) -> None:
        status, body = self._req("POST", "/api/cases/CASE-OK/decisions", {
            "result": "批准", "decided_by": "监管员甲",
            "reason": "全部通过", "idempotency_key": "api-key-1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["result"], "批准")
        self.assertIsNotNone(body["data"]["grant"])

        # 同幂等键回放
        status2, body2 = self._req("POST", "/api/cases/CASE-OK/decisions", {
            "result": "批准", "decided_by": "监管员甲",
            "reason": "重复点", "idempotency_key": "api-key-1"})
        self.assertEqual(status2, 200)
        self.assertTrue(body2["data"].get("idempotent_replay"))

        # 新幂等键重复批准 -> 409 duplicate_grant
        status3, body3 = self._req("POST", "/api/cases/CASE-OK/decisions", {
            "result": "批准", "decided_by": "监管员甲",
            "reason": "再次批准", "idempotency_key": "api-key-2"})
        self.assertEqual(status3, 409)
        self.assertEqual(body3["error"], "duplicate_grant")

    def test_approve_denied_case_is_409(self) -> None:
        status, body = self._req("POST", "/api/cases/CASE-LEVEL-DENY/decisions", {
            "result": "批准", "decided_by": "监管员甲",
            "reason": "强行批准", "idempotency_key": "bad-1"})
        self.assertEqual(status, 409)
        self.assertIn("拒绝", body["message"])

    def test_scopes(self) -> None:
        s1, b1 = self._req("GET", "/api/institutions/INST-RK/scope?at=2026-06-01T00:00:00Z")
        self.assertEqual(s1, 200)
        self.assertIn("SURG-GS", b1["data"]["allowed_projects"])
        s2, b2 = self._req("GET", "/api/persons/PER-ZHAO/scope?at=2026-06-01T00:00:00Z")
        self.assertEqual(s2, 200)
        # 赵医生在瑞康（备案）与外院（主机构）各有一条分组
        insts = {r["institution_id"] for r in b2["data"]["registrations"]}
        self.assertEqual(insts, {"INST-RK", "OTHER-HOSP"})

    def test_supplement_after_approval_keeps_grant(self) -> None:
        self._req("POST", "/api/cases/CASE-OK/decisions", {
            "result": "批准", "decided_by": "监管员甲",
            "reason": "通过", "idempotency_key": "supp-1"})
        status, body = self._req("POST", "/api/cases/CASE-OK/supplement", {
            "filename": "补充.pdf", "payload": {"x": 1}})
        self.assertEqual(status, 200)
        self.assertIn("原决定", body["data"]["notice"])
        _, case_body = self._req("GET", "/api/cases/CASE-OK")
        self.assertEqual(len(case_body["data"]["decisions"]), 1)
        self.assertEqual(case_body["data"]["grants"][0]["status"], "有效")


if __name__ == "__main__":
    unittest.main()
