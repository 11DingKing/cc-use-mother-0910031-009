"""端到端场景演示：监管窗口核对机构新增项目申请。

运行：python3 tools/demo.py
覆盖：登记 → 证书有效但范围/等级/机构不符被识别 → 补件 → 原子批准 →
      暂停失效 → 续期新版本 → 恢复后重新立案；原决定与证据全程保留。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification import (  # noqa: E402
    DuplicateGrantError,
    QualificationError,
    QualificationService,
    connect,
)

MATERIALS = [
    {"material_type": "机构资质凭证", "file_ref": "f/inst.pdf"},
    {"material_type": "执业证凭证", "file_ref": "f/cert.pdf"},
    {"material_type": "主诊资格凭证", "file_ref": "f/attending.pdf"},
    {"material_type": "注册或备案凭证", "file_ref": "f/registration.pdf"},
]


def show(title: str, result: dict) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")
    print(f"结论：{result['decision']}　{result['summary']}")
    for rule in result["rules"]:
        mark = "✓" if rule["passed"] else "✗"
        print(f"  [{mark}] {rule['rule_code']} {rule['title']} —— {rule['detail']}")
        if not rule["passed"]:
            for ev in rule["evidence"]:
                print(f"        证据: {json.dumps(ev, ensure_ascii=False)}")


def main() -> None:
    db_dir = Path(tempfile.mkdtemp(prefix="qual-demo-"))
    svc = QualificationService(connect(db_dir / "demo.db"))

    # 1. 登记 -------------------------------------------------------------
    svc.register_institution("H", "华康医院", at="2026-01-01")
    svc.register_institution("M", "民安门诊部", at="2026-01-01")
    svc.register_personnel("P", "张医生", at="2026-01-01")
    svc.register_category("S", "眼科", "四级")
    svc.register_category("D", "口腔", "三级")

    # 机构 H：眼科三级；机构 M：口腔三级
    svc.register_institution_qualification(
        "H", [{"category_code": "S", "grade": "三级"}], "2025-01-01", "2026-12-31",
        at="2026-01-01")
    svc.register_institution_qualification(
        "M", [{"category_code": "D", "grade": "三级"}], "2025-01-01", "2026-12-31",
        at="2026-01-01")
    # 张医生：执业证眼科四级（有效！），主诊资格仅眼科三级，注册于 H
    svc.register_cert(
        "P", "LIC-0001", [{"category_code": "S", "grade": "四级"}],
        "2025-01-01", "2026-12-31", at="2026-01-01")
    svc.register_attending_qualification(
        "P", "S", "三级", "2025-01-01", "2026-12-31", at="2026-01-01")
    svc.register_practice_site("P", "H", "2025-01-01", at="2026-01-01")

    # 2. 误判高发场景：执业证有效，但向无眼科资质的 M 申请 ----------------
    c1 = svc.open_case("M", "P", "S", "三级", MATERIALS, case_id="CASE-A", at="2026-03-01")
    show("案件 A：向口腔门诊部申请眼科三级（证书有效但机构范围不符+未备案）",
         svc.evaluate(c1, as_of="2026-03-01"))
    try:
        svc.approve(c1, as_of="2026-03-01")
    except QualificationError as exc:
        print(f"  >> 批准被拦截：{exc}")

    # 3. 等级不符：机构与主诊资格都只有三级，申请四级 ---------------------
    c2 = svc.open_case("H", "P", "S", "四级", MATERIALS, case_id="CASE-B", at="2026-03-01")
    show("案件 B：眼科四级（执业证够，但机构资质与主诊资格仅三级）",
         svc.evaluate(c2, as_of="2026-03-01"))

    # 4. 材料不齐 → 补交 → 允许 -------------------------------------------
    c3 = svc.open_case("H", "P", "S", "二级", MATERIALS[:2],
                       case_id="CASE-C", at="2026-03-01")
    show("案件 C：眼科二级，先缺 2 份材料", svc.evaluate(c3, as_of="2026-03-01"))
    svc.supplement_material(c3, MATERIALS[2:], at="2026-03-05")
    show("案件 C：补交后重新核验", svc.evaluate(c3, as_of="2026-03-05"))
    svc.approve(c3, as_of="2026-03-05")
    print("  >> 已原子写入授权：", json.dumps(svc.list_grants(c3), ensure_ascii=False))

    # 5. 暂停 → 授权失效；恢复 → 重新立案授予 -----------------------------
    svc.set_cert_status("P", "暂停", at="2026-05-01")
    c4 = svc.open_case("H", "P", "S", "一级", MATERIALS, case_id="CASE-D", at="2026-05-02")
    show("案件 D：执业证暂停期间的新申请", svc.evaluate(c4, as_of="2026-05-02"))
    svc.set_cert_status("P", "有效", at="2026-06-01")
    c5 = svc.open_case("H", "P", "S", "一级", MATERIALS, case_id="CASE-E", at="2026-06-02")
    svc.approve(c5, as_of="2026-06-02")
    print("  >> 恢复后重新立案授予成功；旧失效授权仍在台账（status=已失效）")

    # 6. 跨机构备案，备案等级限缩 -----------------------------------------
    svc.register_institution_qualification(
        "M", [{"category_code": "D", "grade": "三级"},
              {"category_code": "S", "grade": "二级"}],
        "2026-02-01", "2027-12-31", at="2026-02-01")
    svc.file_cross_institution(
        "P", "H", "M", "S", "一级", "2026-02-01", valid_to="2026-12-31", at="2026-02-01")
    c6 = svc.open_case("M", "P", "S", "二级", MATERIALS, case_id="CASE-F", at="2026-07-01")
    show("案件 F：备案仅一级，申请二级", svc.evaluate(c6, as_of="2026-07-01"))

    # 7. 防重复授予 --------------------------------------------------------
    try:
        svc.approve(c3, as_of="2026-08-01")
    except DuplicateGrantError as exc:
        print(f"\n案件 C 重复批准被拒绝：{exc}")

    # 8. 原决定与证据快照保留 ----------------------------------------------
    case = svc.conn.execute("SELECT * FROM cases WHERE id = 'CASE-C'").fetchone()
    print(f"\n案件 C 原决定保持：{case['decision']} @ {case['decided_at']}，"
          f"事件流 {len(svc.case_timeline('CASE-C'))} 条（含历次核验快照）")
    print(f"演示数据库（可继续查询）：{db_dir / 'demo.db'}")


if __name__ == "__main__":
    main()
