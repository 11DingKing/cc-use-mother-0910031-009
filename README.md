# 执业资质范围核验

监管窗口核对机构新增项目申请时，需要同时核对**人员执业证、主诊资格、注册机构和项目等级**。本服务把这些事实登记为带时态的版本与证据，对**机构与人员分别**计算可执业范围并生成核验案件；每条允许/拒绝结论都给出规则与证据，专门防止"证书有效但范围不符"被误判为可开展。

纯 Python 标准库实现（`sqlite3` + `http.server`），无第三方依赖，Python ≥ 3.11。

## 核心规则（共 14 条，逐条出证）

| 规则 | 核对内容 |
|---|---|
| R01–R03 | 申请要素完整、项目分类在监管目录内、申请等级不超过目录上限 |
| R04–R06 | 机构存在已生效资质版本、在有效期内、状态为有效（非暂停） |
| R07–R08 | **机构资质范围包含申请分类、等级覆盖申请等级**（证书有效≠范围覆盖） |
| R09–R10 | 人员执业证时态有效、证载范围与等级覆盖申请项目 |
| R11 | 主诊资格有效且等级覆盖 |
| R12 | 注册机构为主执业机构，或持**有效跨机构备案**（备案按分类/等级限缩） |
| R13 | **双主体可执业范围交集**包含申请项目（机构侧、人员侧均覆盖） |
| R14 | 四类必备材料齐备，缺失则结论为"补交材料"而非拒绝 |

结论三态：`允许` / `拒绝`（范围不符）/ `补交材料`（材料缺失）。

## 关键不变量如何保证

- **资质时态核验**：机构资质、执业证、主诊资格均为 append-only 版本表；续期颁发新版本（旧版本保留），暂停/恢复/吊销登记在最新版本上；所有核验显式传入 `as_of` 时点，结论可复现。
- **双主体授权范围**：`scopes.py` 分别计算机构与人员范围（等级按"最高等级覆盖"解释），案件结论取交集；跨机构备案在分类与等级两个维度限缩人员范围。
- **决定证据快照**：每次核验（立案、补件、批准尝试）都向 `case_evaluations` 写入不可变的规则明细与登记事实快照；批准时再固化为授权证据。证书事后吊销不影响已存档快照。
- **防重复授权**：批准是**单事务**（重新核验→存证→机构+人员两条授权写入→案件状态翻转→事件），失败整体回滚；`grants` 上对"有效"授权建部分唯一索引，案件状态机 + 数据库约束双重兜底。暂停/备案结束使相关授权成对失效（历史行保留为`已失效`），恢复后须重新立案授予。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/qualification/`：核验后端
  - `models.py`：领域常量、异常与只读结果结构
  - `store.py`：SQLite schema（版本表/备案/案件/事件/快照/授权）
  - `scopes.py`：机构与人员可执业范围计算（纯函数）
  - `rules.py`：14 条核验规则与证据快照
  - `service.py`：登记、时态事件、案件流转、原子批准
  - `api.py`：HTTP API（标准库）
- `tools/check_contract.py`：契约摘要检查。
- `tools/demo.py`：端到端场景演示（误判识别、补件、批准、暂停、续期、备案）。
- `tests/`：契约回归 + 22 个后端/API 测试。

## 运行

```bash
# 全部测试
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests

# 场景演示
python3 tools/demo.py

# 启动 HTTP 服务
python3 -m qualification.api --db data/qual.db --port 8080
```

## API 摘要

登记类：`POST /api/institutions`、`/api/personnel`、`/api/categories`、
`/api/institutions/<id>/qualifications`（续期再次调用即新版本）、
`/api/institutions/<id>/qualification-status`（暂停/恢复）、
`/api/personnel/<id>/certs`、`/api/personnel/<id>/cert-status`、
`/api/personnel/<id>/attending`、`/api/personnel/<id>/attending-status`、
`/api/personnel/<id>/registrations`、`/api/cross-filings`。

范围与案件：

- `GET /api/institutions/<id>/scope?as_of=YYYY-MM-DD`
- `GET /api/personnel/<id>/scope?institution_id=..&as_of=..`
- `POST /api/cases`（立案即出首次核验）
- `GET /api/cases/<id>/evaluation`（当前规则解释）、`/evaluations`（历次快照）、`/timeline`（事件流）、`/grants`
- `POST /api/cases/<id>/supplement`（材料补交，追加留痕、重新核验）
- `POST /api/cases/<id>/approve`（原子批准，重复授予返回 409）、`/reject`、`/request-materials`、`/archive`
- `GET /api/grants?status=有效|已失效|全部`（授权台账）、`GET /api/evidence/<eid>`

批准成功返回机构、人员两条授权及共同的证据快照 id；任一规则不满足时返回 422 并附带全部规则解释。
