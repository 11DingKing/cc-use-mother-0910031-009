"""核验后端的端到端回归测试：规则引擎、时态事件、证据快照、防重复授权。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification_verifier.seed import seed_demo
from qualification_verifier.service import (
    ConflictError,
    DuplicateGrantError,
    NotFoundError,
    QualificationService,
)
from qualification_verifier.storage import connect, init_db


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "test.db")
        init_db(self.conn)
        self.svc = QualificationService(self.conn)
        self.ids = seed_demo(self.svc)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()


class RuleEngineTest(ServiceTestBase):
    def test_all_match_case_is_allowed_with_explanations(self) -> None:
        ev = self.svc.latest_evaluation("CASE-OK")
        self.assertEqual(ev["overall_verdict"], "允许")
        self.assertEqual(ev["institution_verdict"], "允许")
        self.assertEqual(ev["person_verdict"], "允许")
        rules = {f["rule"]: f for f in ev["findings"]}
        # 9 条规则全部给出结论，且每条都有理由与证据
        self.assertEqual(set(rules), {
            "I-001", "I-002", "I-003", "I-004",
            "P-001", "P-002", "P-003", "P-004", "P-005"})
        for finding in ev["findings"]:
            self.assertEqual(finding["verdict"], "允许")
            self.assertTrue(finding["reason"])
            self.assertTrue(finding["evidence"])

    def test_cert_valid_but_scope_mismatch_must_deny(self) -> None:
        """核心防误判：李医生证书在有效期内，但执业范围不覆盖普通外科。"""
        ev = self.svc.latest_evaluation("CASE-SCOPE-DENY")
        self.assertEqual(ev["overall_verdict"], "拒绝")
        p001 = next(f for f in ev["findings"] if f["rule"] == "P-001")
        p002 = next(f for f in ev["findings"] if f["rule"] == "P-002")
        self.assertEqual(p001["verdict"], "允许")  # 证书本身有效
        self.assertEqual(p002["verdict"], "拒绝")  # 范围不符必须拦截
        self.assertIn("不覆盖", p002["reason"])
        self.assertIn("practice_scope", p002["evidence"][0]["excerpt"])
        self.assertIn("P-002", ev["denied_rules"])

    def test_attending_level_insufficient_is_denied(self) -> None:
        ev = self.svc.latest_evaluation("CASE-LEVEL-DENY")
        self.assertEqual(ev["overall_verdict"], "拒绝")
        p002 = next(f for f in ev["findings"] if f["rule"] == "P-002")
        p004 = next(f for f in ev["findings"] if f["rule"] == "P-004")
        self.assertEqual(p002["verdict"], "允许")
        self.assertEqual(p004["verdict"], "拒绝")
        self.assertIn("等级", p004["reason"])

    def test_institution_scope_tree_expansion_and_level(self) -> None:
        # 瑞康医院许可含 SURG/MED-AESTH 且等级 2：等级1的四级项目不在范围内
        scope = self.svc.institution_scope("INST-RK", "2026-06-01T00:00:00Z")
        self.assertIn("SURG-GS", scope["allowed_projects"])
        self.assertIn("AESTH-SURG", scope["allowed_projects"])
        self.assertNotIn("AESTH-SURG-4", scope["allowed_projects"])
        self.assertEqual(len(scope["sites"]), 1)

    def test_person_scope_grouped_by_registration(self) -> None:
        scope = self.svc.person_scope("PER-ZHANG", "2026-06-01T00:00:00Z")
        self.assertEqual(len(scope["registrations"]), 1)
        reg = scope["registrations"][0]
        self.assertEqual(reg["institution_id"], "INST-RK")
        self.assertIn("SURG-GS", reg["allowed_projects"])
        self.assertNotIn("AESTH-SURG", reg["allowed_projects"])

    def test_filing_case_flips_to_allowed_after_cross_institution(self) -> None:
        history = self.svc.list_evaluations("CASE-FILING-OK")
        triggers = [
            (self.svc.get_evaluation("CASE-FILING-OK", h["id"])["trigger"],
             self.svc.get_evaluation("CASE-FILING-OK", h["id"])["overall_verdict"])
            for h in history]
        self.assertIn(("登记评估", "拒绝"), triggers)   # 初次 P-005 拒绝
        self.assertEqual(triggers[-1][1], "允许")       # 备案+补交后允许
        latest = self.svc.latest_evaluation("CASE-FILING-OK")
        p005 = next(f for f in latest["findings"] if f["rule"] == "P-005")
        self.assertEqual(p005["verdict"], "允许")
        self.assertIn("跨机构备案", p005["reason"])


class TemporalEventTest(ServiceTestBase):
    def test_suspension_denies_while_active_and_resume_restores(self) -> None:
        self.assertEqual(self.svc.latest_evaluation("CASE-OK")["overall_verdict"], "允许")
        self.svc.suspend_certificate("PER-ZHANG", "2026-03-01T00:00:00Z", reason="离岗检查")
        suspended = self.svc.latest_evaluation("CASE-OK")
        self.assertEqual(suspended["overall_verdict"], "拒绝")
        p001 = next(f for f in suspended["findings"] if f["rule"] == "P-001")
        self.assertEqual(p001["verdict"], "拒绝")
        self.assertIn("暂停", p001["reason"])
        self.assertEqual(p001["evidence"][1]["kind"], "暂停事件")
        # 暂停时点之前的历史评估结论不变
        first = self.svc.get_evaluation("CASE-OK", 1)
        self.assertEqual(first["overall_verdict"], "允许")

        self.svc.resume_certificate("PER-ZHANG", "2026-05-01T00:00:00Z")
        self.assertEqual(self.svc.latest_evaluation("CASE-OK")["overall_verdict"], "允许")

    def test_renewal_appends_version_and_reevaluates(self) -> None:
        # 登记一份 2028 年到期的新证书版本（旧版本置“已换发”）
        result = self.svc.register_certificate(
            "PER-ZHANG", "CERT-ZHANG-NEW", ["SURG"],
            valid_from="2028-01-01T00:00:00Z", valid_to="2030-12-31T23:59:59Z")
        self.assertEqual(result["version"], 2)
        old = self.conn.execute(
            "SELECT status FROM person_certificate_versions WHERE person_id='PER-ZHANG'"
            " ORDER BY version").fetchall()
        self.assertEqual([r["status"] for r in old], ["已换发", "有效"])
        # 2029 年的评估使用新版本
        ev = self.svc.add_evaluation("CASE-OK", "时态抽查", "2029-06-01T00:00:00Z")
        p001 = next(f for f in ev["findings"] if f["rule"] == "P-001")
        self.assertEqual(p001["verdict"], "允许")
        self.assertIn("第2版", p001["reason"])

    def test_double_suspend_is_rejected(self) -> None:
        self.svc.suspend_certificate("PER-ZHANG", "2026-03-01T00:00:00Z")
        with self.assertRaises(ConflictError):
            self.svc.suspend_certificate("PER-ZHANG", "2026-04-01T00:00:00Z")


class DecisionAndGrantTest(ServiceTestBase):
    def _approve(self, case_id: str, key: str = "key-1"):
        return self.svc.decide(case_id, "批准", "监管员甲", "核验全部通过", key)

    def test_approve_atomically_writes_decision_and_grant(self) -> None:
        out = self._approve("CASE-OK")
        self.assertEqual(out["result"], "批准")
        self.assertIsNotNone(out["grant"])
        self.assertEqual(out["grant"]["project_code"], "SURG-GS")
        self.assertEqual(out["grant"]["status"], "有效")
        case = self.svc.get_case("CASE-OK")
        self.assertEqual(case["case"]["status"], "已决定")
        self.assertEqual(len(case["decisions"]), 1)

    def test_deny_cannot_be_approved_without_new_allow_evaluation(self) -> None:
        with self.assertRaises(ConflictError):
            self.svc.decide("CASE-SCOPE-DENY", "批准", "监管员甲", "尝试批准", "k1")
        # 拒绝决定允许写入
        rej = self.svc.decide("CASE-SCOPE-DENY", "拒绝", "监管员甲", "范围不符", "k2")
        self.assertIsNone(rej["grant"])

    def test_duplicate_grant_prevented(self) -> None:
        self._approve("CASE-OK")
        # 同幂等键：回放同一决定，不新增授权
        replay = self.svc.decide("CASE-OK", "批准", "监管员甲", "重复提交", "key-1")
        self.assertTrue(replay.get("idempotent_replay"))
        grants = self.conn.execute(
            "SELECT COUNT(*) AS c FROM grants WHERE case_id='CASE-OK'").fetchone()
        self.assertEqual(grants["c"], 1)
        # 不同幂等键再次批准：被业务守卫拦截
        with self.assertRaises(DuplicateGrantError):
            self._approve("CASE-OK", key="key-2")

    def test_same_subject_project_grant_unique_index(self) -> None:
        """即使绕过案件层，数据库部分唯一索引也阻止同机构-地点-人员-项目重复授权。"""
        self._approve("CASE-OK")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO grants(case_id, institution_id, site_id, person_id, project_code,"
                " status, granted_by, granted_at, decision_id)"
                " VALUES('CASE-OK','INST-RK','SITE-RK-MAIN','PER-ZHANG','SURG-GS',"
                " '有效','x','2026-10-01T00:00:00Z',1)")

    def test_revoke_then_reapprove_with_fresh_evaluation(self) -> None:
        self._approve("CASE-OK", key="a1")
        self.svc.revoke_grant("CASE-OK", "监管员乙", "检查不合格")
        # 无新评估不能重批；补交材料产生新评估后可以
        with self.assertRaises(ConflictError):
            self._approve("CASE-OK", key="a2")
        self.svc.supplement_evidence("CASE-OK", "整改报告.pdf", {"fixed": True})
        out = self.svc.decide("CASE-OK", "批准", "监管员甲", "整改后重新批准", "a3")
        self.assertEqual(out["grant"]["status"], "有效")
        case = self.svc.get_case("CASE-OK")
        self.assertEqual(len(case["grants"]), 2)
        self.assertEqual([g["status"] for g in case["grants"]], ["撤销", "有效"])
        self.assertEqual([d["result"] for d in case["decisions"]], ["批准", "批准"])

    def test_revoke_then_case_is_archivable_history_kept(self) -> None:
        self._approve("CASE-OK")
        self.svc.revoke_grant("CASE-OK", "监管员乙", "事后检查不合格")
        self.svc.archive_case("CASE-OK")
        case = self.svc.get_case("CASE-OK")
        self.assertEqual(case["case"]["status"], "已归档")
        self.assertEqual(case["grants"][0]["status"], "撤销")
        # 决定与评估快照仍可回放
        self.assertEqual(len(case["decisions"]), 1)
        self.assertGreaterEqual(len(case["evaluation_history"]), 1)

    def test_reject_then_new_evidence_allows_new_decision_history_kept(self) -> None:
        """原拒绝决定保留；情况变化产生更新评估后可改判，不得在无新评估时改判。"""
        # 初次拒绝（范围不符）
        first = self.svc.decide("CASE-SCOPE-DENY", "拒绝", "监管员甲", "范围不符", "d1")
        self.assertIsNone(first["grant"])
        # 无新评估时尝试改判 -> 冲突
        with self.assertRaises(ConflictError):
            self.svc.decide("CASE-SCOPE-DENY", "批准", "监管员甲", "想改判", "d2")
        # 人员取得外科执业证新版本（范围覆盖），已决定案件需显式发起复评
        self.svc.register_certificate(
            "PER-LI", "CERT-LI-NEW", ["SURG"],
            valid_from="2026-06-01T00:00:00Z", valid_to="2030-12-31T23:59:59Z")
        self.svc.add_evaluation("CASE-SCOPE-DENY", trigger="换证后人工复评")
        latest = self.svc.latest_evaluation("CASE-SCOPE-DENY")
        self.assertEqual(latest["overall_verdict"], "允许")
        # 基于新评估批准：授权写入，旧拒绝决定仍在
        out = self.svc.decide("CASE-SCOPE-DENY", "批准", "监管员甲",
                              "换证后范围覆盖", "d3")
        self.assertIsNotNone(out["grant"])
        case = self.svc.get_case("CASE-SCOPE-DENY")
        self.assertEqual([d["result"] for d in case["decisions"]], ["拒绝", "批准"])
        self.assertEqual(len(case["grants"]), 1)

    def test_supplement_preserves_original_decision(self) -> None:
        # 对一个已拒绝后仍在办的案件补交材料：历史评估数量增加，结论可追溯
        before = len(self.svc.list_evaluations("CASE-SCOPE-DENY"))
        out = self.svc.supplement_evidence(
            "CASE-SCOPE-DENY", "情况说明.pdf", {"note": "补充说明"})
        self.assertIn("原决定", out["notice"])
        after = self.svc.list_evaluations("CASE-SCOPE-DENY")
        self.assertEqual(len(after), before + 1)
        self.assertEqual(after[-1]["trigger"], "材料补交")
        # 补交并不能改变范围事实，最新结论仍是拒绝且解释不变
        latest = self.svc.latest_evaluation("CASE-SCOPE-DENY")
        self.assertEqual(latest["overall_verdict"], "拒绝")
        self.assertIn("补交材料",
                      [e["kind"] for e in latest["evidence_bundle"]])

    def test_supplement_after_decision_keeps_decision_intact(self) -> None:
        """已批准案件补交材料：原批准与授权原样保留，另附新评估。"""
        self._approve("CASE-OK")
        original_grant = self.svc.get_case("CASE-OK")["grants"][0]
        out = self.svc.supplement_evidence("CASE-OK", "年度考核表.pdf", {"year": 2026})
        self.assertEqual(out["evaluation"]["trigger"], "材料补交")
        case = self.svc.get_case("CASE-OK")
        self.assertEqual(len(case["decisions"]), 1)
        self.assertEqual(case["grants"][0]["grant_id"], original_grant["grant_id"])
        self.assertEqual(case["grants"][0]["status"], "有效")


class NotFoundTest(ServiceTestBase):
    def test_missing_entity_raises(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.get_case("NOT-EXIST")
        with self.assertRaises(NotFoundError):
            self.svc.create_case("X", "NO-INST", "SITE-RK-MAIN", "PER-ZHANG", "SURG-GS")


if __name__ == "__main__":
    unittest.main()
