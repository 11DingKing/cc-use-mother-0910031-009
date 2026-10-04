"""可执业范围计算（纯函数）。

机构与人员分别计算，互不授予对方不具备的范围；核验案件再对两侧做交集。
每个条目都带允许/拒绝原因与证据引用，避免"证书有效但范围不符"被误判。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from .models import GRADE_RANK, SubjectScope, ScopeItem


def _grade_covers(entry_grade: str, target_grade: str) -> bool:
    """授权条目按最高等级解释：证载"三级"覆盖一/二/三级。"""
    return GRADE_RANK[entry_grade] >= GRADE_RANK[target_grade]


def _latest_version(rows: Iterable[sqlite3.Row], as_of: str) -> sqlite3.Row | None:
    """取 valid_from 已生效的最大版本号（续期登记新版本而非改旧行）。"""
    candidates = [row for row in rows if row["valid_from"] <= as_of]
    return max(candidates, key=lambda row: row["version_no"], default=None)


def _parse_scope(raw: str) -> list[dict]:
    return json.loads(raw)


def compute_institution_scope(conn: sqlite3.Connection, institution_id: str, as_of: str) -> SubjectScope:
    """计算机构在 as_of 时点对目录内全部 (分类, 等级) 的可执业范围。"""
    categories = conn.execute(
        "SELECT code, name, max_grade FROM project_categories ORDER BY code"
    ).fetchall()
    version_rows = conn.execute(
        """SELECT * FROM institution_qual_versions
           WHERE institution_id = ? ORDER BY version_no""",
        (institution_id,),
    ).fetchall()
    version = _latest_version(version_rows, as_of)

    items: list[ScopeItem] = []
    for category in categories:
        granted_grade = None
        if version is not None:
            for entry in _parse_scope(version["scope_json"]):
                if entry["category_code"] == category["code"]:
                    granted_grade = entry["grade"]
                    break

        for grade in sorted(GRADE_RANK, key=GRADE_RANK.get):
            reasons: list[str] = []
            evidence: list[str] = []
            if version is None:
                reasons.append("机构无已生效的资质版本")
            else:
                evidence.append(f"机构资质版本 v{version['version_no']}")
                if version["valid_to"] < as_of:
                    reasons.append(f"机构资质已过期（有效期至 {version['valid_to']}）")
                elif version["status"] == "暂停":
                    reasons.append("机构资质处于暂停状态")
                if granted_grade is None:
                    reasons.append(f"机构资质范围未包含分类「{category['name']}」")
                elif not _grade_covers(granted_grade, grade):
                    reasons.append(f"机构资质等级 {granted_grade} 不覆盖{grade}项目")
                if GRADE_RANK[grade] > GRADE_RANK[category["max_grade"]]:
                    reasons.append(f"{grade}超出项目目录对该分类设定的最高等级（{category['max_grade']}）")

            items.append(
                ScopeItem(
                    category_code=category["code"],
                    category_name=category["name"],
                    grade=grade,
                    allowed=not reasons,
                    reasons=tuple(reasons),
                    evidence=tuple(evidence),
                )
            )
    return SubjectScope("机构", institution_id, as_of, tuple(items))


def _personnel_base(
    conn: sqlite3.Connection, personnel_id: str, as_of: str
) -> tuple:
    """汇总人员与机构无关的时态事实：执业证、主诊资格。"""
    cert_rows = conn.execute(
        "SELECT * FROM personnel_cert_versions WHERE personnel_id = ? ORDER BY version_no",
        (personnel_id,),
    ).fetchall()
    cert = _latest_version(cert_rows, as_of)

    attending_rows = conn.execute(
        "SELECT * FROM attending_qual_versions WHERE personnel_id = ?",
        (personnel_id,),
    ).fetchall()
    # 每个分类各自取最新生效版本
    attending: dict[str, sqlite3.Row] = {}
    for row in attending_rows:
        if row["valid_from"] <= as_of:
            current = attending.get(row["category_code"])
            if current is None or row["version_no"] > current["version_no"]:
                attending[row["category_code"]] = row
    return cert, attending


def _registration_at(
    conn: sqlite3.Connection, personnel_id: str, institution_id: str, as_of: str
) -> sqlite3.Row | None:
    rows = conn.execute(
        """SELECT * FROM registrations
           WHERE personnel_id = ? AND institution_id = ?
             AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)
           ORDER BY id DESC""",
        (personnel_id, institution_id, as_of, as_of),
    ).fetchall()
    return rows[0] if rows else None


def _filing_at(
    conn: sqlite3.Connection, personnel_id: str, institution_id: str,
    category_code: str, as_of: str,
) -> sqlite3.Row | None:
    rows = conn.execute(
        """SELECT * FROM cross_filings
           WHERE personnel_id = ? AND host_institution_id = ? AND category_code = ?
             AND status = '有效' AND valid_from <= ?
             AND (valid_to IS NULL OR valid_to >= ?)
           ORDER BY id DESC""",
        (personnel_id, institution_id, category_code, as_of, as_of),
    ).fetchall()
    return rows[0] if rows else None


def compute_personnel_scope(
    conn: sqlite3.Connection, personnel_id: str, institution_id: str, as_of: str,
) -> SubjectScope:
    """计算人员在指定机构、指定时点的可执业范围。

    主执业机构凭注册地点；其他机构凭有效的跨机构备案，备案范围在分类/等级
    两个维度上限缩执业证与主诊资格的交集。
    """
    categories = conn.execute(
        "SELECT code, name, max_grade FROM project_categories ORDER BY code"
    ).fetchall()
    cert, attending = _personnel_base(conn, personnel_id, as_of)
    registration = _registration_at(conn, personnel_id, institution_id, as_of)

    # 该机构是否为人员的主执业机构
    is_home = registration is not None

    cert_entries = _parse_scope(cert["scope_json"]) if cert is not None else []
    cert_map = {entry["category_code"]: entry["grade"] for entry in cert_entries}

    items: list[ScopeItem] = []
    for category in categories:
        filing = None if is_home else _filing_at(
            conn, personnel_id, institution_id, category["code"], as_of
        )
        att = attending.get(category["code"])
        cert_grade = cert_map.get(category["code"])

        for grade in sorted(GRADE_RANK, key=GRADE_RANK.get):
            reasons: list[str] = []
            evidence: list[str] = []

            # 规则1：执业证时态
            if cert is None:
                reasons.append("人员无已生效的执业证版本")
            else:
                evidence.append(f"执业证版本 v{cert['version_no']}")
                if cert["valid_to"] < as_of:
                    reasons.append(f"执业证已过期（有效期至 {cert['valid_to']}）")
                elif cert["status"] == "吊销":
                    reasons.append("执业证已吊销")
                elif cert["status"] == "暂停":
                    reasons.append("执业证处于暂停状态")

            # 规则2：执业证载范围
            if cert is not None and cert_grade is None:
                reasons.append(f"执业证载范围未包含分类「{category['name']}」")
            elif cert is not None and cert_grade is not None and not _grade_covers(cert_grade, grade):
                reasons.append(f"执业证载等级 {cert_grade} 不覆盖{grade}项目")

            # 规则3：主诊资格
            if att is None:
                reasons.append(f"人员无「{category['name']}」分类的主诊资格")
            else:
                evidence.append(f"主诊资格版本 v{att['version_no']}")
                if att["valid_to"] < as_of:
                    reasons.append(f"主诊资格已过期（有效期至 {att['valid_to']}）")
                elif att["status"] == "吊销":
                    reasons.append("主诊资格已吊销")
                elif att["status"] == "暂停":
                    reasons.append("主诊资格处于暂停状态")
                elif not _grade_covers(att["max_grade"], grade):
                    reasons.append(f"主诊资格最高{att['max_grade']}，不覆盖{grade}项目")

            # 规则4：注册机构 / 跨机构备案
            if is_home:
                evidence.append(
                    f"注册地点 #{registration['id']}（{registration['valid_from']} 起）"
                )
            elif filing is not None:
                evidence.append(f"跨机构备案 #{filing['id']}")
                if not _grade_covers(filing["max_grade"], grade):
                    reasons.append(f"跨机构备案最高{filing['max_grade']}，不覆盖{grade}项目")
            else:
                reasons.append("该机构既非人员有效注册的主执业机构，也无有效跨机构备案")

            items.append(
                ScopeItem(
                    category_code=category["code"],
                    category_name=category["name"],
                    grade=grade,
                    allowed=not reasons,
                    reasons=tuple(reasons),
                    evidence=tuple(evidence),
                )
            )
    return SubjectScope("人员", personnel_id, as_of, tuple(items))
