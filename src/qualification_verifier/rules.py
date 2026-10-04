"""核验规则引擎。

机构侧（I 系列）与人员侧（P 系列）规则**分别独立计算**，每条规则都给出
``允许/拒绝`` 结论、理由文字以及支撑该结论的证据摘录。总判定为所有规则
的合取：任何一条拒绝（典型情形：证书在有效期内但执业范围不覆盖项目）即
整体拒绝，避免“证书有效即视为可开展”的误判。

所有时态判断以评估时点 ``at`` 为准，决定落库时保存完整快照，之后资质
续期、暂停等变化不会改变历史结论。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import constants as C
from .storage import loads


@dataclass
class Finding:
    rule: str
    title: str
    subject: str  # 机构 / 人员
    verdict: str
    reason: str
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "title": self.title,
            "subject": self.subject,
            "verdict": self.verdict,
            "reason": self.reason,
            "evidence": self.evidence,
        }


def _allow(rule: str, subject: str, reason: str, evidence: list[dict[str, Any]]) -> Finding:
    return Finding(rule, C.RULE_TITLES[rule], subject, C.Verdict.ALLOW, reason, evidence)


def _deny(rule: str, subject: str, reason: str, evidence: list[dict[str, Any]] | None = None) -> Finding:
    return Finding(rule, C.RULE_TITLES[rule], subject, C.Verdict.DENY, reason, evidence or [])


def _ref(kind: str, row: sqlite3.Row | None, excerpt: dict[str, Any]) -> dict[str, Any]:
    record_id = None
    if row is not None:
        for key in ("id", "code"):
            if key in row.keys():
                record_id = row[key]
                break
    return {"kind": kind, "record_id": record_id, "excerpt": excerpt}


# ---------- 基础查询 ----------

def get_category(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM project_categories WHERE code = ?", (code,)).fetchone()


def category_chain(conn: sqlite3.Connection, code: str) -> list[str]:
    """项目代码自底向上的祖先链（含自身）。"""
    chain: list[str] = []
    current: str | None = code
    seen: set[str] = set()
    while current and current not in seen:
        row = conn.execute(
            "SELECT code, parent_code FROM project_categories WHERE code = ?",
            (current,),
        ).fetchone()
        if row is None:
            break
        seen.add(current)
        chain.append(row["code"])
        current = row["parent_code"]
    return chain


def scope_covers(conn: sqlite3.Connection, scope_codes: list[str], project_code: str) -> tuple[bool, str | None]:
    """判断授权范围代码列表是否覆盖某项目（沿分类树向上匹配父级科目）。

    返回 (是否覆盖, 命中的范围代码)。
    """
    scope = set(scope_codes)
    for ancestor in category_chain(conn, project_code):
        if ancestor in scope:
            return True, ancestor
    return False, None


def descendant_codes(conn: sqlite3.Connection, root_codes: list[str]) -> set[str]:
    """展开分类代码到全部活跃后代（含自身）。"""
    rows = conn.execute("SELECT code, parent_code, active FROM project_categories").fetchall()
    children: dict[str | None, list[str]] = {}
    active: set[str] = set()
    for r in rows:
        children.setdefault(r["parent_code"], []).append(r["code"])
        if r["active"]:
            active.add(r["code"])
    result: set[str] = set()
    stack = list(root_codes)
    while stack:
        code = stack.pop()
        if code in result:
            continue
        if code in active:
            result.add(code)
        stack.extend(children.get(code, []))
    return result


def effective_license(conn: sqlite3.Connection, institution_id: str, at: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM institution_license_versions
        WHERE institution_id = ? AND status = '有效'
          AND valid_from <= ? AND valid_to >= ?
        ORDER BY version DESC LIMIT 1
        """,
        (institution_id, at, at),
    ).fetchone()


