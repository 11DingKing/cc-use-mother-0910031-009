"""HTTP API 端到端测试（真实端口，零第三方依赖）。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification.api import make_server


def body(materials=None):
    if materials is None:
        materials = [
            {"material_type": "机构资质凭证", "file_ref": "a"},
            {"material_type": "执业证凭证", "file_ref": "b"},
            {"material_type": "主诊资格凭证", "file_ref": "c"},
            {"material_type": "注册或备案凭证", "file_ref": "d"},
        ]
    return {
        "institutions": [
            {"id": "H", "name": "华康医院"},
            {"id": "M", "name": "民安医院"},
        ],
        "personnel": [{"id": "P", "name": "张医生"}],
        "categories": [
            {"code": "S", "name": "眼科", "max_grade": "四级"},
        ],
        "inst_quals": [
            {"institution_id": "H",
             "scope": [{"category_code": "S", "grade": "三级"}],
             "valid_from": "2025-01-01", "valid_to": "2026-12-31", "at": "2026-01-01"},
        ],
        "certs": [
            {"personnel_id": "P", "license_no": "LIC-1",
             "scope": [{"category_code": "S", "grade": "四级"}],
             "valid_from": "2025-01-01", "valid_to": "2026-12-31", "at": "2026-01-01"},
        ],
        "attending": [
            {"personnel_id": "P", "category_code": "S", "max_grade": "三级",
             "valid_from": "2025-01-01", "valid_to": "2026-12-31", "at": "2026-01-01"},
        ],
        "registrations": [
            {"personnel_id": "P", "institution_id": "H",
             "valid_from": "2025-01-01", "at": "2026-01-01"},
        ],
        "materials": materials or [
            {"material_type": "机构资质凭证", "file_ref": "a"},
            {"material_type": "执业证凭证", "file_ref": "b"},
            {"material_type": "主诊资格凭证", "file_ref": "c"},
            {"material_type": "注册或备案凭证", "file_ref": "d"},
        ],
    }


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.server = make_server("127.0.0.1", 0, self.tmp.name)
        self.port = self.server.server_address[1]
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def call(self, method: str, path: str, payload=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def seed(self, materials=None) -> None:
        data = body(materials)
        for item in data["institutions"]:
            self.call("POST", "/api/institutions", item)
        for item in data["personnel"]:
            self.call("POST", "/api/personnel", item)
        for item in data["categories"]:
            self.call("POST", "/api/categories", item)
        for item in data["inst_quals"]:
            self.call("POST", "/api/institutions/H/qualifications", item)
        self.call("POST", "/api/personnel/P/certs", data["certs"][0])
        self.call("POST", "/api/personnel/P/attending", data["attending"][0])
        self.call("POST", "/api/personnel/P/registrations", data["registrations"][0])
        self._materials = data["materials"]

    def test_full_flow_allow_and_duplicate_409(self) -> None:
        self.seed()
        status, created = self.call("POST", "/api/cases", {
            "institution_id": "H", "personnel_id": "P", "category_code": "S",
            "grade": "三级", "materials": self._materials, "case_id": "CASE-1",
            "at": "2026-03-01",
        })
        self.assertEqual(status, 200)
        self.assertEqual(created["evaluation"]["decision"], "允许")

        status, grants = self.call("POST", "/api/cases/CASE-1/approve",
                                   {"at": "2026-03-01"})
        self.assertEqual(status, 200)
        self.assertEqual(len(grants["grants"]), 2)

        status, err = self.call("POST", "/api/cases/CASE-1/approve",
                                {"at": "2026-03-02"})
        self.assertEqual(status, 409)
        self.assertEqual(err["rule"], "防重复授权")

        status, detail = self.call("GET", "/api/cases/CASE-1/evaluation",
                                   )
        self.assertEqual(status, 200)
        self.assertTrue(all("rule_code" in r for r in detail["rules"]))

    def test_scope_mismatch_deny_explained(self) -> None:
        self.seed()
        # 机构 M 无任何资质，P 也未注册到 M
        self.call("POST", "/api/institutions", {"id": "M2", "name": "外院"})
        status, created = self.call("POST", "/api/cases", {
            "institution_id": "M2", "personnel_id": "P", "category_code": "S",
            "grade": "一级", "materials": self._materials, "case_id": "CASE-2",
            "at": "2026-03-01",
        })
        self.assertEqual(status, 200)
        self.assertEqual(created["evaluation"]["decision"], "拒绝")
        failed = {r["rule_code"] for r in created["evaluation"]["rules"] if not r["passed"]}
        self.assertIn("R04", failed)  # 机构无资质版本
        self.assertIn("R12", failed)  # 注册机构不符

        status, err = self.call("POST", "/api/cases/CASE-2/approve",
                                {"at": "2026-03-01"})
        self.assertEqual(status, 422)
        self.assertIn("核验不通过", err["error"])

    def test_supplement_then_approve(self) -> None:
        self.seed(materials=[])
        status, created = self.call("POST", "/api/cases", {
            "institution_id": "H", "personnel_id": "P", "category_code": "S",
            "grade": "二级", "materials": [], "case_id": "CASE-3",
            "at": "2026-03-01",
        })
        self.assertEqual(created["evaluation"]["decision"], "补交材料")
        status, out = self.call("POST", "/api/cases/CASE-3/supplement", {
            "materials": [
                {"material_type": "机构资质凭证", "file_ref": "a"},
                {"material_type": "执业证凭证", "file_ref": "b"},
                {"material_type": "主诊资格凭证", "file_ref": "c"},
                {"material_type": "注册或备案凭证", "file_ref": "d"},
            ],
            "at": "2026-03-03",
        })
        self.assertEqual(status, 200)
        self.assertEqual(out["evaluation"]["decision"], "允许")
        status, _ = self.call("POST", "/api/cases/CASE-3/approve",
                              {"at": "2026-03-03"})
        self.assertEqual(status, 200)

        status, timeline = self.call("GET", "/api/cases/CASE-3/timeline")
        self.assertEqual(status, 200)
        actions = [t.get("action") for t in timeline["timeline"] if t["kind"] == "event"]
        self.assertIn("材料补交", actions)

    def test_health_and_index(self) -> None:
        status, out = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(out["status"], "ok")
        status, out = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(out["endpoints"]), 20)


if __name__ == "__main__":
    unittest.main()
