"""核验规则引擎。

把"执业证、主诊资格、注册机构、项目等级"拆成可解释的规则序列，
每条规则独立给出通过/拒绝、原因文字和所引用的登记证据。
综合结论区分三种情形：材料缺失 → 补交材料；范围不符 → 拒绝；全部满足 → 允许。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from .models import (
    CASE_PENDING,
    DECISION_ALLOW,
    DECISION_DENY,
    DECISION_PENDING_MATERIAL,
    GRADE_RANK,
    CaseEvaluation,
    RuleResult,
)
from .scopes import (
    _filing_at,
    _grade_covers,
    _latest_version,
    _parse_scope,
    _personnel_base,
    _registration_at,
    compute_institution_scope,
    compute_personnel_scope,
)

# 申请案件必备的材料类型（缺一则进入补交流程，而非直接拒绝）
REQUIRED_MATERIALS = (
    ("机构资质凭证", "机构资质版本的凭证材料"),
    ("执业证凭证", "人员执业证的凭证材料"),
    ("主诊资格凭证", "主诊资格的证明材料"),
    ("注册或备案凭证", "执业注册地点或跨机构备案的凭证材料"),
)


def _evidence_ref(row: sqlite3.Row | None, kind: str) -> dict:
    if row is None:
        return {"kind": kind, "found": False}
    keys = row.keys()
    payload = {key: row[key] for key in keys if key not in ("scope_json",)}
    if "scope_json" in keys:
        payload["scope"] = _parse_scope(row["scope_json"])
    payload["kind"] = kind
    payload["found"] = True
    return payload


def evaluate_case(conn: sqlite3.Connection, case_row: sqlite3.Row, as_of: str) -> CaseEvaluation:
    """对案件执行全部规则并给出综合结论。纯读操作，不写库。"""
    case_id = case_row["id"]
    institution_id = case_row["institution_id"]
    personnel_id = case_row["personnel_id"]
    category_code = case_row["category_code"]
    grade = case_row["grade"]

    category = conn.execute(
        "SELECT code, name, max_grade FROM project_categories WHERE code = ?",
        (category_code,),
    ).fetchone()

    institution = conn.execute("SELECT * FROM institutions WHERE id = ?", (institution_id,)).fetchone()
    personnel = conn.execute("SELECT * FROM personnel WHERE id = ?", (personnel_id,)).fetchone()

    # ---- 取全部时态事实 --------------------------------------------------
    inst_version = _latest_version(
        conn.execute(
            "SELECT * FROM institution_qual_versions WHERE institution_id = ? ORDER BY version_no",
            (institution_id,),
        ).fetchall(),
        as_of,
    )
    cert, attending = _personnel_base(conn, personnel_id, as_of)
    att = attending.get(category_code)
    registration = _registration_at(conn, personnel_id, institution_id, as_of)
    filing = None if registration is not None else _filing_at(
        conn, personnel_id, institution_id, category_code, as_of
    )

    materials = conn.execute(
        "SELECT material_type, file_ref, submitted_at FROM case_materials WHERE case_id = ? ORDER BY id",
        (case_id,),
    ).fetchall()
    submitted_types = {row["material_type"] for row in materials}

    inst_scope = compute_institution_scope(conn, institution_id, as_of)
    person_scope = compute_personnel_scope(conn, personnel_id, institution_id, as_of)
    inst_item = inst_scope.find(category_code, grade)
    person_item = person_scope.find(category_code, grade)

    cert_scope = _parse_scope(cert["scope_json"]) if cert is not None else []
    cert_grade = next(
        (entry["grade"] for entry in cert_scope if entry["category_code"] == category_code), None
    )
    inst_scope_list = _parse_scope(inst_version["scope_json"]) if inst_version is not None else []
    inst_granted_grade = next(
        (entry["grade"] for entry in inst_scope_list if entry["category_code"] == category_code), None
    )

    rules: list[RuleResult] = []

    def rule(code: str, title: str, passed: bool, detail: str, evidence: list[dict] | None = None) -> None:
        rules.append(RuleResult(code, title, passed, detail, tuple(evidence or [])))

    # R01 申请要素
    elements_ok = all([institution is not None, personnel is not None, category is not None, grade])
    rule(
        "R01", "申请要素完整", elements_ok,
        "机构、人员、项目分类与等级均已登记" if elements_ok
        else "申请缺少机构、人员、项目分类或等级要素",
        [{"kind": "申请", "case_id": case_id}],
    )

    # R02 项目分类在目录内
    rule(
        "R02", "项目分类在监管目录内", category is not None,
        f"「{category['name']}」属于登记的项目分类目录" if category
        else f"分类 {category_code} 不在项目分类目录中，任何主体均不得开展",
        [{"kind": "项目分类", "code": category_code, "found": category is not None}],
    )

    # R03 申请等级不超过目录上限
    if category is not None:
        within_catalog = GRADE_RANK[grade] <= GRADE_RANK[category["max_grade"]]
        rule(
            "R03", "申请等级不超过目录设定上限", within_catalog,
            f"申请{grade}，目录内该分类最高{category['max_grade']}"
            + ("，未超上限" if within_catalog else "，超出目录上限"),
            [{"kind": "项目分类", "code": category_code, "max_grade": category["max_grade"]}],
        )
    else:
        rule("R03", "申请等级不超过目录设定上限", False, "分类不存在，无法核定等级上限")

    # ---- 机构侧 R04-R08 --------------------------------------------------
    rule(
        "R04", "机构存在已生效资质版本", inst_version is not None,
        f"当前生效版本为 v{inst_version['version_no']}（{inst_version['valid_from']} 起）"
        if inst_version else f"截至 {as_of} 机构无已生效的资质版本",
        [_evidence_ref(inst_version, "机构资质版本")],
    )

    if inst_version is not None:
        in_date = inst_version["valid_to"] >= as_of
        rule(
            "R05", "机构资质在有效期内", in_date,
            f"资质有效期至 {inst_version['valid_to']}"
            + ("，尚未届满" if in_date else "，已届满，需先完成续期登记"),
            [_evidence_ref(inst_version, "机构资质版本")],
        )
        status_ok = inst_version["status"] == "有效"
        rule(
            "R06", "机构资质状态为有效", status_ok,
            "资质状态为有效" if status_ok
            else f"资质状态为{inst_version['status']}，暂停期间不得新增项目授权",
            [_evidence_ref(inst_version, "机构资质版本")],
        )
        covers_cat = inst_granted_grade is not None
        rule(
            "R07", "机构资质范围包含申请分类", covers_cat,
            f"机构资质范围包含「{category['name']}」（等级 {inst_granted_grade}）" if covers_cat
            else (f"机构资质证载范围未包含「{category['name'] if category else category_code}」"
                  "——证书本身有效不代表范围覆盖，机构侧不得开展"),
            [_evidence_ref(inst_version, "机构资质版本")],
        )
        if covers_cat:
            covers_grade = _grade_covers(inst_granted_grade, grade)
            rule(
                "R08", "机构资质等级覆盖申请等级", covers_grade,
                f"机构资质 {inst_granted_grade} 覆盖申请{grade}" if covers_grade
                else f"机构资质在该分类仅到 {inst_granted_grade}，申请{grade}超出等级范围",
                [_evidence_ref(inst_version, "机构资质版本")],
            )
        else:
            rule("R08", "机构资质等级覆盖申请等级", False, "分类未被机构资质包含，等级无从覆盖")
    else:
        for code, title in (("R05", "机构资质在有效期内"), ("R06", "机构资质状态为有效"),
                            ("R07", "机构资质范围包含申请分类"), ("R08", "机构资质等级覆盖申请等级")):
            rule(code, title, False, "无已生效资质版本")

    # ---- 人员侧 R09-R12 --------------------------------------------------
    rule(
        "R09", "人员执业证时态有效",
        cert is not None and cert["valid_to"] >= as_of and cert["status"] == "有效",
        (f"执业证 v{cert['version_no']}（{cert['license_no']}）状态{cert['status']}，"
         f"有效期至 {cert['valid_to']}") if cert else f"截至 {as_of} 无已生效执业证版本",
        [_evidence_ref(cert, "执业证版本")],
    )

    cert_covers = cert is not None and cert_grade is not None and _grade_covers(cert_grade, grade)
    rule(
        "R10", "执业证载范围与等级覆盖申请项目", bool(cert_covers),
        (f"执业证载范围包含「{category['name'] if category else category_code}」"
         f"且等级 {cert_grade} 覆盖{grade}") if cert_covers
        else ("执业证虽有效，但证载范围"
              + ("未包含申请分类" if cert_grade is None
                 else f"仅到 {cert_grade}，不覆盖{grade}")
              + "——不得因证书有效而误判为可开展"),
        [_evidence_ref(cert, "执业证版本")],
    )

    att_ok = att is not None and att["valid_to"] >= as_of and att["status"] == "有效" \
        and _grade_covers(att["max_grade"], grade)
    rule(
        "R11", "主诊资格有效且等级覆盖", bool(att_ok),
        (f"主诊资格 v{att['version_no']} 状态{att['status']}，最高{att['max_grade']}，"
         f"有效期至 {att['valid_to']}，覆盖{grade}") if att_ok
        else ("无该分类主诊资格" if att is None
              else (f"主诊资格已过期（至 {att['valid_to']}）" if att["valid_to"] < as_of
                    else (f"主诊资格状态为{att['status']}" if att["status"] != "有效"
                          else f"主诊资格最高{att['max_grade']}，不覆盖{grade}"))),
        [_evidence_ref(att, "主诊资格版本")],
    )

    # R12 注册机构 / 跨机构备案
    if registration is not None:
        reg_ok = True
        rule(
            "R12", "注册机构或跨机构备案有效", True,
            f"人员有效注册于本机构（注册地点生效日 {registration['valid_from']}）",
            [_evidence_ref(registration, "注册地点")],
        )
    elif filing is not None:
        filing_ok = filing["valid_to"] is None or filing["valid_to"] >= as_of
        filing_grade_ok = _grade_covers(filing["max_grade"], grade)
        rule(
            "R12", "注册机构或跨机构备案有效", filing_ok and filing_grade_ok,
            (f"持跨机构备案 #{filing['id']} 在本机构执业，备案最高{filing['max_grade']}"
             + ("，覆盖申请等级" if filing_grade_ok else f"，不覆盖{grade}"))
            if filing_ok else f"跨机构备案 #{filing['id']} 已过有效期",
            [_evidence_ref(filing, "跨机构备案")],
        )
    else:
        rule(
            "R12", "注册机构或跨机构备案有效", False,
            "申请机构既不是人员的有效注册主执业机构，也无针对该分类的有效跨机构备案",
            [{"kind": "注册地点", "found": False}, {"kind": "跨机构备案", "found": False}],
        )

    # R13 双主体范围交集
    pair_allowed = (
        inst_item is not None and person_item is not None
        and inst_item.allowed and person_item.allowed
    )
    blocker: list[str] = []
    if inst_item is not None and not inst_item.allowed:
        blocker.extend(f"机构侧：{reason}" for reason in inst_item.reasons)
    if person_item is not None and not person_item.allowed:
        blocker.extend(f"人员侧：{reason}" for reason in person_item.reasons)
    rule(
        "R13", "双主体可执业范围交集包含申请项目", pair_allowed,
        "机构与人员可执业范围在申请项目上相交，双方均覆盖" if pair_allowed
        else "双主体范围交集不包含申请项目：" + "；".join(blocker),
        [
            {"kind": "机构范围明细", "item": None if inst_item is None else {
                "allowed": inst_item.allowed, "reasons": list(inst_item.reasons)}},
            {"kind": "人员范围明细", "item": None if person_item is None else {
                "allowed": person_item.allowed, "reasons": list(person_item.reasons)}},
        ],
    )

    # ---- 材料完整性（决定补交 vs 拒绝） ----------------------------------
    missing = [label for key, label in REQUIRED_MATERIALS if key not in submitted_types]
    rule(
        "R14", "申请材料齐备", not missing,
        "必备材料均已提交" if not missing else "缺少材料：" + "、".join(missing),
        [{"kind": "申请材料", "submitted": sorted(submitted_types), "missing": missing}],
    )

    failed = [item for item in rules if not item.passed]
    if missing:
        decision = DECISION_PENDING_MATERIAL
        summary = "材料不齐，需补交：" + "、".join(missing)
    elif failed:
        decision = DECISION_DENY
        summary = "核验不通过：" + "；".join(f"{r.rule_code} {r.title}" for r in failed)
    else:
        decision = DECISION_ALLOW
        summary = f"机构与人员在「{category['name']}」{grade}项目上均具备有效资质与范围，允许开展"

    return CaseEvaluation(case_id, as_of, decision, summary, tuple(rules), inst_scope, person_scope)


def build_snapshot(conn: sqlite3.Connection, case_row: sqlite3.Row, as_of: str) -> dict:
    """收集本次核验所依赖的登记事实，作为决定证据快照存档。"""
    institution_id = case_row["institution_id"]
    personnel_id = case_row["personnel_id"]
    category_code = case_row["category_code"]

    inst_version = _latest_version(
        conn.execute(
            "SELECT * FROM institution_qual_versions WHERE institution_id = ? ORDER BY version_no",
            (institution_id,),
        ).fetchall(),
        as_of,
    )
    cert, attending = _personnel_base(conn, personnel_id, as_of)
    registration = _registration_at(conn, personnel_id, institution_id, as_of)
    filing = None if registration is not None else _filing_at(
        conn, personnel_id, institution_id, category_code, as_of
    )
    materials = conn.execute(
        "SELECT material_type, file_ref, submitted_at FROM case_materials WHERE case_id = ?",
        (case_row["id"],),
    ).fetchall()

    def slim(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        if "scope_json" in data:
            data["scope"] = json.loads(data.pop("scope_json"))
        return data

    return {
        "as_of": as_of,
        "case": dict(case_row),
        "institution_qual_version": slim(inst_version),
        "personnel_cert_version": slim(cert),
        "attending_qual_version": slim(attending.get(category_code)),
        "registration": slim(registration),
        "cross_filing": slim(filing),
        "materials": [dict(row) for row in materials],
    }
