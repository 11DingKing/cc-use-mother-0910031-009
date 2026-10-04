"""应用服务层：登记、时态事件、核验案件与原子批准。

设计要点：
- 续期/暂停/跨机构备案/材料补交全部 append-only：新版本或新事件行，
  历史决定及其证据快照永不改写；
- 批准动作在单个数据库事务内完成"重新核验 → 快照存证 → 双主体授权写入
  → 案件状态翻转 → 事件登记"，任一步失败整体回滚；
- grants 的唯一约束 + 案件状态机双重防止重复授予；
- 每个写方法自行开启事务，可被 API、CLI、测试直接复用。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .models import (
    ACTION_APPROVE,
    ACTION_APPROVE_BLOCKED,
    ACTION_ARCHIVE,
    ACTION_REJECT,
    ACTION_REQUEST_MATERIAL,
    ACTION_SUBMIT,
    ACTION_SUPPLEMENT,
    CASE_DECIDED,
    CASE_PENDING,
    CASE_PROCESSING,
    CERT_REVOKED,
    DECISION_ALLOW,
    DECISION_DENY,
    DECISION_PENDING_MATERIAL,
    DuplicateGrantError,
    EVIDENCE_DECISION_SNAPSHOT,
    EVIDENCE_MATERIAL,
    GRADE_RANK,
    NotFoundError,
    QualificationError,
)
from .rules import build_snapshot, evaluate_case
from .scopes import compute_institution_scope, compute_personnel_scope


class QualificationService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _get(self, sql: str, params: tuple, label: str) -> sqlite3.Row:
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            raise NotFoundError(label)
        return row

    def _require_case(self, case_id: str) -> sqlite3.Row:
        return self._get("SELECT * FROM cases WHERE id = ?", (case_id,), f"案件 {case_id} 不存在")

    def _event(self, case_id: str, action: str, detail: dict[str, Any], actor: str, at: str) -> None:
        self.conn.execute(
            "INSERT INTO case_events (case_id, action, detail_json, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (case_id, action, json.dumps(detail, ensure_ascii=False), actor, at),
        )

    def _add_evidence(self, evidence_type: str, title: str, payload: dict[str, Any], at: str) -> str:
        evidence_id = f"EV-{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            "INSERT INTO evidence (id, evidence_type, title, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (evidence_id, evidence_type, title, json.dumps(payload, ensure_ascii=False), at),
        )
        return evidence_id

    def _touch(self, case_id: str, at: str) -> None:
        self.conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (at, case_id))

    def _void_active_grants(
        self, where_sql: str, params: tuple, reason: str, at: str,
    ) -> int:
        """使匹配的有效授权失效（历史行保留），返回失效条数。

        机构与人员两条授权成对失效：任一主体失去资格，该项目授权即失效；
        恢复后须重新立案批准，原决定与旧授权行均可追溯。
        """
        cur = self.conn.execute(
            f"""UPDATE grants SET status = '已失效', void_reason = ?, voided_at = ?
                WHERE status = '有效' AND {where_sql}""",
            (reason, at, *params),
        )
        return cur.rowcount

    # ------------------------------------------------------------------
    # 基础登记：机构、人员、项目分类
    # ------------------------------------------------------------------
    def register_institution(self, institution_id: str, name: str, *, at: str | None = None) -> dict:
        at = at or _today()
        with self.conn:
            self.conn.execute(
                "INSERT INTO institutions (id, name, created_at) VALUES (?, ?, ?)",
                (institution_id, name, at),
            )
        return {"id": institution_id, "name": name}

    def register_personnel(self, personnel_id: str, name: str, *, at: str | None = None) -> dict:
        at = at or _today()
        with self.conn:
            self.conn.execute(
                "INSERT INTO personnel (id, name, created_at) VALUES (?, ?, ?)",
                (personnel_id, name, at),
            )
        return {"id": personnel_id, "name": name}

    def register_category(self, code: str, name: str, max_grade: str) -> dict:
        if max_grade not in GRADE_RANK:
            raise QualificationError(f"未知项目等级：{max_grade}")
        with self.conn:
            self.conn.execute(
                "INSERT INTO project_categories (code, name, max_grade) VALUES (?, ?, ?)",
                (code, name, max_grade),
            )
        return {"code": code, "name": name, "max_grade": max_grade}

    # ------------------------------------------------------------------
    # 机构资质版本登记 / 续期 / 暂停 / 恢复
    # ------------------------------------------------------------------
    def _next_inst_version(self, institution_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) AS v FROM institution_qual_versions "
            "WHERE institution_id = ?",
            (institution_id,),
        ).fetchone()
        return int(row["v"]) + 1

    def register_institution_qualification(
        self, institution_id: str, scope: list[dict[str, str]],
        valid_from: str, valid_to: str, *, note: str = "", at: str | None = None,
    ) -> int:
        """登记机构资质新版本。scope=[{"category_code","grade"}]。

        初次登记与续期统一走此方法：续期即颁发新版本，旧版本保留可查。
        """
        at = at or _today()
        self._get("SELECT 1 FROM institutions WHERE id = ?", (institution_id,),
                  f"机构 {institution_id} 不存在")
        self._validate_scope(scope)
        version_no = self._next_inst_version(institution_id)
        with self.conn:
            self.conn.execute(
                """INSERT INTO institution_qual_versions
                   (institution_id, version_no, status, valid_from, valid_to, scope_json, note, created_at)
                   VALUES (?, ?, '有效', ?, ?, ?, ?, ?)""",
                (institution_id, version_no, valid_from, valid_to,
                 json.dumps(scope, ensure_ascii=False), note, at),
            )
        return version_no

    def set_institution_qualification_status(
        self, institution_id: str, status: str, *, at: str | None = None,
    ) -> int:
        """暂停/恢复：在最新版本上登记状态变化（不改有效期、不删历史）。"""
        if status not in ("有效", "暂停"):
            raise QualificationError("机构资质状态只能是 有效/暂停")
        at = at or _today()
        row = self.conn.execute(
            """SELECT id, version_no FROM institution_qual_versions
               WHERE institution_id = ? AND valid_from <= ?
               ORDER BY version_no DESC LIMIT 1""",
            (institution_id, at),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"机构 {institution_id} 无已生效资质版本")
        with self.conn:
            self.conn.execute(
                "UPDATE institution_qual_versions SET status = ? WHERE id = ?",
                (status, row["id"]),
            )
            if status == "暂停":
                self._void_active_grants(
                    "institution_id = ?", (institution_id,),
                    f"机构资质暂停（v{row['version_no']}）", at)
        return int(row["version_no"])

    # ------------------------------------------------------------------
    # 人员执业证：版本登记（续期颁发新版本）、暂停/恢复/吊销
    # ------------------------------------------------------------------
    def _next_cert_version(self, personnel_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) AS v FROM personnel_cert_versions "
            "WHERE personnel_id = ?",
            (personnel_id,),
        ).fetchone()
        return int(row["v"]) + 1

    def register_cert(
        self, personnel_id: str, license_no: str, scope: list[dict[str, str]],
        valid_from: str, valid_to: str, *, note: str = "", at: str | None = None,
    ) -> int:
        """登记执业证新版本；续期传入新有效期即生成新版本，旧证保留。"""
        at = at or _today()
        self._get("SELECT 1 FROM personnel WHERE id = ?", (personnel_id,),
                  f"人员 {personnel_id} 不存在")
        self._validate_scope(scope)
        version_no = self._next_cert_version(personnel_id)
        with self.conn:
            self.conn.execute(
                """INSERT INTO personnel_cert_versions
                   (personnel_id, version_no, license_no, status, valid_from, valid_to,
                    scope_json, note, created_at)
                   VALUES (?, ?, ?, '有效', ?, ?, ?, ?, ?)""",
                (personnel_id, version_no, license_no, valid_from, valid_to,
                 json.dumps(scope, ensure_ascii=False), note, at),
            )
        return version_no

    def set_cert_status(
        self, personnel_id: str, status: str, *, at: str | None = None,
    ) -> int:
        """暂停/恢复/吊销最新已生效执业证版本。"""
        if status not in ("有效", "暂停", CERT_REVOKED):
            raise QualificationError("执业证状态只能是 有效/暂停/吊销")
        at = at or _today()
        row = self.conn.execute(
            """SELECT id, version_no FROM personnel_cert_versions
               WHERE personnel_id = ? AND valid_from <= ?
               ORDER BY version_no DESC LIMIT 1""",
            (personnel_id, at),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"人员 {personnel_id} 无已生效执业证版本")
        with self.conn:
            self.conn.execute(
                "UPDATE personnel_cert_versions SET status = ? WHERE id = ?",
                (status, row["id"]),
            )
            if status in ("暂停", CERT_REVOKED):
                self._void_active_grants(
                    "personnel_id = ?", (personnel_id,),
                    f"执业证{status}（v{row['version_no']}）", at)
        return int(row["version_no"])

    # ------------------------------------------------------------------
    # 主诊资格：版本登记、暂停/恢复/吊销
    # ------------------------------------------------------------------
    def register_attending_qualification(
        self, personnel_id: str, category_code: str, max_grade: str,
        valid_from: str, valid_to: str, *, note: str = "", at: str | None = None,
    ) -> int:
        """登记某分类的主诊资格新版本（按分类各自递增版本号）。"""
        at = at or _today()
        if max_grade not in GRADE_RANK:
            raise QualificationError(f"未知项目等级：{max_grade}")
        self._get("SELECT 1 FROM personnel WHERE id = ?", (personnel_id,),
                  f"人员 {personnel_id} 不存在")
        self._get("SELECT 1 FROM project_categories WHERE code = ?", (category_code,),
                  f"项目分类 {category_code} 不存在")
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) AS v FROM attending_qual_versions "
            "WHERE personnel_id = ? AND category_code = ?",
            (personnel_id, category_code),
        ).fetchone()
        version_no = int(row["v"]) + 1
        with self.conn:
            self.conn.execute(
                """INSERT INTO attending_qual_versions
                   (personnel_id, version_no, category_code, max_grade, status,
                    valid_from, valid_to, note, created_at)
                   VALUES (?, ?, ?, ?, '有效', ?, ?, ?, ?)""",
                (personnel_id, version_no, category_code, max_grade,
                 valid_from, valid_to, note, at),
            )
        return version_no

    def set_attending_status(
        self, personnel_id: str, category_code: str, status: str, *, at: str | None = None,
    ) -> int:
        if status not in ("有效", "暂停", CERT_REVOKED):
            raise QualificationError("主诊资格状态只能是 有效/暂停/吊销")
        at = at or _today()
        row = self.conn.execute(
            """SELECT id, version_no FROM attending_qual_versions
               WHERE personnel_id = ? AND category_code = ? AND valid_from <= ?
               ORDER BY version_no DESC LIMIT 1""",
            (personnel_id, category_code, at),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"人员 {personnel_id} 在分类 {category_code} 无已生效主诊资格")
        with self.conn:
            self.conn.execute("UPDATE attending_qual_versions SET status = ? WHERE id = ?",
                              (status, row["id"]))
            if status in ("暂停", CERT_REVOKED):
                self._void_active_grants(
                    "personnel_id = ? AND category_code = ?",
                    (personnel_id, category_code),
                    f"主诊资格{status}（{category_code} v{row['version_no']}）", at)
        return int(row["version_no"])

    # ------------------------------------------------------------------
    # 注册地点与跨机构备案
    # ------------------------------------------------------------------
    def register_practice_site(
        self, personnel_id: str, institution_id: str, valid_from: str,
        *, valid_to: str | None = None, at: str | None = None,
    ) -> int:
        """登记主执业机构。同一人员同一机构重复登记将被拒绝。"""
        at = at or _today()
        self._get("SELECT 1 FROM personnel WHERE id = ?", (personnel_id,),
                  f"人员 {personnel_id} 不存在")
        self._get("SELECT 1 FROM institutions WHERE id = ?", (institution_id,),
                  f"机构 {institution_id} 不存在")
        exists = self.conn.execute(
            "SELECT 1 FROM registrations WHERE personnel_id = ? AND institution_id = ?",
            (personnel_id, institution_id),
        ).fetchone()
        if exists is not None:
            raise QualificationError("该人员在此机构的注册记录已存在")
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO registrations
                   (personnel_id, institution_id, valid_from, valid_to, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (personnel_id, institution_id, valid_from, valid_to, at),
            )
            rid = int(cur.lastrowid)
        return rid

    def close_registration(self, registration_id: int, valid_to: str) -> None:
        """结束注册（变更注册时关闭旧地点，历史行保留，相关授权失效）。"""
        with self.conn:
            row = self.conn.execute(
                "SELECT * FROM registrations WHERE id = ? AND valid_to IS NULL",
                (registration_id,)).fetchone()
            self.conn.execute(
                "UPDATE registrations SET valid_to = ? WHERE id = ? AND valid_to IS NULL",
                (valid_to, registration_id),
            )
            if row is not None:
                self._void_active_grants(
                    "personnel_id = ? AND institution_id = ?",
                    (row["personnel_id"], row["institution_id"]),
                    f"注册地点结束（#{registration_id}）", valid_to)

    def file_cross_institution(
        self, personnel_id: str, home_institution_id: str, host_institution_id: str,
        category_code: str, max_grade: str, valid_from: str,
        *, valid_to: str | None = None, at: str | None = None,
    ) -> int:
        """登记跨机构备案。备案在分类与等级上限定人员在执业机构的范围。"""
        at = at or _today()
        if max_grade not in GRADE_RANK:
            raise QualificationError(f"未知项目等级：{max_grade}")
        if home_institution_id == host_institution_id:
            raise QualificationError("跨机构备案的主执业机构与执业机构不能相同")
        for sql, param, label in (
            ("SELECT 1 FROM personnel WHERE id = ?", personnel_id, f"人员 {personnel_id} 不存在"),
            ("SELECT 1 FROM institutions WHERE id = ?", home_institution_id,
             f"机构 {home_institution_id} 不存在"),
            ("SELECT 1 FROM institutions WHERE id = ?", host_institution_id,
             f"机构 {host_institution_id} 不存在"),
            ("SELECT 1 FROM project_categories WHERE code = ?", category_code,
             f"项目分类 {category_code} 不存在"),
        ):
            self._get(sql, (param,), label)
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO cross_filings
                   (personnel_id, home_institution_id, host_institution_id, category_code,
                    max_grade, status, valid_from, valid_to, created_at)
                   VALUES (?, ?, ?, ?, ?, '有效', ?, ?, ?)""",
                (personnel_id, home_institution_id, host_institution_id, category_code,
                 max_grade, valid_from, valid_to, at),
            )
            fid = int(cur.lastrowid)
        return fid

    def close_cross_filing(self, filing_id: int, valid_to: str) -> None:
        with self.conn:
            row = self.conn.execute(
                "SELECT * FROM cross_filings WHERE id = ?", (filing_id,)).fetchone()
            self.conn.execute(
                "UPDATE cross_filings SET status = '已结束', valid_to = ? WHERE id = ?",
                (valid_to, filing_id),
            )
            if row is not None:
                self._void_active_grants(
                    "personnel_id = ? AND institution_id = ? AND category_code = ?",
                    (row["personnel_id"], row["host_institution_id"], row["category_code"]),
                    f"跨机构备案结束（#{filing_id}）", valid_to)

    # ------------------------------------------------------------------
    # 核验案件
    # ------------------------------------------------------------------
    def open_case(
        self, institution_id: str, personnel_id: str, category_code: str, grade: str,
        materials: list[dict[str, str]], *, case_id: str | None = None,
        at: str | None = None, actor: str = "机构合规员",
    ) -> str:
        """机构新增项目申请：登记案件、材料与初始核验快照，进入待核验。"""
        at = at or _today()
        if grade not in GRADE_RANK:
            raise QualificationError(f"未知项目等级：{grade}")
        self._get("SELECT 1 FROM institutions WHERE id = ?", (institution_id,),
                  f"机构 {institution_id} 不存在")
        self._get("SELECT 1 FROM personnel WHERE id = ?", (personnel_id,),
                  f"人员 {personnel_id} 不存在")
        self._get("SELECT 1 FROM project_categories WHERE code = ?", (category_code,),
                  f"项目分类 {category_code} 不存在")

        case_id = case_id or f"CASE-{uuid.uuid4().hex[:10]}"
        with self.conn:
            self.conn.execute(
                """INSERT INTO cases
                   (id, institution_id, personnel_id, category_code, grade,
                    state, decision, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                (case_id, institution_id, personnel_id, category_code, grade,
                 CASE_PENDING, at, at),
            )
            for item in materials:
                self._insert_material(case_id, item, at)
            self._event(case_id, ACTION_SUBMIT,
                        {"materials": [m["material_type"] for m in materials]}, actor, at)
            self._record_evaluation(case_id, at)
        return case_id

    def supplement_material(
        self, case_id: str, materials: list[dict[str, str]], *,
        at: str | None = None, actor: str = "机构合规员",
    ) -> dict[str, Any]:
        """材料补交：只追加材料与事件，并重新核验生成新快照。

        已决定/已归档的案件不改变其原决定，仅追加补交记录与新快照，
        历史快照原样保留；处理中案件补交后自动重新评估。
        """
        at = at or _today()
        case = self._require_case(case_id)
        if not materials:
            raise QualificationError("补交材料列表不能为空")
        with self.conn:
            for item in materials:
                self._insert_material(case_id, item, at)
            self._event(case_id, ACTION_SUPPLEMENT,
                        {"materials": [m["material_type"] for m in materials]}, actor, at)
            self._touch(case_id, at)
            evaluation = self._record_evaluation(case_id, at)
        return {"case_id": case_id, "state": case["state"], "evaluation": evaluation.to_dict()}

    def evaluate(self, case_id: str, *, as_of: str | None = None) -> dict[str, Any]:
        """只读核验：返回规则明细与双主体范围，不落任何决定。"""
        as_of = as_of or _today()
        case = self._require_case(case_id)
        evaluation = evaluate_case(self.conn, case, as_of)
        result = evaluation.to_dict()
        result["institution_scope"] = _scope_to_dict(evaluation.institution_scope)
        result["personnel_scope"] = _scope_to_dict(evaluation.personnel_scope)
        return result

    def approve(
        self, case_id: str, *, as_of: str | None = None, actor: str = "监管人员",
    ) -> dict[str, Any]:
        """原子批准：核验 → 存证 → 双主体授权 → 状态翻转 → 事件，单事务。

        - 核验结论不是"允许"则拒绝批准（材料缺→提示补件，范围不符→拒绝）；
        - 案件已决定则拒绝重复操作；
        - 授权唯一约束兜底，并发/重复批准不可能产生重复授权。
        """
        at = as_of or _today()
        case = self._require_case(case_id)
        if case["state"] == CASE_DECIDED:
            raise DuplicateGrantError(
                f"案件 {case_id} 已于 {case['decided_at']} 决定为「{case['decision']}」，"
                "不得重复授予；续期/变更请另行立案"
            )

        evaluation = evaluate_case(self.conn, case, at)
        if evaluation.decision == DECISION_PENDING_MATERIAL:
            with self.conn:
                self._event(case_id, ACTION_APPROVE_BLOCKED,
                            {"reason": "材料不齐", "summary": evaluation.summary}, actor, at)
                self._touch(case_id, at)
                self._record_evaluation(case_id, at)
            raise QualificationError("材料不齐，不能批准：" + evaluation.summary)
        if evaluation.decision == DECISION_DENY:
            with self.conn:
                self._event(case_id, ACTION_APPROVE_BLOCKED,
                            {"reason": "核验不通过", "summary": evaluation.summary}, actor, at)
                self._touch(case_id, at)
                self._record_evaluation(case_id, at)
            raise QualificationError("核验不通过，不能批准：" + evaluation.summary)

        snapshot = build_snapshot(self.conn, case, at)
        try:
            with self.conn:  # 单事务：以下全部成功才提交
                evidence_id = self._add_evidence(
                    EVIDENCE_DECISION_SNAPSHOT,
                    f"案件 {case_id} 批准证据快照",
                    {"snapshot": snapshot, "rules": evaluation.to_dict()["rules"]}, at,
                )
                for subject_type, subject_id in (
                    ("机构", case["institution_id"]),
                    ("人员", case["personnel_id"]),
                ):
                    self.conn.execute(
                        """INSERT INTO grants
                           (subject_type, subject_id, institution_id, personnel_id,
                            category_code, grade, case_id, evidence_id, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (subject_type, subject_id, case["institution_id"],
                         case["personnel_id"], case["category_code"], case["grade"],
                         case_id, evidence_id, at),
                    )
                cur = self.conn.execute(
                    """UPDATE cases SET state = ?, decision = ?, decided_at = ?, updated_at = ?
                       WHERE id = ? AND state <> ?""",
                    (CASE_DECIDED, DECISION_ALLOW, at, at, case_id, CASE_DECIDED),
                )
                if cur.rowcount == 0:
                    raise DuplicateGrantError("案件状态已变化，批准中止")
                self._event(case_id, ACTION_APPROVE,
                            {"evidence_id": evidence_id, "summary": evaluation.summary},
                            actor, at)
        except sqlite3.IntegrityError as exc:
            raise DuplicateGrantError("授权已存在，防止重复授予") from exc

        return {"case_id": case_id, "decision": DECISION_ALLOW, "grants": self.list_grants(case_id)}

    def reject(
        self, case_id: str, *, reason: str = "", as_of: str | None = None,
        actor: str = "监管人员",
    ) -> dict[str, Any]:
        """拒绝立案（监管直接否决）。已决定案件不可重复操作。"""
        at = as_of or _today()
        case = self._require_case(case_id)
        if case["state"] == CASE_DECIDED:
            raise DuplicateGrantError(f"案件 {case_id} 已决定，不能重复处置")
        evaluation = evaluate_case(self.conn, case, at)
        with self.conn:
            self.conn.execute(
                "UPDATE cases SET state = ?, decision = ?, decided_at = ?, updated_at = ? WHERE id = ?",
                (CASE_DECIDED, DECISION_DENY, at, at, case_id),
            )
            self._event(case_id, ACTION_REJECT,
                        {"manual_reason": reason, "summary": evaluation.summary}, actor, at)
        return {"case_id": case_id, "decision": DECISION_DENY}

    def request_materials(
        self, case_id: str, *, as_of: str | None = None, actor: str = "监管人员",
    ) -> dict[str, Any]:
        at = as_of or _today()
        case = self._require_case(case_id)
        if case["state"] == CASE_DECIDED:
            raise QualificationError("案件已决定，不能要求补件")
        evaluation = evaluate_case(self.conn, case, at)
        with self.conn:
            self.conn.execute(
                "UPDATE cases SET state = ?, updated_at = ? WHERE id = ?",
                (CASE_PROCESSING, at, case_id),
            )
            self._event(case_id, ACTION_REQUEST_MATERIAL,
                        {"summary": evaluation.summary}, actor, at)
            self._record_evaluation(case_id, at)
        return {"case_id": case_id, "state": CASE_PROCESSING, "evaluation": evaluation.to_dict()}

    def archive_case(self, case_id: str, *, at: str | None = None, actor: str = "监管人员") -> dict:
        at = at or _today()
        case = self._require_case(case_id)
        if case["state"] != CASE_DECIDED:
            raise QualificationError("只有已决定案件才能归档")
        with self.conn:
            self.conn.execute(
                "UPDATE cases SET state = ?, updated_at = ? WHERE id = ?",
                ("已归档", at, case_id),
            )
            self._event(case_id, ACTION_ARCHIVE, {}, actor, at)
        return {"case_id": case_id, "state": "已归档"}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_grants(self, case_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM grants WHERE case_id = ? ORDER BY subject_type", (case_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def case_timeline(self, case_id: str) -> list[dict[str, Any]]:
        """案件事件流 + 历次核验快照，证明"原决定保留"。"""
        self._require_case(case_id)
        events = [
            {"kind": "event", "at": row["created_at"], "action": row["action"],
             "actor": row["actor"], "detail": json.loads(row["detail_json"])}
            for row in self.conn.execute(
                "SELECT * FROM case_events WHERE case_id = ? ORDER BY id", (case_id,))
        ]
        evaluations = [
            {"kind": "evaluation", "at": row["created_at"], "as_of": row["as_of"],
             "decision": row["decision"], "summary": row["summary"],
             "rules": json.loads(row["rules_json"])}
            for row in self.conn.execute(
                "SELECT * FROM case_evaluations WHERE case_id = ? ORDER BY id", (case_id,))
        ]
        timeline = events + evaluations
        timeline.sort(key=lambda item: (item["at"], 0 if item["kind"] == "event" else 1))
        return timeline

    def institution_scope(self, institution_id: str, *, as_of: str | None = None) -> dict:
        as_of = as_of or _today()
        self._get("SELECT 1 FROM institutions WHERE id = ?", (institution_id,),
                  f"机构 {institution_id} 不存在")
        return _scope_to_dict(compute_institution_scope(self.conn, institution_id, as_of))

    def personnel_scope(
        self, personnel_id: str, institution_id: str, *, as_of: str | None = None,
    ) -> dict:
        as_of = as_of or _today()
        self._get("SELECT 1 FROM personnel WHERE id = ?", (personnel_id,),
                  f"人员 {personnel_id} 不存在")
        self._get("SELECT 1 FROM institutions WHERE id = ?", (institution_id,),
                  f"机构 {institution_id} 不存在")
        return _scope_to_dict(
            compute_personnel_scope(self.conn, personnel_id, institution_id, as_of))

    # ------------------------------------------------------------------
    # 私有辅助
    # ------------------------------------------------------------------
    def _validate_scope(self, scope: list[dict[str, str]]) -> None:
        if not isinstance(scope, list) or not scope:
            raise QualificationError("资质范围必须是非空列表")
        seen: set[tuple[str, str]] = set()
        for entry in scope:
            if set(entry) != {"category_code", "grade"}:
                raise QualificationError("范围条目必须且只能包含 category_code 与 grade")
            if entry["grade"] not in GRADE_RANK:
                raise QualificationError(f"未知项目等级：{entry['grade']}")
            key = (entry["category_code"], entry["grade"])
            if key in seen:
                raise QualificationError(f"范围条目重复：{key}")
            seen.add(key)

    def _insert_material(self, case_id: str, item: dict[str, str], at: str) -> None:
        for key in ("material_type", "file_ref"):
            if not item.get(key):
                raise QualificationError(f"材料缺少字段：{key}")
        evidence_id = self._add_evidence(
            EVIDENCE_MATERIAL, f"{item['material_type']}（{case_id}）",
            {"case_id": case_id, **item}, at,
        )
        self.conn.execute(
            """INSERT INTO case_materials
               (case_id, material_type, file_ref, submitted_at, note)
               VALUES (?, ?, ?, ?, ?)""",
            (case_id, item["material_type"], item["file_ref"], at, evidence_id),
        )

    def _record_evaluation(self, case_id: str, as_of: str):
        """执行核验并把规则明细与证据快照写入历史（append-only）。"""
        case = self.conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        evaluation = evaluate_case(self.conn, case, as_of)
        snapshot = build_snapshot(self.conn, case, as_of)
        self.conn.execute(
            """INSERT INTO case_evaluations
               (case_id, as_of, decision, summary, rules_json, snapshot_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (case_id, as_of, evaluation.decision, evaluation.summary,
             json.dumps(evaluation.to_dict()["rules"], ensure_ascii=False),
             json.dumps(snapshot, ensure_ascii=False, default=str), as_of),
        )
        return evaluation


def _scope_to_dict(scope) -> dict[str, Any]:
    return {
        "subject_type": scope.subject_type,
        "subject_id": scope.subject_id,
        "as_of": scope.as_of,
        "items": [
            {
                "category_code": item.category_code,
                "category_name": item.category_name,
                "grade": item.grade,
                "allowed": item.allowed,
                "reasons": list(item.reasons),
                "evidence": list(item.evidence),
            }
            for item in scope.items
        ],
    }


def _today() -> str:
    from datetime import date
    return date.today().isoformat()
