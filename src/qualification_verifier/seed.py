"""演示数据与场景播种。

运行：``python -m qualification_verifier.seed <db_path>``

场景设计（用于人工核对规则解释）：

* 案件 CASE-OK：机构许可、人员执业证、主诊资格、注册机构全部匹配 → 允许。
* 案件 CASE-SCOPE-DENY：人员执业证在有效期内但执业范围不覆盖项目 →
  必须拒绝（直击“证书有效但范围不符被误判为可开展”）。
* 案件 CASE-LEVEL-DENY：证书有效且范围覆盖，但主诊资格等级不足 → 拒绝。
* 案件 CASE-FILING-OK：人员主机构为其他医院，凭跨机构备案 + 补交材料 → 允许。
"""
from __future__ import annotations

import sys

from .service import QualificationService
from .storage import connect, init_db


def seed_demo(svc: QualificationService) -> dict[str, str]:
    # 项目分类树：外科(SURG,1) > 普外科(SURG-GS,2)；医疗美容(MED-AESTH,1) >
    # 美容外科(AESTH-SURG,2) > 四级美容外科(AESTH-SURG-4,1)
    svc.register_category("SURG", "外科", level=1)
    svc.register_category("SURG-GS", "普通外科", level=2, parent_code="SURG")
    svc.register_category("MED-AESTH", "医疗美容科", level=1)
    svc.register_category("AESTH-SURG", "美容外科", level=2, parent_code="MED-AESTH")
    svc.register_category("AESTH-SURG-4", "四级美容外科项目", level=1, parent_code="AESTH-SURG")

    # 机构：瑞康医院（许可科目：外科、医疗美容科，等级 2）
    svc.create_institution("INST-RK", "瑞康医院")
    svc.register_site("INST-RK", "瑞康医院本部", "2025-01-01T00:00:00Z",
                      site_id="SITE-RK-MAIN")
    svc.register_institution_license(
        "INST-RK", "LIC-RK-2025", ["SURG", "MED-AESTH"], level=2,
        valid_from="2025-01-01T00:00:00Z", valid_to="2027-12-31T23:59:59Z")

    # 外院（用于跨机构备案场景）
    svc.create_institution("OTHER-HOSP", "外院")
    svc.register_site("OTHER-HOSP", "外院本部", "2025-01-01T00:00:00Z",
                      site_id="SITE-OTHER")
    svc.register_institution_license(
        "OTHER-HOSP", "LIC-OTHER", ["SURG"], level=2,
        valid_from="2025-01-01T00:00:00Z", valid_to="2027-12-31T23:59:59Z")

    # 张医生：执业范围外科，主诊资格普通外科等级2
    svc.create_person("PER-ZHANG", "张医生")
    svc.register_primary_registration("PER-ZHANG", "INST-RK", "2025-01-01T00:00:00Z")
    svc.register_certificate(
        "PER-ZHANG", "CERT-ZHANG", ["SURG"],
        valid_from="2025-01-01T00:00:00Z", valid_to="2027-12-31T23:59:59Z")
    svc.register_attending(
        "PER-ZHANG", "普通外科主诊医师", ["SURG-GS"], level=2,
        valid_from="2025-01-01T00:00:00Z", valid_to="2027-12-31T23:59:59Z")

    # 李医生：执业范围仅内科（此处以内科不在树中表示超范围），证书有效
    svc.create_person("PER-LI", "李医生")
    svc.register_primary_registration("PER-LI", "INST-RK", "2025-01-01T00:00:00Z")
    svc.register_certificate(
        "PER-LI", "CERT-LI", ["INTERNAL-MED"],
        valid_from="2025-01-01T00:00:00Z", valid_to="2027-12-31T23:59:59Z")
    svc.register_attending(
        "PER-LI", "普通外科主诊医师", ["SURG-GS"], level=2,
        valid_from="2025-01-01T00:00:00Z", valid_to="2027-12-31T23:59:59Z")

    # 王医生：范围覆盖但主诊资格只能做等级2，申请等级1的四级项目
    svc.create_person("PER-WANG", "王医生")
    svc.register_primary_registration("PER-WANG", "INST-RK", "2025-01-01T00:00:00Z")
    svc.register_certificate(
        "PER-WANG", "CERT-WANG", ["MED-AESTH"],
        valid_from="2025-01-01T00:00:00Z", valid_to="2027-12-31T23:59:59Z")
    svc.register_attending(
        "PER-WANG", "美容外科主诊医师", ["AESTH-SURG"], level=2,
        valid_from="2025-01-01T00:00:00Z", valid_to="2027-12-31T23:59:59Z")

    # 赵医生：主机构在外院，在瑞康医院只有跨机构备案
    svc.create_person("PER-ZHAO", "赵医生")
    svc.register_primary_registration("PER-ZHAO", "OTHER-HOSP", "2025-01-01T00:00:00Z")
    svc.register_certificate(
        "PER-ZHAO", "CERT-ZHAO", ["SURG"],
        valid_from="2025-01-01T00:00:00Z", valid_to="2027-12-31T23:59:59Z")
    svc.register_attending(
        "PER-ZHAO", "普通外科主诊医师", ["SURG-GS"], level=2,
        valid_from="2025-01-01T00:00:00Z", valid_to="2027-12-31T23:59:59Z")

    # 案件
    svc.create_case("CASE-OK", "INST-RK", "SITE-RK-MAIN", "PER-ZHANG", "SURG-GS")
    svc.add_evidence("CASE-OK", "申请表.pdf", {"type": "申请表", "project": "SURG-GS"})

    svc.create_case("CASE-SCOPE-DENY", "INST-RK", "SITE-RK-MAIN", "PER-LI", "SURG-GS")
    svc.add_evidence("CASE-SCOPE-DENY", "申请表.pdf", {"type": "申请表", "project": "SURG-GS"})

    svc.create_case("CASE-LEVEL-DENY", "INST-RK", "SITE-RK-MAIN", "PER-WANG",
                    "AESTH-SURG-4")

    svc.create_case("CASE-FILING-OK", "INST-RK", "SITE-RK-MAIN", "PER-ZHAO", "SURG-GS")
    # 初次评估 P-005 拒绝（赵医生未注册在瑞康）；跨机构备案后自动追加评估转为允许
    svc.file_cross_institution(
        "PER-ZHAO", "INST-RK", valid_from="2026-01-01T00:00:00Z")
    svc.supplement_evidence(
        "CASE-FILING-OK", "跨机构执业备案表.pdf",
        {"type": "跨机构备案凭证", "institution_id": "INST-RK"})

    return {"ok": "CASE-OK", "scope_deny": "CASE-SCOPE-DENY",
            "level_deny": "CASE-LEVEL-DENY", "filing": "CASE-FILING-OK"}


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    db_path = args[0] if args else "qualification.db"
    conn = connect(db_path)
    init_db(conn)
    ids = seed_demo(QualificationService(conn))
    print("演示数据播种完成：", ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