def effective_cert(conn: sqlite3.Connection, person_id: str, at: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM person_certificate_versions
        WHERE person_id = ? AND status = '有效'
          AND valid_from <= ? AND valid_to >= ?
        ORDER BY version DESC LIMIT 1
        """,
        (person_id, at, at),
    ).fetchone()


def active_suspension(conn: sqlite3.Connection, cert_version_id: int, at: str) -> sqlite3.Row | None:
    """评估时点处于持续状态的暂停事件。"""
    return conn.execute(
        """
        SELECT * FROM person_cert_events
        WHERE cert_version_id = ? AND event = '暂停'
          AND effective_from <= ? AND (effective_to IS NULL OR effective_to > ?)
        ORDER BY effective_from DESC LIMIT 1
        """,
        (cert_version_id, at, at),
    ).fetchone()


def effective_attendings(conn: sqlite3.Connection, person_id: str, at: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT * FROM attending_qualification_versions
        WHERE person_id = ? AND status = '有效'
          AND valid_from <= ? AND valid_to >= ?
        ORDER BY version DESC
        """,
        (person_id, at, at),
    ))


def effective_registrations(conn: sqlite3.Connection, person_id: str, at: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT * FROM person_registrations
        WHERE person_id = ? AND status = '有效'
          AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)
        ORDER BY kind, valid_from
        """,
        (person_id, at, at),
    ))


def effective_site(conn: sqlite3.Connection, site_id: str, at: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM institution_sites
        WHERE id = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)
        """,
        (site_id, at, at),
    ).fetchone()


# ---------- 规则评估 ----------

def evaluate_institution(
    conn: sqlite3.Connection,
    institution_id: str,
    site_id: str,
    project: sqlite3.Row,
    at: str,
) -> tuple[list[Finding], dict[str, Any]]:
    """机构侧四条规则。返回（结论列表，原始快照）。"""
    findings: list[Finding] = []
    license_row = effective_license(conn, institution_id, at)
    site_row = effective_site(conn, site_id, at)
    site_belongs = (
        site_row is not None and site_row["institution_id"] == institution_id
    )

    # I-001 许可在评估时点有效（含时态，不依赖证书人员侧）
    if license_row is not None:
        findings.append(_allow(
            C.RULE_INST_LICENSE_EFFECTIVE, C.SubjectType.INSTITUTION,
            f"机构执业许可 {license_row['license_no']} 第{license_row['version']}版在有效期内"
            f"（{license_row['valid_from']} 至 {license_row['valid_to']}）",
            [_ref("机构许可版本", license_row, {
                "license_no": license_row["license_no"], "version": license_row["version"],
                "status": license_row["status"], "valid_from": license_row["valid_from"],
                "valid_to": license_row["valid_to"],
            })],
        ))
    else:
        latest = conn.execute(
            "SELECT * FROM institution_license_versions WHERE institution_id = ? ORDER BY version DESC LIMIT 1",
            (institution_id,),
        ).fetchone()
        findings.append(_deny(
            C.RULE_INST_LICENSE_EFFECTIVE, C.SubjectType.INSTITUTION,
            "评估时点不存在有效的机构执业许可（未登记、已过期或已注销）",
            [_ref("最近许可版本", latest, _row_or_none(latest))],
        ))

    # I-002 地点已登记且属于该机构、在有效期内
    if site_row is not None and site_belongs:
        findings.append(_allow(
            C.RULE_INST_SITE_REGISTERED, C.SubjectType.INSTITUTION,
            f"执业地点 {site_row['address']} 已登记于该机构",
            [_ref("机构注册地点", site_row, {
                "site_id": site_row["id"], "address": site_row["address"],
                "institution_id": site_row["institution_id"],
            })],
        ))
    else:
        findings.append(_deny(
            C.RULE_INST_SITE_REGISTERED, C.SubjectType.INSTITUTION,
            "申请执业地点未在该机构名下有效登记（地点不存在、已失效或属于其他机构）",
            [_ref("机构注册地点", site_row, _row_or_none(site_row))],
        ))

    # I-003 许可科目覆盖项目 —— 证书有效但范围不符的关键拦截点
    if license_row is not None:
        scope = loads(license_row["scope_categories"])
        covered, hit = scope_covers(conn, scope, project["code"])
        ev = [_ref("机构许可版本", license_row, {
            "license_no": license_row["license_no"], "scope_categories": scope,
        })]
        if covered:
            findings.append(_allow(
                C.RULE_INST_SCOPE_COVERS, C.SubjectType.INSTITUTION,
                f"许可科目 {hit} 覆盖申请项目 {project['code']}（{project['name']}）", ev,
            ))
        else:
            findings.append(_deny(
                C.RULE_INST_SCOPE_COVERS, C.SubjectType.INSTITUTION,
                f"许可科目 {sorted(scope)} 不覆盖项目 {project['code']}（{project['name']}），"
                "许可有效不等于可开展该项目", ev,
            ))

        # I-004 资质等级满足项目等级（等级数字越小越高）
        ev = [_ref("机构许可版本", license_row, {"level": license_row["level"]}),
              _ref("项目分类", project, {"code": project["code"], "level": project["level"]})]
        if license_row["level"] <= project["level"]:
            findings.append(_allow(
                C.RULE_INST_LEVEL_SUFFICIENT, C.SubjectType.INSTITUTION,
                f"机构资质等级 {license_row['level']} 满足项目等级 {project['level']}", ev,
            ))
        else:
            findings.append(_deny(
                C.RULE_INST_LEVEL_SUFFICIENT, C.SubjectType.INSTITUTION,
                f"机构资质等级 {license_row['level']} 低于项目要求等级 {project['level']}", ev,
            ))
    else:
        findings.append(_deny(C.RULE_INST_SCOPE_COVERS, C.SubjectType.INSTITUTION,
                              "无有效机构许可，无法核验科目范围"))
        findings.append(_deny(C.RULE_INST_LEVEL_SUFFICIENT, C.SubjectType.INSTITUTION,
                              "无有效机构许可，无法核验资质等级"))

    snapshot = {
        "license": dict(license_row) if license_row else None,
        "site": dict(site_row) if site_row else None,
        "site_belongs_to_institution": site_belongs,
        "project": dict(project),
    }
    return findings, snapshot


