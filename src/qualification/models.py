"""领域常量、异常与只读数据结构。

所有时间统一使用 ``YYYY-MM-DD`` 字符串（ISO 日期，字典序即时间序），
避免引入第三方依赖；核验时由调用方显式传入 ``as_of``，保证结论可复现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------------------
# 案件状态：与 domain/contract.json 的 states 对齐
# ---------------------------------------------------------------------------
CASE_REGISTERED = "登记"
CASE_PENDING = "待核验"
CASE_PROCESSING = "处置中"
CASE_DECIDED = "已决定"
CASE_ARCHIVED = "已归档"

CASE_STATES = (CASE_REGISTERED, CASE_PENDING, CASE_PROCESSING, CASE_DECIDED, CASE_ARCHIVED)

# 案件结论
DECISION_ALLOW = "允许"
DECISION_DENY = "拒绝"
DECISION_PENDING_MATERIAL = "补交材料"

# 核验动作（append-only 事件）
ACTION_SUBMIT = "提交申请"
ACTION_SUPPLEMENT = "材料补交"
ACTION_APPROVE = "批准"
ACTION_APPROVE_BLOCKED = "批准驳回"
ACTION_REJECT = "拒绝"
ACTION_REQUEST_MATERIAL = "要求补件"
ACTION_ARCHIVE = "归档"

# 人员执业证状态
CERT_ACTIVE = "有效"
CERT_SUSPENDED = "暂停"
CERT_EXPIRED = "过期"
CERT_REVOKED = "吊销"

# 机构资质状态
INST_QUAL_ACTIVE = "有效"
INST_QUAL_SUSPENDED = "暂停"

# 跨机构备案状态
FILING_ACTIVE = "有效"
FILING_CLOSED = "已结束"

# 项目等级（数字越大等级越高，例如四级手术 > 一级手术）
PROJECT_GRADES = ("一级", "二级", "三级", "四级")
GRADE_RANK = {grade: index for index, grade in enumerate(PROJECT_GRADES, start=1)}

# 证据类型
EVIDENCE_QUALIFICATION_VERSION = "资质版本"
EVIDENCE_REGISTRATION = "注册地点"
EVIDENCE_FILING = "跨机构备案"
EVIDENCE_PROJECT_CATALOG = "项目分类"
EVIDENCE_MATERIAL = "申请材料"
EVIDENCE_DECISION_SNAPSHOT = "核验快照"

Decision = Literal["允许", "拒绝", "补交材料"]


class QualificationError(Exception):
    """业务规则错误（输入非法或状态机冲突）。"""


class NotFoundError(QualificationError):
    """引用的领域对象不存在。"""


class DuplicateGrantError(QualificationError):
    """授权已存在或案件已决定，防止重复授予。"""


@dataclass(frozen=True)
class ScopeItem:
    """单条可执业范围条目。

    ``allowed=False`` 时 ``reasons`` 给出拒绝原因（可能多条，例如
    执业证有效但主诊资格过期且备案无效）；``evidence`` 为支撑证据 id 列表。
    """

    category_code: str
    category_name: str
    grade: str
    allowed: bool
    reasons: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubjectScope:
    """单个主体（机构或人员）的可执业范围计算结果。"""

    subject_type: str
    subject_id: str
    as_of: str
    items: tuple[ScopeItem, ...] = field(default_factory=tuple)

    def allowed_pairs(self) -> frozenset[tuple[str, str]]:
        return frozenset((item.category_code, item.grade) for item in self.items if item.allowed)

    def find(self, category_code: str, grade: str) -> ScopeItem | None:
        for item in self.items:
            if item.category_code == category_code and item.grade == grade:
                return item
        return None


@dataclass(frozen=True)
class RuleResult:
    """一条核验规则的判定明细。"""

    rule_code: str
    title: str
    passed: bool
    detail: str
    evidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CaseEvaluation:
    """案件在某一时点的完整核验结论。"""

    case_id: str
    as_of: str
    decision: Decision
    summary: str
    rules: tuple[RuleResult, ...]
    institution_scope: SubjectScope
    personnel_scope: SubjectScope

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "as_of": self.as_of,
            "decision": self.decision,
            "summary": self.summary,
            "rules": [
                {
                    "rule_code": rule.rule_code,
                    "title": rule.title,
                    "passed": rule.passed,
                    "detail": rule.detail,
                    "evidence": list(rule.evidence),
                }
                for rule in self.rules
            ],
        }
