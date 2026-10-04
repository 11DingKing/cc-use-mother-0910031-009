"""应用服务层：登记、生命周期事件、评估快照与原子批准。

历史不变性：

* 评估结果（``evaluations``）与决定（``decisions``）只增不改。证书续期、
  暂停、跨机构备案、材料补交只会**追加**一条新评估并记录事件日志，原决定
  与其证据快照原样保留，可随时回放。
* 批准在一个 ``BEGIN IMMEDIATE`` 事务内完成“复核最新评估 → 写决定 →
  写授权”，配合数据库唯一索引保证：同一幂等键返回同一决定、同一案件/同一
  机构-地点-人员-项目不可能被重复授予。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from . import constants as C
from . import rules
from .storage import dumps, loads, now_iso


class ServiceError(Exception):
    """业务错误，映射为 API 4xx。"""


class NotFoundError(ServiceError):
    pass


class ConflictError(ServiceError):
    pass


class DuplicateGrantError(ConflictError):
    pass


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _require(conn: sqlite3.Connection, table: str, obj_id: str, label: str,
             key_col: str = "id") -> sqlite3.Row:
    row = conn.execute(f"SELECT * FROM {table} WHERE {key_col} = ?", (obj_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"{label}不存在：{obj_id}")
    return row


class QualificationService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------- 基础主体 ----------

    def create_institution(self, inst_id: str, name: str) -> dict[str, Any]:
        ts = now_iso()
        try:
            self.conn.execute(
                "INSERT INTO institutions(id, name, created_at) VALUES(?,?,?)",
                (inst_id, name, ts),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"机构已存在：{inst_id}") from exc
        return {"id": inst_id, "name": name, "created_at": ts}

    def create_person(self, person_id: str, name: str) -> dict[str, Any]:
        ts = now_iso()
        try:
            self.conn.execute(
                "INSERT INTO persons(id, name, created_at) VALUES(?,?,?)",
                (person_id, name, ts),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"人员已存在：{person_id}") from exc
        return {"id": person_id, "name": name, "created_at": ts}

    def register_site(self, inst_id: str, address: str, valid_from: str,
                      valid_to: str | None = None, site_id: str | None = None) -> dict[str, Any]:
        _require(self.conn, "institutions", inst_id, "机构")
        site_id = site_id or _uid("SITE")
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO institution_sites(id, institution_id, address, valid_from, valid_to, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (site_id, inst_id, address, valid_from, valid_to, ts),
        )
        self.conn.commit()
        return {"site_id": site_id, "institution_id": inst_id, "address": address,
                "valid_from": valid_from, "valid_to": valid_to}

    def register_category(self, code: str, name: str, level: int,
                          parent_code: str | None = None, active: bool = True) -> dict[str, Any]:
        if parent_code is not None:
            _require(self.conn, "project_categories", parent_code, "父级项目分类", key_col="code")
        ts = now_iso()
        try:
            self.conn.execute(
                "INSERT INTO project_categories(code, name, parent_code, level, active, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (code, name, parent_code, level, 1 if active else 0, ts),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"项目分类已存在：{code}") from exc
        return {"code": code, "name": name, "parent_code": parent_code, "level": level,
                "active": active}

    # ---------- 资质版本登记 ----------

    def register_institution_license(self, inst_id: str, license_no: str,
                                     scope_categories: list[str], level: int,
                                     valid_from: str, valid_to: str,
                                     issued_at: str | None = None) -> dict[str, Any]:
        _require(self.conn, "institutions", inst_id, "机构")
        result = self._new_version(
            "institution_license_versions", "institution_id", inst_id,
            ("license_no", "scope_categories", "level", "valid_from", "valid_to", "issued_at"),
            (license_no, dumps(scope_categories), level, valid_from, valid_to, issued_at or valid_from),
            ("license_no", "scope_categories", "level", "valid_from", "valid_to", "version"),
        )
        affected = self._open_cases_for_institution(inst_id)
        for case_id in affected:
            self.add_evaluation(case_id, trigger="许可续期")
        result["reevaluated_cases"] = affected
        return result

    def register_certificate(self, person_id: str, cert_no: str, practice_scope: list[str],
                             valid_from: str, valid_to: str,
                             issued_at: str | None = None) -> dict[str, Any]:
        _require(self.conn, "persons", person_id, "人员")
        result = self._new_version(
            "person_certificate_versions", "person_id", person_id,
            ("cert_no", "practice_scope", "valid_from", "valid_to", "issued_at"),
            (cert_no, dumps(practice_scope), valid_from, valid_to, issued_at or valid_from),
            ("cert_no", "practice_scope", "valid_from", "valid_to", "version"),
        )
        affected = self._open_cases_for_person(person_id)
        for case_id in affected:
            self.add_evaluation(case_id, trigger="证书续期")
        result["reevaluated_cases"] = affected
        return result

    def register_attending(self, person_id: str, title: str, scope_categories: list[str],
                           level: int, valid_from: str, valid_to: str,
                           issued_at: str | None = None) -> dict[str, Any]:
        _require(self.conn, "persons", person_id, "人员")
        return self._new_version(
            "attending_qualification_versions", "person_id", person_id,
            ("title", "scope_categories", "level", "valid_from", "valid_to", "issued_at"),
            (title, dumps(scope_categories), level,
             valid_from, valid_to, issued_at or valid_from),
            ("title", "scope_categories", "level", "valid_from", "valid_to", "version"),
            immutable_status=True,
        )

    def _new_version(self, table: str, owner_col: str, owner_id: str,
                     columns: tuple[str, ...], values: tuple[Any, ...],
                     result_cols: tuple[str, ...], *, immutable_status: bool = False) -> dict[str, Any]:
        """追加资质版本：旧有效版本置“已换发”，新版本置“有效”。

        ``immutable_status`` 用于主诊资格：一个人可同时持有多科有效主诊资格，
        新登记不作废旧版本。
        """
        ts = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                f"SELECT COALESCE(MAX(version), 0) AS v FROM {table} WHERE {owner_col} = ?",
                (owner_id,),
            ).fetchone()
            version = row["v"] + 1
            if not immutable_status:
                self.conn.execute(
                    f"UPDATE {table} SET status = ?, superseded_at = ? "
                    f"WHERE {owner_col} = ? AND status = ?",
                    (C.LicenseStatus.SUPERSEDED, ts, owner_id, C.LicenseStatus.EFFECTIVE),
                )
            col_placeholders = ", ".join(["?"] * len(columns))
            self.conn.execute(
                f"INSERT INTO {table}({owner_col}, version, status, {', '.join(columns)})"
                f" VALUES(?, ?, ?, {col_placeholders})",
                (owner_id, version, C.CertificateStatus.EFFECTIVE, *values),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        new = self.conn.execute(
            f"SELECT {', '.join(result_cols)} FROM {table} WHERE {owner_col} = ? AND version = ?",
            (owner_id, version),
        ).fetchone()
        result = dict(new)
        for json_col in ("scope_categories", "practice_scope"):
            if json_col in result:
                result[json_col] = loads(result[json_col])
        return result

    # ---------- 人员时态事件 ----------

    def suspend_certificate(self, person_id: str, effective_from: str,
                            reason: str | None = None) -> dict[str, Any]:
        cert = rules.effective_cert(self.conn, person_id, effective_from)
        if cert is None:
            raise NotFoundError("该时点没有可暂停的有效执业证")
        if rules.active_suspension(self.conn, cert["id"], effective_from) is not None:
            raise ConflictError("执业证已处于暂停状态，不能重复暂停")
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO person_cert_events(cert_version_id, event, effective_from, effective_to,"
            " reason, created_at) VALUES(?,?,?,?,?,?)",
            (cert["id"], C.CertificateEvent.SUSPEND, effective_from, None, reason, ts),
        )
        # 暂停不改变既有决定，仅追加对相关在办案件的评估留痕
        affected = self._open_cases_for_person(person_id)
        self.conn.commit()
        for case_id in affected:
            self.add_evaluation(case_id, trigger="证书暂停")
        return {"cert_version_id": cert["id"], "event": C.CertificateEvent.SUSPEND,
                "effective_from": effective_from, "reason": reason,
                "reevaluated_cases": affected}

    def resume_certificate(self, person_id: str, effective_from: str,
                           reason: str | None = None) -> dict[str, Any]:
        cert = rules.effective_cert(self.conn, person_id, effective_from)
        if cert is None:
            raise NotFoundError("该时点没有可恢复的有效执业证")
        susp = self._latest_open_suspension(cert["id"])
        if susp is None:
            raise ConflictError("执业证当前未暂停")
        self.conn.execute(
            "UPDATE person_cert_events SET effective_to = ? WHERE id = ?",
            (effective_from, susp["id"]),
        )
        affected = self._open_cases_for_person(person_id)
        self.conn.commit()
        for case_id in affected:
            self.add_evaluation(case_id, trigger="证书恢复")
        return {"cert_version_id": cert["id"], "event": "恢复",
                "effective_from": effective_from, "reevaluated_cases": affected}

    def _latest_open_suspension(self, cert_version_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM person_cert_events WHERE cert_version_id = ? AND event = '暂停'"
            " AND effective_to IS NULL ORDER BY effective_from DESC LIMIT 1",
            (cert_version_id,),
        ).fetchone()

    def file_cross_institution(self, person_id: str, institution_id: str,
                               valid_from: str, valid_to: str | None = None,
                               registration_id: str | None = None) -> dict[str, Any]:
        _require(self.conn, "persons", person_id, "人员")
        _require(self.conn, "institutions", institution_id, "机构")
        reg_id = registration_id or _uid("REG")
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO person_registrations(id, person_id, institution_id, kind, status,"
            " valid_from, valid_to, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (reg_id, person_id, institution_id, C.RegistrationKind.FILING,
             C.RegistrationStatus.EFFECTIVE, valid_from, valid_to, ts),
        )
        affected = self._open_cases_for_person(person_id)
        self.conn.commit()
        for case_id in affected:
            self.add_evaluation(case_id, trigger="跨机构备案")
        return {"registration_id": reg_id, "person_id": person_id,
                "institution_id": institution_id, "kind": C.RegistrationKind.FILING,
                "valid_from": valid_from, "valid_to": valid_to,
                "reevaluated_cases": affected}

    def register_primary_registration(self, person_id: str, institution_id: str,
                                      valid_from: str, valid_to: str | None = None) -> dict[str, Any]:
        _require(self.conn, "persons", person_id, "人员")
        _require(self.conn, "institutions", institution_id, "机构")
        reg_id = _uid("REG")
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO person_registrations(id, person_id, institution_id, kind, status,"
            " valid_from, valid_to, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (reg_id, person_id, institution_id, C.RegistrationKind.PRIMARY,
             C.RegistrationStatus.EFFECTIVE, valid_from, valid_to, ts),
        )
        self.conn.commit()
        return {"registration_id": reg_id, "person_id": person_id,
                "institution_id": institution_id, "kind": C.RegistrationKind.PRIMARY,
                "valid_from": valid_from, "valid_to": valid_to}

    # ---------- 案件与证据 ----------

    def create_case(self, case_id: str, institution_id: str, site_id: str,
                    person_id: str, project_code: str) -> dict[str, Any]:
        _require(self.conn, "institutions", institution_id, "机构")
        _require(self.conn, "institution_sites", site_id, "注册地点")
        _require(self.conn, "persons", person_id, "人员")
        _require(self.conn, "project_categories", project_code, "项目分类", key_col="code")
        ts = now_iso()
        try:
            self.conn.execute(
                "INSERT INTO cases(id, institution_id, site_id, person_id, project_code,"
                " status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (case_id, institution_id, site_id, person_id, project_code,
                 C.CaseStatus.PENDING, ts, ts),
            )
            self.conn.execute(
                "INSERT INTO case_event_log(case_id, event, detail_json, created_at)"
                " VALUES(?,?,?,?)",
                (case_id, "案件登记", dumps({"institution_id": institution_id, "site_id": site_id,
                 "person_id": person_id, "project_code": project_code}), ts),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"案件编号已存在：{case_id}") from exc
        evaluation = self.add_evaluation(case_id, trigger="登记评估")
        return self.get_case(case_id, evaluation_id=evaluation["id"])

    def add_evidence(self, case_id: str, filename: str, payload: dict[str, Any],
                     kind: str = C.EvidenceKind.APPLICATION,
                     submitted_at: str | None = None, evidence_id: str | None = None,
                     allow_decided: bool = False) -> dict[str, Any]:
        case = _require(self.conn, "cases", case_id, "案件")
        if not allow_decided and case["status"] in (C.CaseStatus.DECIDED, C.CaseStatus.ARCHIVED):
            raise ConflictError("已决定/归档案件的证据通过材料补交通道追加，原决定不变")
        eid = evidence_id or _uid("EV")
        ts = submitted_at or now_iso()
        sha = hashlib.sha256(dumps(payload).encode("utf-8")).hexdigest()
        self.conn.execute(
            "INSERT INTO case_evidence(id, case_id, kind, filename, sha256, payload_json, submitted_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (eid, case_id, kind, filename, sha, dumps(payload), ts),
        )
        self.conn.commit()
        return {"evidence_id": eid, "case_id": case_id, "kind": kind,
                "filename": filename, "sha256": sha, "submitted_at": ts}

    def supplement_evidence(self, case_id: str, filename: str, payload: dict[str, Any],
                            submitted_at: str | None = None) -> dict[str, Any]:
        """材料补交：证据追加为“补交材料”，原决定/原快照保留，另出一条新评估。"""
        case = _require(self.conn, "cases", case_id, "案件")
        evidence = self.add_evidence(case_id, filename, payload,
                                     kind=C.EvidenceKind.SUPPLEMENT, submitted_at=submitted_at,
                                     allow_decided=True)
        evaluation = self.add_evaluation(case_id, trigger="材料补交")
        self._log(case_id, "材料补交", {"evidence_id": evidence["evidence_id"],
                                        "new_evaluation_id": evaluation["id"],
                                        "prior_decisions_preserved": True})
        return {"evidence": evidence, "evaluation": evaluation,
                "notice": "原决定与历史评估未被修改，补交材料仅形成新评估供后续决定参考"}

    # ---------- 评估 ----------

    def add_evaluation(self, case_id: str, trigger: str, at: str | None = None) -> dict[str, Any]:
        """对案件执行一次完整双主体评估，结果快照 append-only。"""
        case = _require(self.conn, "cases", case_id, "案件")
        ts = at or now_iso()
        project = rules.get_category(self.conn, case["project_code"])
        inst_findings, inst_snapshot = rules.evaluate_institution(
            self.conn, case["institution_id"], case["site_id"], project, ts)
        person_findings, person_snapshot = rules.evaluate_person(
            self.conn, case["person_id"], case["institution_id"], project, ts)
        findings = inst_findings + person_findings
        denied = [f.as_dict() for f in findings if f.verdict == C.Verdict.DENY]
        overall = C.Verdict.ALLOW if not denied else C.Verdict.DENY
        evidence_rows = self.conn.execute(
            "SELECT id, kind, filename, sha256, submitted_at FROM case_evidence"
            " WHERE case_id = ? ORDER BY submitted_at, id", (case_id,),
        ).fetchall()
        result = {
            "case_id": case_id,
            "evaluated_at": ts,
            "trigger": trigger,
            "overall_verdict": overall,
            "institution_verdict": C.Verdict.ALLOW
            if all(f.verdict == C.Verdict.ALLOW for f in inst_findings) else C.Verdict.DENY,
            "person_verdict": C.Verdict.ALLOW
            if all(f.verdict == C.Verdict.ALLOW for f in person_findings) else C.Verdict.DENY,
            "findings": [f.as_dict() for f in findings],
            "denied_rules": [f["rule"] for f in denied],
            "evidence_bundle": [dict(r) for r in evidence_rows],
            "snapshots": {"institution": inst_snapshot, "person": person_snapshot},
        }
        cur = self.conn.execute(
            "INSERT INTO evaluations(case_id, trigger, evaluated_at, overall_verdict, result_json)"
            " VALUES(?,?,?,?,?)",
            (case_id, trigger, ts, overall, dumps(result)),
        )
        self.conn.execute(
            "UPDATE cases SET updated_at = ? WHERE id = ?", (ts, case_id),
        )
        self.conn.commit()
        result["id"] = cur.lastrowid
        return result

    def latest_evaluation(self, case_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT id, result_json FROM evaluations WHERE case_id = ?"
            " ORDER BY id DESC LIMIT 1", (case_id,),
        ).fetchone()
        if row is None:
            return None
        result = loads(row["result_json"])
        result["id"] = row["id"]
        return result

    def list_evaluations(self, case_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, trigger, evaluated_at, overall_verdict, result_json"
            " FROM evaluations WHERE case_id = ? ORDER BY id", (case_id,),
        ).fetchall()
        out = []
        for r in rows:
            item = {"id": r["id"], "trigger": r["trigger"],
                    "evaluated_at": r["evaluated_at"], "overall_verdict": r["overall_verdict"]}
            out.append(item)
        return out

    # ---------- 决定与授权 ----------

    def decide(self, case_id: str, result: str, decided_by: str, reason: str,
               idempotency_key: str, use_evaluation_id: int | None = None) -> dict[str, Any]:
        """写入决定；批准时在同一事务内原子写入授权，重复授予被拒绝。"""
        if result not in (C.DecisionResult.APPROVED, C.DecisionResult.REJECTED):
            raise ServiceError("决定结果必须是 批准/拒绝")
        case = _require(self.conn, "cases", case_id, "案件")

        # 幂等：同一 key 直接返回既有决定
        existing = self.conn.execute(
            "SELECT * FROM decisions WHERE idempotency_key = ?", (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return self._decision_view(existing, idempotent_replay=True)

        if case["status"] == C.CaseStatus.ARCHIVED:
            raise ConflictError("案件已归档，不得再作决定")
        active_grant = self.conn.execute(
            "SELECT id FROM grants WHERE case_id = ? AND status = '有效'", (case_id,),
        ).fetchone()
        if active_grant is not None:
            raise DuplicateGrantError("该案件已存在有效授权，防止重复授予；原决定保留")
        # 已有决定但无有效授权（曾被拒绝或授权已撤销）时，必须存在晚于既往决定
        # 所依据评估的新评估，才可再作决定；原决定记录原样保留。
        decided_eval = self.conn.execute(
            "SELECT MAX(evaluation_id) AS m FROM decisions WHERE case_id = ?", (case_id,),
        ).fetchone()["m"]
        newest_eval_id = self.conn.execute(
            "SELECT MAX(id) AS m FROM evaluations WHERE case_id = ?", (case_id,),
        ).fetchone()["m"]
        if decided_eval is not None and newest_eval_id <= decided_eval:
            raise ConflictError(
                "案件已有决定且尚无更新评估；请先补交材料/办理变更后复评（原决定保留）")

        eval_row = self.conn.execute(
            "SELECT * FROM evaluations WHERE case_id = ? AND id = ?",
            (case_id, use_evaluation_id),
        ).fetchone() if use_evaluation_id else self.conn.execute(
            "SELECT * FROM evaluations WHERE case_id = ? ORDER BY id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if eval_row is None:
            raise NotFoundError("案件尚无评估结果，不能作出决定")

        ts = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if result == C.DecisionResult.APPROVED:
                # 事务内复核：必须基于最新评估，且最新评估为“允许”
                latest = self.conn.execute(
                    "SELECT id, overall_verdict FROM evaluations WHERE case_id = ?"
                    " ORDER BY id DESC LIMIT 1", (case_id,),
                ).fetchone()
                if eval_row["id"] != latest["id"]:
                    raise ConflictError("存在更新的评估，请基于最新评估重新决定（原决定不受影响）")
                if latest["overall_verdict"] != C.Verdict.ALLOW:
                    raise ConflictError("最新核验结论为拒绝，不能批准；请查看拒绝规则与证据")
                already = self.conn.execute(
                    "SELECT id FROM grants WHERE case_id = ? AND status = '有效'",
                    (case_id,),
                ).fetchone()
                if already is not None:
                    raise DuplicateGrantError("该案件已存在有效授权，防止重复授予")

            cur = self.conn.execute(
                "INSERT INTO decisions(case_id, result, reason, evaluation_id, decided_by,"
                " decided_at, idempotency_key) VALUES(?,?,?,?,?,?,?)",
                (case_id, result, reason, eval_row["id"], decided_by, ts, idempotency_key),
            )
            decision_id = cur.lastrowid

            grant_view = None
            if result == C.DecisionResult.APPROVED:
                try:
                    gcur = self.conn.execute(
                        "INSERT INTO grants(case_id, institution_id, site_id, person_id,"
                        " project_code, status, granted_by, granted_at, decision_id)"
                        " SELECT ?, institution_id, site_id, person_id, project_code,"
                        " '有效', ?, ?, ? FROM cases WHERE id = ?",
                        (case_id, decided_by, ts, decision_id, case_id),
                    )
                except sqlite3.IntegrityError as exc:
                    raise DuplicateGrantError(
                        "同一机构-地点-人员-项目已存在有效授权，防止重复授予") from exc
                grant_view = self._grant_view(gcur.lastrowid)

            self.conn.execute(
                "UPDATE cases SET status = ?, updated_at = ? WHERE id = ?",
                (C.CaseStatus.DECIDED, ts, case_id),
            )
            self.conn.execute(
                "INSERT INTO case_event_log(case_id, event, detail_json, created_at)"
                " VALUES(?,?,?,?)",
                (case_id, f"作出{result}决定",
                 dumps({"decision_id": decision_id, "evaluation_id": eval_row["id"],
                        "reason": reason, "decided_by": decided_by}), ts),
            )
            self.conn.commit()
        except DuplicateGrantError:
            self.conn.rollback()
            raise
        except ConflictError:
            self.conn.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise DuplicateGrantError(f"决定写入冲突，可能为重复授予：{exc}") from exc
        except Exception:
            self.conn.rollback()
            raise

        return {"decision_id": decision_id, "case_id": case_id, "result": result,
                "reason": reason, "evaluation_id": eval_row["id"], "decided_by": decided_by,
                "decided_at": ts, "idempotency_key": idempotency_key,
                "evaluation": loads(eval_row["result_json"]),
                "grant": grant_view}

    def revoke_grant(self, case_id: str, revoked_by: str, reason: str) -> dict[str, Any]:
        _require(self.conn, "cases", case_id, "案件")
        ts = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT id FROM grants WHERE case_id = ? AND status = '有效'", (case_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("该案件没有可撤销的有效授权")
            self.conn.execute("UPDATE grants SET status = '撤销' WHERE id = ?", (row["id"],))
            self.conn.execute(
                "INSERT INTO case_event_log(case_id, event, detail_json, created_at)"
                " VALUES(?,?,?,?)",
                (case_id, "撤销授权", dumps({"grant_id": row["id"], "reason": reason,
                 "revoked_by": revoked_by}), ts),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"grant_id": row["id"], "status": C.GrantStatus.REVOKED, "reason": reason}

    def archive_case(self, case_id: str) -> dict[str, Any]:
        case = _require(self.conn, "cases", case_id, "案件")
        if case["status"] != C.CaseStatus.DECIDED:
            raise ConflictError("仅已决定案件可以归档")
        ts = now_iso()
        self.conn.execute("UPDATE cases SET status = ?, updated_at = ? WHERE id = ?",
                          (C.CaseStatus.ARCHIVED, ts, case_id))
        self.conn.commit()
        return {"case_id": case_id, "status": C.CaseStatus.ARCHIVED}

    # ---------- 查询 ----------

    def get_case(self, case_id: str, evaluation_id: int | None = None) -> dict[str, Any]:
        case = _require(self.conn, "cases", case_id, "案件")
        decisions = self.conn.execute(
            "SELECT * FROM decisions WHERE case_id = ? ORDER BY id", (case_id,),
        ).fetchall()
        grants = self.conn.execute(
            "SELECT * FROM grants WHERE case_id = ? ORDER BY id", (case_id,),
        ).fetchall()
        view = {
            "case": dict(case),
            "latest_evaluation": self.latest_evaluation(case_id),
            "evaluation_history": self.list_evaluations(case_id),
            "decisions": [
                {"id": d["id"], "result": d["result"], "reason": d["reason"],
                 "evaluation_id": d["evaluation_id"], "decided_by": d["decided_by"],
                 "decided_at": d["decided_at"]}
                for d in decisions
            ],
            "grants": [self._grant_view(g["id"]) for g in grants],
            "event_log": [dict(r) for r in self.conn.execute(
                "SELECT id, event, detail_json, created_at FROM case_event_log"
                " WHERE case_id = ? ORDER BY id", (case_id,))],
        }
        return view

    def get_evaluation(self, case_id: str, evaluation_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT id, result_json FROM evaluations WHERE case_id = ? AND id = ?",
            (case_id, evaluation_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"评估不存在：案件 {case_id} 评估 {evaluation_id}")
        result = loads(row["result_json"])
        result["id"] = row["id"]
        return result

    def institution_scope(self, institution_id: str, at: str | None = None) -> dict[str, Any]:
        _require(self.conn, "institutions", institution_id, "机构")
        return rules.institution_scope(self.conn, institution_id, at or now_iso())

    def person_scope(self, person_id: str, at: str | None = None) -> dict[str, Any]:
        _require(self.conn, "persons", person_id, "人员")
        return rules.person_scope(self.conn, person_id, at or now_iso())

    # ---------- 内部工具 ----------

    def _open_cases_for_person(self, person_id: str) -> list[str]:
        """受人员时态事件影响、尚未终结的案件（用于追加评估）。"""
        return [r["id"] for r in self.conn.execute(
            "SELECT id FROM cases WHERE person_id = ? AND status IN ('登记','待核验','处置中')"
            " ORDER BY id", (person_id,))]

    def _open_cases_for_institution(self, institution_id: str) -> list[str]:
        return [r["id"] for r in self.conn.execute(
            "SELECT id FROM cases WHERE institution_id = ? AND status IN ('登记','待核验','处置中')"
            " ORDER BY id", (institution_id,))]

    def _log(self, case_id: str, event: str, detail: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO case_event_log(case_id, event, detail_json, created_at)"
            " VALUES(?,?,?,?)", (case_id, event, dumps(detail), now_iso()))
        self.conn.commit()

    def _decision_view(self, row: sqlite3.Row, idempotent_replay: bool = False) -> dict[str, Any]:
        eval_row = self.conn.execute(
            "SELECT result_json FROM evaluations WHERE id = ?", (row["evaluation_id"],),
        ).fetchone()
        view = {"decision_id": row["id"], "case_id": row["case_id"], "result": row["result"],
                "reason": row["reason"], "evaluation_id": row["evaluation_id"],
                "decided_by": row["decided_by"], "decided_at": row["decided_at"],
                "idempotency_key": row["idempotency_key"],
                "evaluation": loads(eval_row["result_json"])}
        if idempotent_replay:
            view["idempotent_replay"] = True
        grant = self.conn.execute("SELECT id FROM grants WHERE decision_id = ?", (row["id"],)).fetchone()
        if grant is not None:
            view["grant"] = self._grant_view(grant["id"])
        return view

    def _grant_view(self, grant_id: int) -> dict[str, Any]:
        g = self.conn.execute("SELECT * FROM grants WHERE id = ?", (grant_id,)).fetchone()
        return {"grant_id": g["id"], "case_id": g["case_id"],
                "institution_id": g["institution_id"], "site_id": g["site_id"],
                "person_id": g["person_id"], "project_code": g["project_code"],
                "status": g["status"], "granted_by": g["granted_by"],
                "granted_at": g["granted_at"], "decision_id": g["decision_id"]}