def evaluate_person(
    conn: sqlite3.Connection,
    person_id: str,
    institution_id: str,
    project: sqlite3.Row,
    at: str,
) -> tuple[list[Finding], dict[str, Any]]:
    """人员侧五条规则。"""
    findings: list[Finding] = []
    cert = effective_cert(conn, person_id, at)
    suspension = active_suspension(conn, cert["id"], at) if cert else None
    attendings = effective_attendings(conn, person_id, at)
    registrations = effective_registrations(conn, person_id, at)

    # P-001 执业证有效：版本在有效期内，且评估时点没有未解除的暂停
    if cert is None:
        latest = conn.execute(
            "SELECT * FROM person_certificate_versions WHERE person_id = ? ORDER BY version DESC LIMIT 1",
            (person_id,),
        ).fetchone()
        findings.append(_deny(
            C.RULE_PERSON_CERT_EFFECTIVE, C.SubjectType.PERSON,
            "评估时点不存在有效的人员执业证（未登记、已过期或已注销）",
            [_ref("最近证书版本", latest, _row_or_none(latest))],
        ))
    elif suspension is not None:
        findings.append(_deny(
            C.RULE_PERSON_CERT_EFFECTIVE, C.SubjectType.PERSON,
            f"执业证 {cert['cert_no']} 自 {suspension['effective_from']} 起处于暂停状态"
            + (f"：{suspension['reason']}" if suspension["reason"] else ""),
            [_ref("人员证书版本", cert, {
                "cert_no": cert["cert_no"], "version": cert["version"],
                "valid_from": cert["valid_from"], "valid_to": cert["valid_to"],
            }), _ref("暂停事件", suspension, {
                "event": suspension["event"], "effective_from": suspension["effective_from"],
                "effective_to": suspension["effective_to"], "reason": suspension["reason"],
            })],
        ))
    else:
        findings.append(_allow(
            C.RULE_PERSON_CERT_EFFECTIVE, C.SubjectType.PERSON,
            f"执业证 {cert['cert_no']} 第{cert['version']}版有效且未被暂停"
            f"（{cert['valid_from']} 至 {cert['valid_to']}）",
            [_ref("人员证书版本", cert, {
                "cert_no": cert["cert_no"], "version": cert["version"],
                "status": cert["status"], "valid_from": cert["valid_from"],
                "valid_to": cert["valid_to"],
            })],
        ))

    # P-002 执业证执业范围覆盖项目
    if cert is not None and suspension is None:
        scope = loads(cert["practice_scope"])
        covered, hit = scope_covers(conn, scope, project["code"])
        ev = [_ref("人员证书版本", cert, {"cert_no": cert["cert_no"], "practice_scope": scope})]
        if covered:
            findings.append(_allow(
                C.RULE_PERSON_CERT_SCOPE, C.SubjectType.PERSON,
                f"执业范围 {hit} 覆盖项目 {project['code']}（{project['name']}）", ev,
            ))
        else:
            findings.append(_deny(
                C.RULE_PERSON_CERT_SCOPE, C.SubjectType.PERSON,
                f"执业证有效，但执业范围 {sorted(scope)} 不覆盖项目 "
                f"{project['code']}（{project['name']}），不得开展", ev,
            ))
    else:
        findings.append(_deny(C.RULE_PERSON_CERT_SCOPE, C.SubjectType.PERSON,
                              "执业证当前不可用，无法核验执业范围"))

    # P-003 主诊资格存在且在有效期内
    if attendings:
        names = "、".join(a["title"] for a in attendings)
        findings.append(_allow(
            C.RULE_PERSON_ATTENDING_EXISTS, C.SubjectType.PERSON,
            f"登记有有效主诊资格：{names}",
            [_ref("主诊资格版本", a, {
                "title": a["title"], "version": a["version"],
                "valid_from": a["valid_from"], "valid_to": a["valid_to"],
            }) for a in attendings],
        ))
    else:
        findings.append(_deny(
            C.RULE_PERSON_ATTENDING_EXISTS, C.SubjectType.PERSON,
            "未登记评估时点有效的主诊资格（缺失、过期或注销）",
        ))

    # P-004 主诊资格范围与等级满足项目
    matching = [
        a for a in attendings
        if scope_covers(conn, loads(a["scope_categories"]), project["code"])[0]
        and a["level"] <= project["level"]
    ]
    if matching:
        a = matching[0]
        findings.append(_allow(
            C.RULE_PERSON_ATTENDING_LEVEL, C.SubjectType.PERSON,
            f"主诊资格 {a['title']}（等级 {a['level']}）覆盖项目 {project['code']} "
            f"且满足项目等级 {project['level']}",
            [_ref("主诊资格版本", a, {
                "title": a["title"], "level": a["level"],
                "scope_categories": loads(a["scope_categories"]),
            }), _ref("项目分类", project, {"code": project["code"], "level": project["level"]})],
        ))
    elif attendings:
        findings.append(_deny(
            C.RULE_PERSON_ATTENDING_LEVEL, C.SubjectType.PERSON,
            "现有有效主诊资格的科目范围或等级不覆盖该项目"
            f"（项目 {project['code']}，等级 {project['level']}）",
            [_ref("主诊资格版本", a, {
                "title": a["title"], "level": a["level"],
                "scope_categories": loads(a["scope_categories"]),
            }) for a in attendings],
        ))
    else:
        findings.append(_deny(C.RULE_PERSON_ATTENDING_LEVEL, C.SubjectType.PERSON,
                              "无有效主诊资格，无法核验主诊范围与等级"))

    # P-005 注册机构匹配：主执业机构或有效跨机构备案均可
    at_inst = [r for r in registrations if r["institution_id"] == institution_id]
    if at_inst:
        r = at_inst[0]
        findings.append(_allow(
            C.RULE_PERSON_REGISTRATION, C.SubjectType.PERSON,
            f"人员以{r['kind']}关系注册于申请机构（有效期自 {r['valid_from']} 起）",
            [_ref("人员注册记录", r, {
                "kind": r["kind"], "institution_id": r["institution_id"],
                "valid_from": r["valid_from"], "valid_to": r["valid_to"],
            })],
        ))
    else:
        others = [f"{r['institution_id']}（{r['kind']}）" for r in registrations]
        findings.append(_deny(
            C.RULE_PERSON_REGISTRATION, C.SubjectType.PERSON,
            "人员的主执业机构与有效跨机构备案均不包含申请机构"
            + (f"；当前注册：{('、'.join(others))}" if others else "；当前无任何有效注册"),
            [_ref("人员注册记录", r, {
                "kind": r["kind"], "institution_id": r["institution_id"],
                "valid_from": r["valid_from"], "valid_to": r["valid_to"],
            }) for r in registrations],
        ))

    snapshot = {
        "certificate": dict(cert) if cert else None,
        "active_suspension": dict(suspension) if suspension else None,
        "attending": [dict(a) for a in attendings],
        "registrations": [dict(r) for r in registrations],
        "project": dict(project),
    }
    return findings, snapshot


