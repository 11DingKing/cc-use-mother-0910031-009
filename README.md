# 执业资质范围核验

监管窗口受理机构新增项目申请时，需要同时核对**人员执业证、主诊资格、注册机构、项目等级**。本项目在领域契约之上提供完整 Python 后端：登记资质版本、注册地点、项目分类与授权证据，对机构与人员**分别**计算可执业范围并生成核验案件；证书续期、暂停、跨机构备案、材料补交均保留原决定；批准动作在单个事务内原子写入授权，数据库层防止重复授予；API 对每个允许/拒绝结论给出规则代码、理由与证据摘录。

## 领域模型

| 主题 | 表 | 说明 |
| --- | --- | --- |
| 资质版本 | `institution_license_versions`、`person_certificate_versions`、`attending_qualification_versions` | 新发/续期只**追加版本**，旧版本置“已换发”，历史决定可回放 |
| 注册地点 | `institution_sites` | 机构执业地点，带有效期 |
| 人员注册 | `person_registrations` | 主执业机构 / 跨机构备案，时态有效 |
| 暂停事件 | `person_cert_events` | 暂停/恢复区间，评估时点命中即视为证书不可用 |
| 项目分类 | `project_categories` | 父子科目树 + 项目等级（数字越小等级越高） |
| 授权证据 | `case_evidence` | 申请材料 / 补交材料，SHA-256 快照 |
| 核验案件 | `cases`、`evaluations` | 状态：登记→待核验→处置中→已决定→已归档；评估结果 append-only |
| 决定与授权 | `decisions`、`grants` | 批准与授权同事务原子写入；部分唯一索引防重复授予 |

## 核验规则（共 9 条，全部合取）

**机构侧**

- `I-001` 机构执业许可评估时点有效
- `I-002` 执业地点已登记于该机构且在有效期内
- `I-003` 许可科目沿分类树覆盖申请项目
- `I-004` 机构资质等级满足项目等级

**人员侧**

- `P-001` 执业证有效且无未解除的暂停
- `P-002` 执业证执业范围覆盖项目（**证书有效但范围不符在此拦截**）
- `P-003` 主诊资格登记且在有效期内
- `P-004` 主诊资格科目与等级覆盖项目
- `P-005` 主执业机构或有效跨机构备案包含申请机构

每条规则结论结构：

```json
{
  "rule": "P-002",
  "title": "执业证执业范围覆盖申请项目",
  "subject": "人员",
  "verdict": "拒绝",
  "reason": "执业证有效，但执业范围 ['INTERNAL-MED'] 不覆盖项目 SURG-GS（普通外科），不得开展",
  "evidence": [{"kind": "人员证书版本", "record_id": 7, "excerpt": {"cert_no": "CERT-LI", "practice_scope": ["INTERNAL-MED"]}}]
}
```

## 时态事件与原决定保留

- **证书续期 / 许可续期**：追加新版本，旧版置“已换发”，自动为在办案件追加一条新评估。
- **暂停 / 恢复**：写入时态事件区间；仅影响事件区间内的评估，历史评估快照不变。
- **跨机构备案**：新增“跨机构备案”注册记录，自动追加评估（P-005 可由拒绝变允许）。
- **材料补交**：证据以“补交材料”追加并生成新评估；**已作出的决定、授权与历史评估永不修改**，可按评估编号逐条回放。

## 防重复授予（三重保障）

1. 决定接口幂等：同一 `idempotency_key` 返回同一决定（`idempotent_replay: true`）。
2. 业务守卫：案件已有批准决定时再次批准返回 `409 duplicate_grant`。
3. 数据库部分唯一索引：
   - `ux_grants_case`（每案件至多一条**有效**授权；撤销后可重新批准，决定史完整保留）；
   - `ux_grants_subject_project`（同机构-地点-人员-项目至多一条有效授权）。

批准在 `BEGIN IMMEDIATE` 事务内完成“复核最新评估为允许 → 写决定 → 写授权 → 更新案件状态”，任一步失败整体回滚。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/qualification_verifier/`：
  - `constants.py` 状态与规则代码；`storage.py` SQLite 建表；
  - `rules.py` 双主体规则引擎与可执业范围计算；
  - `service.py` 登记、时态事件、评估快照、原子决定/授权；
  - `api.py` 标准库 JSON HTTP 服务；`seed.py` 演示数据。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约、服务层与 HTTP API 回归测试。

## 快速开始

```bash
# 播种演示数据（含四个对照案件：全匹配 / 范围不符 / 等级不足 / 跨机构备案）
PYTHONPATH=src python3 -m qualification_verifier.seed demo.db

# 启动 API（零第三方依赖，仅需 Python 3.11+）
PYTHONPATH=src python3 -m qualification_verifier --db demo.db --port 8080
```

## 主要 API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/institutions`、`/api/persons`、`/api/categories` | 主体与项目分类登记 |
| POST | `/api/institutions/{id}/sites`、`/licenses` | 注册地点、许可版本 |
| GET | `/api/institutions/{id}/scope?at=` | 机构可执业范围 |
| POST | `/api/persons/{id}/certificates`、`/certificates/suspend`、`/certificates/resume` | 执业证版本与暂停/恢复 |
| POST | `/api/persons/{id}/attending-qualifications` | 主诊资格版本 |
| POST | `/api/persons/{id}/registrations/primary`、`.../cross-institution` | 注册机构 / 跨机构备案 |
| GET | `/api/persons/{id}/scope?at=` | 人员可执业范围（按注册机构分组） |
| POST | `/api/cases` | 生成核验案件并立即双主体评估 |
| GET | `/api/cases/{id}` | 案件、最新评估、评估历史、决定、授权、事件日志 |
| POST | `/api/cases/{id}/evidence`、`/supplement` | 证据登记、材料补交（原决定保留） |
| POST | `/api/cases/{id}/evaluations`、GET `/evaluations/{eid}` | 人工复评、历史快照回放 |
| POST | `/api/cases/{id}/decisions` | 批准/拒绝（批准原子写授权，需 `idempotency_key`） |
| POST | `/api/cases/{id}/revoke-grant`、`/archive` | 撤销授权、归档 |

批准请求示例：

```json
POST /api/cases/CASE-OK/decisions
{"result": "批准", "decided_by": "监管员甲", "reason": "九项规则全部通过", "idempotency_key": "k-001"}
```

## 验证

```bash
python3 -m unittest discover -s tests -v        # 24 项测试
python3 -m compileall -q src tools tests        # 编译检查
python3 tools/check_contract.py domain/contract.json
```
