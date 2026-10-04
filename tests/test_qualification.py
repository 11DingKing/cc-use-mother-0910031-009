"""端到端业务测试：登记 → 范围计算 → 案件 → 批准/拒绝/补件。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification import (
    DuplicateGrantError,
    QualificationError,
    QualificationService,
    connect,
)

ALL_MATERIALS = [
    {"material_type": "机构资质凭证", "file_ref": "docs/inst.pdf"},
    {"material_type": "执业证凭证", "file_ref": "docs/cert.pdf"},
    {"material_type": "主诊资格凭证", "file_ref": "docs/attending.pdf"},
    {"material_type": "注册或备案凭证", "file_ref": "docs/registration.pdf"},
]


def build_world(at: str = "2026-01-01") -> tuple[QualificationService, str, str]:
    """标准世界：机构 H 与 M；医师 P 注册于 H。

    分类 S（眼科，目录上限四级）、D（口腔，目录上限三级）。
    机构 H：S 三级；人员 P：执业证 S 四级 + 主诊 S 三级。
    机构 M：D 三级。
    """
    svc = QualificationService(connect(":memory:"))
    svc.register_institution("H", "华康医院", at=at)
    svc.register_institution("M", "民安医院", at=at)
    svc.register_personnel("P", "张医生", at=at)
    svc.register_category("S", "眼科", "四级")
    svc.register_category("D", "口腔", "三级")

    svc.register_institution_qualification(
        "H", [{"category_code": "S", "grade": "三级"}], "2025-01-01", "2026-12-31", at=at)
    svc.register_institution_qualification(
        "M", [{"category_code": "D", "grade": "三级"}], "2025-01-01", "2026-12-31", at=at)
    svc.register_cert(
        "P", "LIC-001", [{"category_code": "S", "grade": "四级"}],
        "2025-01-01", "2026-12-31", at=at)
    svc.register_attending_qualification(
        "P", "S", "三级", "2025-01-01", "2026-12-31", at=at)
    svc.register_practice_site("P", "H", "2025-01-01", at=at)
    return svc, "H", "M"


class ScopeTest(unittest.TestCase):
    def test_institution_scope_grade_ladder_and_boundary(self) -> None:
        svc, h, _ = build_world()
        scope = svc.institution_scope("H", as_of="2026-06-01")
        items = {(i["category_code"], i["grade"]): i for i in scope["items"]}
        # 三级资质覆盖一/二/三级，不覆盖四级
        self.assertTrue(items[("S", "一级")]["allowed"])
        self.assertTrue(items[("S", "三级")]["allowed"])
        denied = items[("S", "四级")]
        self.assertFalse(denied["allowed"])
        self.assertIn("等级", denied["reasons"][0])
        # 未包含分类 D
        d_denied = items[("D", "一级")]
        self.assertFalse(d_denied["allowed"])
        self.assertTrue(any("未包含分类" in r for r in d_denied["reasons"]))

    def test_personnel_scope_requires_attending_for_each_grade(self) -> None:
        svc, h, _ = build_world()
        scope = svc.personnel_scope("P", "H", as_of="2026-06-01")
        items = {(i["category_code"], i["grade"]): i for i in scope["items"]}
        # 执业证四级，但主诊资格只有三级 → 四级不可开展
        self.assertTrue(items[("S", "三级")]["allowed"])
        denied = items[("S", "四级")]
        self.assertFalse(denied["allowed"])
        self.assertTrue(any("主诊资格" in r for r in denied["reasons"]))
        self.assertEqual(len(items[("S", "三级")]["evidence"]), 3)  # 证+主诊+注册

    def test_expired_qualification_blocks_scope(self) -> None:
        svc, h, _ = build_world()
        scope = svc.institution_scope("H", as_of="2027-06-01")
        self.assertFalse(any(i["allowed"] for i in scope["items"]))
        self.assertTrue(any("已过期" in r for i in scope["items"] for r in i["reasons"]))


class CaseEvaluationTest(unittest.TestCase):
    def test_cert_valid_but_scope_mismatch_is_denied_not_allowed(self) -> None:
        """核心防误判：证书有效，但机构范围不含申请分类 → 拒绝。"""
        svc, h, m = build_world()
        # P 在 M 申请眼科三级：P 的证书有效且含眼科，但 M 只有口腔资质，
        # 且 P 未注册/备案到 M
        case_id = svc.open_case("M", "P", "S", "三级", ALL_MATERIALS, at="2026-03-01")
        result = svc.evaluate(case_id, as_of="2026-03-01")
        self.assertEqual(result["decision"], "拒绝")
        rules = {r["rule_code"]: r for r in result["rules"]}
        self.assertTrue(rules["R09"]["passed"], "执业证本身有效")
        self.assertFalse(rules["R07"]["passed"], "机构资质范围不含眼科")
        self.assertFalse(rules["R12"]["passed"], "无注册或备案")
        self.assertFalse(rules["R13"]["passed"])
        with self.assertRaises(QualificationError):
            svc.approve(case_id, as_of="2026-03-01")

    def test_grade_exceeds_institution_but_cert_valid_is_denied(self) -> None:
        """证书有效、分类匹配，但机构等级不够（申请四级，机构三级）→ 拒绝。"""
        svc, h, _ = build_world()
        case_id = svc.open_case("H", "P", "S", "四级", ALL_MATERIALS, at="2026-03-01")
        result = svc.evaluate(case_id, as_of="2026-03-01")
        self.assertEqual(result["decision"], "拒绝")
        rules = {r["rule_code"]: r for r in result["rules"]}
        self.assertTrue(rules["R09"]["passed"])
        self.assertTrue(rules["R07"]["passed"])
        self.assertFalse(rules["R08"]["passed"], "机构三级不覆盖四级")
        self.assertFalse(rules["R11"]["passed"], "主诊资格三级也不覆盖四级")

    def test_missing_materials_means_supplement_not_deny(self) -> None:
        svc, h, _ = build_world()
        case_id = svc.open_case(
            "H", "P", "S", "三级", ALL_MATERIALS[:2], at="2026-03-01")
        result = svc.evaluate(case_id, as_of="2026-03-01")
        self.assertEqual(result["decision"], "补交材料")
        with self.assertRaises(QualificationError):
            svc.approve(case_id, as_of="2026-03-01")
        # 补交后允许
        svc.supplement_material(case_id, ALL_MATERIALS[2:], at="2026-03-05")
        result2 = svc.evaluate(case_id, as_of="2026-03-05")
        self.assertEqual(result2["decision"], "允许")

    def test_all_rules_pass_and_explain_allow(self) -> None:
        svc, h, _ = build_world()
        case_id = svc.open_case("H", "P", "S", "三级", ALL_MATERIALS, at="2026-03-01")
        result = svc.evaluate(case_id, as_of="2026-03-01")
        self.assertEqual(result["decision"], "允许")
        self.assertTrue(all(r["passed"] for r in result["rules"]))
        # 每条规则都有解释文字
        self.assertTrue(all(r["detail"] for r in result["rules"]))


class TemporalEventTest(unittest.TestCase):
    def test_cert_renewal_creates_new_version_and_keep_decision(self) -> None:
        """续期颁发新版本；在旧决定时点的历史结论保持不变。"""
        svc, h, _ = build_world()
        case_id = svc.open_case("H", "P", "S", "三级", ALL_MATERIALS, at="2026-03-01")
        svc.approve(case_id, as_of="2026-03-01")

        # 2027 年证书过期，此时核验应拒绝
        result_old_time = svc.evaluate(case_id, as_of="2027-03-01")
        self.assertEqual(result_old_time["decision"], "拒绝")
        rules = {r["rule_code"]: r for r in result_old_time["rules"]}
        self.assertFalse(rules["R09"]["passed"])

        # 续期：执业证颁发 v2（同时机构资质与主诊资格也续期）
        v2 = svc.register_cert(
            "P", "LIC-001", [{"category_code": "S", "grade": "四级"}],
            "2027-01-01", "2028-12-31", at="2027-01-01")
        self.assertEqual(v2, 2)
        svc.register_institution_qualification(
            "H", [{"category_code": "S", "grade": "三级"}], "2027-01-01", "2028-12-31",
            at="2027-01-01")
        svc.register_attending_qualification(
            "P", "S", "三级", "2027-01-01", "2028-12-31", at="2027-01-01")
        renewed = svc.evaluate(case_id, as_of="2027-03-01")
        self.assertEqual(renewed["decision"], "允许")

        # 案件原决定仍是 2026-03-01 的允许，且历史快照仍引用 v1
        case = svc.conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        self.assertEqual(case["decision"], "允许")
        self.assertEqual(case["decided_at"], "2026-03-01")
        timeline = svc.case_timeline(case_id)
        first_eval = next(t for t in timeline if t["kind"] == "evaluation")
        self.assertEqual(first_eval["as_of"], "2026-03-01")
        self.assertEqual(first_eval["decision"], "允许")
        # 决定后尝试重复批准 → 拒绝
        with self.assertRaises(DuplicateGrantError):
            svc.approve(case_id, as_of="2027-03-01")

    def test_suspension_blocks_then_restoration_allows(self) -> None:
        svc, h, _ = build_world()
        case_id = svc.open_case("H", "P", "S", "二级", ALL_MATERIALS, at="2026-03-01")
        svc.approve(case_id, as_of="2026-03-01")

        # 新案件在暂停期间被拒
        svc.set_cert_status("P", "暂停", at="2026-05-01")
        c2 = svc.open_case("H", "P", "S", "二级", ALL_MATERIALS, at="2026-05-02")
        self.assertEqual(svc.evaluate(c2, as_of="2026-05-02")["decision"], "拒绝")
        with self.assertRaises(QualificationError):
            svc.approve(c2, as_of="2026-05-02")

        svc.set_cert_status("P", "有效", at="2026-06-01")
        c3 = svc.open_case("H", "P", "S", "二级", ALL_MATERIALS, at="2026-06-02")
        svc.approve(c3, as_of="2026-06-02")
        self.assertEqual(len(svc.list_grants(c3)), 2)

    def test_institution_suspension_blocks_new_project(self) -> None:
        svc, h, _ = build_world()
        svc.set_institution_qualification_status("H", "暂停", at="2026-05-01")
        case_id = svc.open_case("H", "P", "S", "一级", ALL_MATERIALS, at="2026-05-02")
        result = svc.evaluate(case_id, as_of="2026-05-02")
        self.assertEqual(result["decision"], "拒绝")
        rules = {r["rule_code"]: r for r in result["rules"]}
        self.assertFalse(rules["R06"]["passed"])

    def test_cross_institution_filing_scopes_and_limits_grade(self) -> None:
        """跨机构备案：无备案被拒；备案后按备案等级限缩。"""
        svc, h, m = build_world()
        # M 取得眼科二级机构资质
        svc.register_institution_qualification(
            "M", [{"category_code": "D", "grade": "三级"},
                  {"category_code": "S", "grade": "二级"}],
            "2026-01-01", "2027-12-31", at="2026-01-01")

        # 无备案：人员在 M 的范围全部拒绝（注册机构不符）
        scope = svc.personnel_scope("P", "M", as_of="2026-04-01")
        self.assertFalse(any(i["allowed"] for i in scope["items"]))

        # 备案只到眼科一级
        svc.file_cross_institution(
            "P", "H", "M", "S", "一级", "2026-02-01", valid_to="2026-12-31", at="2026-02-01")
        case_id = svc.open_case("M", "P", "S", "二级",
                                [{"material_type": "机构资质凭证", "file_ref": "a"},
                                 {"material_type": "执业证凭证", "file_ref": "b"},
                                 {"material_type": "主诊资格凭证", "file_ref": "c"},
                                 {"material_type": "注册或备案凭证", "file_ref": "d"}],
                                at="2026-04-01")
        result = svc.evaluate(case_id, as_of="2026-04-01")
        self.assertEqual(result["decision"], "拒绝")
        rules = {r["rule_code"]: r for r in result["rules"]}
        self.assertIn("备案", rules["R12"]["detail"])
        self.assertFalse(rules["R12"]["passed"], "备案一级不覆盖二级")

        # 一级申请可批准
        c2 = svc.open_case("M", "P", "S", "一级",
                           [{"material_type": "机构资质凭证", "file_ref": "a"},
                            {"material_type": "执业证凭证", "file_ref": "b"},
                            {"material_type": "主诊资格凭证", "file_ref": "c"},
                            {"material_type": "注册或备案凭证", "file_ref": "d"}],
                           at="2026-04-02")
        svc.approve(c2, as_of="2026-04-02")

        # 备案结束后，原批准不变，新申请被拒
        svc.close_cross_filing(1, "2026-08-01")
        c3 = svc.open_case("M", "P", "S", "一级",
                           [{"material_type": "机构资质凭证", "file_ref": "a"},
                            {"material_type": "执业证凭证", "file_ref": "b"},
                            {"material_type": "主诊资格凭证", "file_ref": "c"},
                            {"material_type": "注册或备案凭证", "file_ref": "d"}],
                           at="2026-09-01")
        self.assertEqual(svc.evaluate(c3, as_of="2026-09-01")["decision"], "拒绝")
        self.assertEqual(len(svc.list_grants(c2)), 2)


class ApprovalTest(unittest.TestCase):
    def test_atomic_approval_writes_two_subject_grants(self) -> None:
        svc, h, _ = build_world()
        case_id = svc.open_case("H", "P", "S", "二级", ALL_MATERIALS, at="2026-03-01")
        out = svc.approve(case_id, as_of="2026-03-01")
        grants = out["grants"]
        self.assertEqual([g["subject_type"] for g in grants], ["人员", "机构"])
        for grant in grants:
            self.assertEqual(grant["category_code"], "S")
            self.assertEqual(grant["grade"], "二级")
            self.assertTrue(grant["evidence_id"].startswith("EV-"))

    def test_duplicate_grant_blocked_by_constraint(self) -> None:
        """直接构造第二次批准：唯一约束兜底，且不会只插入一条。"""
        svc, h, _ = build_world()
        case_id = svc.open_case("H", "P", "S", "二级", ALL_MATERIALS, at="2026-03-01")
        svc.approve(case_id, as_of="2026-03-01")

        # 手动把案件改回待决定，模拟绕过状态机；唯一约束仍应阻止重复授权
        svc.conn.execute("UPDATE cases SET state = '待核验' WHERE id = ?", (case_id,))
        with self.assertRaises(DuplicateGrantError):
            svc.approve(case_id, as_of="2026-03-02")
        count = svc.conn.execute(
            "SELECT COUNT(*) AS c FROM grants WHERE case_id = ?", (case_id,)).fetchone()["c"]
        self.assertEqual(count, 2, "回滚后仍只有机构+人员两条授权")

    def test_reject_is_idempotent_block_and_no_grants(self) -> None:
        svc, h, _ = build_world()
        case_id = svc.open_case("H", "P", "S", "四级", ALL_MATERIALS, at="2026-03-01")
        svc.reject(case_id, reason="等级不符", as_of="2026-03-02")
        self.assertEqual(svc.list_grants(case_id), [])
        with self.assertRaises(DuplicateGrantError):
            svc.approve(case_id, as_of="2026-03-03")
        with self.assertRaises(DuplicateGrantError):
            svc.reject(case_id, as_of="2026-03-03")

    def test_supplement_after_decided_keeps_original_decision(self) -> None:
        """材料补交保留原决定：已拒绝案件补交后不翻案，只追加记录。"""
        svc, h, _ = build_world()
        case_id = svc.open_case("H", "P", "S", "四级", ALL_MATERIALS, at="2026-03-01")
        svc.reject(case_id, reason="等级不符", as_of="2026-03-02")
        svc.supplement_material(
            case_id, [{"material_type": "情况说明", "file_ref": "note.pdf"}], at="2026-03-10")
        case = svc.conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        self.assertEqual(case["decision"], "拒绝")
        self.assertEqual(case["decided_at"], "2026-03-02")
        actions = [row["action"] for row in svc.conn.execute(
            "SELECT action FROM case_events WHERE case_id = ? ORDER BY id", (case_id,))]
        self.assertIn("材料补交", actions)


class ExplanationTest(unittest.TestCase):
    def test_every_denial_has_rule_detail_and_evidence(self) -> None:
        svc, h, m = build_world()
        case_id = svc.open_case("M", "P", "S", "四级", [], at="2026-03-01")
        result = svc.evaluate(case_id, as_of="2026-03-01")
        self.assertEqual(result["decision"], "补交材料")
        failed = [r for r in result["rules"] if not r["passed"]]
        self.assertTrue(failed)
        for rule in failed:
            self.assertTrue(rule["detail"].strip())
            self.assertIsInstance(rule["evidence"], list)
        # R14 明确列出缺失材料
        r14 = next(r for r in result["rules"] if r["rule_code"] == "R14")
        self.assertEqual(len(r14["evidence"][0]["missing"]), 4)

    def test_evidence_snapshot_frozen_at_approval(self) -> None:
        svc, h, _ = build_world()
        case_id = svc.open_case("H", "P", "S", "三级", ALL_MATERIALS, at="2026-03-01")
        out = svc.approve(case_id, as_of="2026-03-01")
        evidence_id = out["grants"][0]["evidence_id"]
        ev = svc.conn.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
        self.assertEqual(ev["evidence_type"], "核验快照")
        # 事后吊销证书，不影响快照
        svc.set_cert_status("P", "吊销", at="2026-09-01")
        ev2 = svc.conn.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
        self.assertEqual(dict(ev2)["payload_json"], dict(ev)["payload_json"])


if __name__ == "__main__":
    unittest.main()
