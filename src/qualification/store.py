"""SQLite 持久化层。

仅使用标准库 ``sqlite3``。所有写操作经过服务层显式事务，
授权表上的部分唯一索引与案件状态机构成"防重复授权"的最后一道防线。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 机构 ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS institutions (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- 人员 ---------------------------------------------------------------------
CREATE TABLE personnel (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- 项目分类目录 -------------------------------------------------------------
CREATE TABLE project_categories (
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    max_grade       TEXT NOT NULL CHECK (max_grade IN ('一级','二级','三级','四级'))
);

-- 机构资质版本（append-only：续期/变更登记新版本，不覆盖旧版本） -----------
CREATE TABLE institution_qual_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    institution_id  TEXT NOT NULL REFERENCES institutions(id),
    version_no      INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('有效','暂停')),
    valid_from      TEXT NOT NULL,
    valid_to        TEXT NOT NULL,
    scope_json      TEXT NOT NULL,          -- [{"category_code","grade"}]
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    UNIQUE (institution_id, version_no)
);

-- 人员执业证版本（append-only：续期颁发新版本） ----------------------------
CREATE TABLE personnel_cert_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    personnel_id    TEXT NOT NULL REFERENCES personnel(id),
    version_no      INTEGER NOT NULL,
    license_no      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('有效','暂停','吊销')),
    valid_from      TEXT NOT NULL,
    valid_to        TEXT NOT NULL,
    scope_json      TEXT NOT NULL,          -- 证载执业范围
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    UNIQUE (personnel_id, version_no)
);

-- 主诊（主持）资格版本 ------------------------------------------------------
CREATE TABLE attending_qual_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    personnel_id    TEXT NOT NULL REFERENCES personnel(id),
    version_no      INTEGER NOT NULL,
    category_code   TEXT NOT NULL REFERENCES project_categories(code),
    max_grade       TEXT NOT NULL CHECK (max_grade IN ('一级','二级','三级','四级')),
    status          TEXT NOT NULL CHECK (status IN ('有效','暂停','吊销')),
    valid_from      TEXT NOT NULL,
    valid_to        TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    UNIQUE (personnel_id, version_no, category_code)
);

-- 执业注册地点（主执业机构；变更注册写新行，旧行 closed） -------------------
CREATE TABLE registrations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    personnel_id    TEXT NOT NULL REFERENCES personnel(id),
    institution_id  TEXT NOT NULL REFERENCES institutions(id),
    valid_from      TEXT NOT NULL,
    valid_to        TEXT,                   -- NULL 表示当前有效
    created_at      TEXT NOT NULL
);

-- 跨机构备案 ----------------------------------------------------------------
CREATE TABLE cross_filings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    personnel_id    TEXT NOT NULL REFERENCES personnel(id),
    home_institution_id  TEXT NOT NULL REFERENCES institutions(id),
    host_institution_id  TEXT NOT NULL REFERENCES institutions(id),
    category_code   TEXT NOT NULL REFERENCES project_categories(code),
    max_grade       TEXT NOT NULL CHECK (max_grade IN ('一级','二级','三级','四级')),
    status          TEXT NOT NULL CHECK (status IN ('有效','已结束')),
    valid_from      TEXT NOT NULL,
    valid_to        TEXT,
    created_at      TEXT NOT NULL,
    CHECK (home_institution_id <> host_institution_id)
);

-- 授权证据 ------------------------------------------------------------------
CREATE TABLE evidence (
    id              TEXT PRIMARY KEY,
    evidence_type   TEXT NOT NULL,
    title           TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- 核验案件 ------------------------------------------------------------------
CREATE TABLE cases (
    id              TEXT PRIMARY KEY,
    institution_id  TEXT NOT NULL REFERENCES institutions(id),
    personnel_id    TEXT NOT NULL REFERENCES personnel(id),
    category_code   TEXT NOT NULL REFERENCES project_categories(code),
    grade           TEXT NOT NULL CHECK (grade IN ('一级','二级','三级','四级')),
    state           TEXT NOT NULL CHECK (state IN ('登记','待核验','处置中','已决定','已归档')),
    decision        TEXT CHECK (decision IS NULL OR decision IN ('允许','拒绝','补交材料')),
    decided_at      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 案件材料（补交只追加；同一类型可有多条历史提交，原记录永不覆盖） ---------
CREATE TABLE IF NOT EXISTS case_materials (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id         TEXT NOT NULL REFERENCES cases(id),
    material_type   TEXT NOT NULL,
    file_ref        TEXT NOT NULL,
    submitted_at    TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT ''
);

-- 案件事件流（append-only：续期/暂停/备案/补交全部留痕，原决定不变） -------
CREATE TABLE case_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id         TEXT NOT NULL REFERENCES cases(id),
    action          TEXT NOT NULL,
    detail_json     TEXT NOT NULL DEFAULT '{}',
    actor           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

-- 每次核验的规则明细与证据快照（历史可追溯、永不改写） ----------------------
CREATE TABLE case_evaluations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id         TEXT NOT NULL REFERENCES cases(id),
    as_of           TEXT NOT NULL,
    decision        TEXT NOT NULL,
    summary         TEXT NOT NULL,
    rules_json      TEXT NOT NULL,
    snapshot_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- 已批准授权（仅由案件批准动作在单事务内写入） ------------------------------
-- status: 有效 / 已失效（证书暂停、备案结束等会使授权失效；历史行保留）
CREATE TABLE grants (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type    TEXT NOT NULL CHECK (subject_type IN ('机构','人员')),
    subject_id      TEXT NOT NULL,
    institution_id  TEXT NOT NULL REFERENCES institutions(id),
    personnel_id    TEXT NOT NULL REFERENCES personnel(id),
    category_code   TEXT NOT NULL REFERENCES project_categories(code),
    grade           TEXT NOT NULL CHECK (grade IN ('一级','二级','三级','四级')),
    case_id         TEXT NOT NULL REFERENCES cases(id),
    evidence_id     TEXT NOT NULL REFERENCES evidence(id),
    status          TEXT NOT NULL DEFAULT '有效' CHECK (status IN ('有效','已失效')),
    void_reason     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    voided_at       TEXT
);

-- 防重复授予：同一主体-机构-人员-分类-等级只允许一条"有效"授权；
-- 已失效授权作为历史保留，不阻碍暂停恢复后重新立案授予。
CREATE UNIQUE INDEX IF NOT EXISTS ux_grants_active
ON grants (subject_type, subject_id, institution_id, personnel_id, category_code, grade)
WHERE status = '有效';

CREATE INDEX IF NOT EXISTS idx_cases_state ON cases(state);
CREATE INDEX IF NOT EXISTS idx_events_case ON case_events(case_id, id);
CREATE INDEX IF NOT EXISTS idx_evals_case ON case_evaluations(case_id, id);
"""


def connect(db_path: str | Path = ":memory:") -> sqlite3.Connection:
    """打开数据库连接并初始化 schema。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn
