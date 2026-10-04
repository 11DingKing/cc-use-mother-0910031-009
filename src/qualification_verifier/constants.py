"""领域常量：状态、角色、规则代码。

状态取值与 ``domain/contract.json`` 中的案件状态保持一致。
"""
from __future__ import annotations


class CaseStatus:
    REGISTERED = "登记"
    PENDING = "待核验"
    HANDLING = "处置中"
    DECIDED = "已决定"
    ARCHIVED = "已归档"


class LicenseStatus:
    EFFECTIVE = "有效"
    SUSPENDED = "暂停"
    SUPERSEDED = "已换发"
    REVOKED = "注销"


class CertificateStatus:
    EFFECTIVE = "有效"
    SUPERSEDED = "已换发"
    REVOKED = "注销"


class CertificateEvent:
    SUSPEND = "暂停"
    RESUME = "恢复"


class RegistrationKind:
    PRIMARY = "主执业机构"
    FILING = "跨机构备案"


class RegistrationStatus:
    EFFECTIVE = "有效"
    STOPPED = "停止"


class QualificationStatus:
    EFFECTIVE = "有效"
    REVOKED = "注销"


class SubjectType:
    INSTITUTION = "机构"
    PERSON = "人员"


class GrantStatus:
    EFFECTIVE = "有效"
    REVOKED = "撤销"


class DecisionResult:
    APPROVED = "批准"
    REJECTED = "拒绝"


class EvidenceKind:
    APPLICATION = "申请材料"
    SUPPLEMENT = "补交材料"


class Verdict:
    ALLOW = "允许"
    DENY = "拒绝"


# 机构侧规则
RULE_INST_LICENSE_EFFECTIVE = "I-001"
RULE_INST_SITE_REGISTERED = "I-002"
RULE_INST_SCOPE_COVERS = "I-003"
RULE_INST_LEVEL_SUFFICIENT = "I-004"

# 人员侧规则
RULE_PERSON_CERT_EFFECTIVE = "P-001"
RULE_PERSON_CERT_SCOPE = "P-002"
RULE_PERSON_ATTENDING_EXISTS = "P-003"
RULE_PERSON_ATTENDING_LEVEL = "P-004"
RULE_PERSON_REGISTRATION = "P-005"

RULE_TITLES = {
    RULE_INST_LICENSE_EFFECTIVE: "机构执业许可当前有效",
    RULE_INST_SITE_REGISTERED: "执业地点已在许可中登记",
    RULE_INST_SCOPE_COVERS: "机构许可科目覆盖申请项目",
    RULE_INST_LEVEL_SUFFICIENT: "机构资质等级满足项目等级",
    RULE_PERSON_CERT_EFFECTIVE: "人员执业证当前有效",
    RULE_PERSON_CERT_SCOPE: "执业证执业范围覆盖申请项目",
    RULE_PERSON_ATTENDING_EXISTS: "主诊资格登记且在有效期内",
    RULE_PERSON_ATTENDING_LEVEL: "主诊资格等级满足项目等级",
    RULE_PERSON_REGISTRATION: "注册机构与申请机构一致（含跨机构备案）",
}