def _row_or_none(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ---------- 范围计算 ----------

def institution_scope(conn: sqlite3.Connection, institution_id: str, at: str) -> dict[str, Any]:
    """计算机构可执业范围：有效许可 × 有效地点 × 科目树 × 等级。"""
    license_row = effective_license(conn, institution_id, at)
    sites = list(conn.execute(
        "SELECT * FROM institution_sites WHERE institution_id = ? AND valid_from <= ? "
        "AND (valid_to IS NULL OR valid_to >= ?) ORDER BY id",
        (institution_id, at, at),
    ))
    if license_row is None:
        allowed: list[str] = []
    else:
        scope = loads(license_row["scope_categories"])
        allowed = sorted(
            code for code in descendant_codes(conn, scope)
            if (cat := get_category(conn, code)) is not None and cat["level"] >= license_row["level"]
        )
    return {
        "institution_id": institution_id,
        "at": at,
        "license": _row_or_none(license_row),
        "sites": [{"site_id": s["id"], "address": s["address"]} for s in sites],
        "allowed_projects": allowed,
        "basis": ("无有效机构许可，可执业范围为空" if license_row is None
                  else f"许可第{license_row['version']}版科目 {sorted(loads(license_row['scope_categories']))} "
                       f"且等级不高于 {license_row['level']}"),
    }


def person_scope(conn: sqlite3.Connection, person_id: str, at: str) -> dict[str, Any]:
    """计算人员可执业范围：执业证 × 主诊资格 × 注册机构，按注册机构分组。"""
    cert = effective_cert(conn, person_id, at)
    suspension = active_suspension(conn, cert["id"], at) if cert else None
    attendings = effective_attendings(conn, person_id, at)
    registrations = effective_registrations(conn, person_id, at)

    cert_codes: set[str] = set()
    if cert is not None and suspension is None:
        cert_codes = descendant_codes(conn, loads(cert["practice_scope"]))

    # 每个主诊资格给出其可覆盖科目（受等级约束）
    per_institution: dict[str, dict[str, Any]] = {}
    for reg in registrations:
        # 无主诊资格则范围为空（P-003）；否则取执业证范围与各主诊资格范围的交集
        allowed = set(cert_codes) if attendings else set()
        qualifying: list[dict[str, Any]] = []
        for a in attendings:
            a_codes = {
                code for code in descendant_codes(conn, loads(a["scope_categories"]))
                if (cat := get_category(conn, code)) is not None and cat["level"] >= a["level"]
            }
            if a_codes:
                qualifying.append({"title": a["title"], "level": a["level"]})
            allowed &= a_codes
        per_institution[reg["institution_id"]] = {
            "institution_id": reg["institution_id"],
            "registration_kind": reg["kind"],
            "valid_from": reg["valid_from"],
            "valid_to": reg["valid_to"],
            "allowed_projects": sorted(allowed),
            "attending_qualifications": qualifying,
        }

    notes = []
    if cert is None:
        notes.append("无有效执业证")
    elif suspension is not None:
        notes.append(f"执业证自 {suspension['effective_from']} 起暂停，范围为空")
    if not attendings:
        notes.append("无有效主诊资格，范围为空")

    return {
        "person_id": person_id,
        "at": at,
        "certificate": _row_or_none(cert),
        "active_suspension": _row_or_none(suspension),
        "registrations": list(per_institution.values()),
        "basis_notes": notes or ["执业证范围 ∩ 主诊资格范围（等级过滤），按注册/备案机构分别列示"],
    }
