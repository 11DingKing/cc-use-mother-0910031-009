"""SQLite 存储层：建表、连接与低级数据访问。

设计要点：

* 资质（机构许可、人员执业证、主诊资格）采用**版本表**：每次新发/换发写入
  新版本，旧版本置为 ``已换发``，永不覆盖，保证历史决定可回放。
* 暂停/恢复、备案停止等**时态事件**独立成表，带起止生效时间。
* 授权表上建部分唯一索引，从数据库层面防止同一案件/同一机构-地点-项目被
  重复授予。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 旧版本索引：与“撤销后可重新批准”的决定史冲突，升级时移除
DROP INDEX IF EXISTS ux_decisions_one_approval;

CREATE TABLE IF NOT EXISTS institutions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persons (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 机构执业许可（资质版本）
CREATE TABLE IF NOT EXISTS institution_license_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    institution_id TEXT NOT NULL REFERENCES institutions(id),
    version INTEGER NOT NULL,
    license_no TEXT NOT NULL,
    status TEXT NOT NULL,                 -- 有效/已换发/注销
    scope_categories TEXT NOT NULL,       -- JSON: 科目代码列表
    level INTEGER NOT NULL,               -- 机构资质等级，数字越小越高
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    superseded_at TEXT,
    UNIQUE(institution_id, version)
);

-- 机构注册地点
CREATE TABLE IF NOT EXISTS institution_sites (
    id TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL REFERENCES institutions(id),
    address TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,                        -- NULL 表示长期有效
    created_at TEXT NOT NULL
);

-- 项目分类（科目/项目目录）
CREATE TABLE IF NOT EXISTS project_categories (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    parent_code TEXT REFERENCES project_categories(code),
    level INTEGER NOT NULL,               -- 项目等级：1最高
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- 人员执业证（资质版本）
CREATE TABLE IF NOT EXISTS person_certificate_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL REFERENCES persons(id),
    version INTEGER NOT NULL,
    cert_no TEXT NOT NULL,
    status TEXT NOT NULL,                 -- 有效/已换发/注销
    practice_scope TEXT NOT NULL,         -- JSON: 执业范围代码列表
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    superseded_at TEXT,
    UNIQUE(person_id, version)
);

-- 执业证暂停/恢复时态事件
CREATE TABLE IF NOT EXISTS person_cert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cert_version_id INTEGER NOT NULL REFERENCES person_certificate_versions(id),
    event TEXT NOT NULL,                  -- 暂停/恢复
    effective_from TEXT NOT NULL,
    effective_to TEXT,                    -- NULL 表示暂停中
    reason TEXT,
    created_at TEXT NOT NULL
);

-- 主诊资格（资质版本）
CREATE TABLE IF NOT EXISTS attending_qualification_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL REFERENCES persons(id),
    version INTEGER NOT NULL,
    title TEXT NOT NULL,                  -- 主诊科目/资格名称
    scope_categories TEXT NOT NULL,       -- JSON
    level INTEGER NOT NULL,               -- 可主诊的最低项目等级
    status TEXT NOT NULL,                 -- 有效/注销
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    UNIQUE(person_id, version)
);

-- 人员注册机构与跨机构备案（时态）
CREATE TABLE IF NOT EXISTS person_registrations (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES persons(id),
    institution_id TEXT NOT NULL REFERENCES institutions(id),
    kind TEXT NOT NULL,                   -- 主执业机构/跨机构备案
    status TEXT NOT NULL,                 -- 有效/停止
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    created_at TEXT NOT NULL
);

-- 核验案件
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL REFERENCES institutions(id),
    site_id TEXT NOT NULL REFERENCES institution_sites(id),
    person_id TEXT NOT NULL REFERENCES persons(id),
    project_code TEXT NOT NULL REFERENCES project_categories(code),
    status TEXT NOT NULL,                 -- 登记/待核验/处置中/已决定/已归档
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 案件申请材料（授权证据）
CREATE TABLE IF NOT EXISTS case_evidence (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    kind TEXT NOT NULL,                   -- 申请材料/补交材料
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,           -- 材料结构化内容快照
    submitted_at TEXT NOT NULL
);

-- 评估快照：每次计算的结果，append-only，原决定永不被修改
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES cases(id),
    trigger TEXT NOT NULL,                -- 登记评估/证书续期/证书暂停/跨机构备案/材料补交/人工复评
    evaluated_at TEXT NOT NULL,
    overall_verdict TEXT NOT NULL,        -- 允许/拒绝
    result_json TEXT NOT NULL,            -- 完整规则结论与证据快照
    UNIQUE(case_id, id)
);

-- 案件决定（每个案件至多一条有效批准；拒绝可多次留痕）
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES cases(id),
    result TEXT NOT NULL,                 -- 批准/拒绝
    reason TEXT NOT NULL,
    evaluation_id INTEGER NOT NULL REFERENCES evaluations(id),
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_decisions_idempotency
    ON decisions(idempotency_key);
-- “每案件至多一个有效授权”由 grants 表的部分唯一索引 ux_grants_case 保证；
-- decisions 允许保留多条决定（如：批准→撤销授权→重新批准）。

-- 授权（批准后原子写入）
CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES cases(id),
    institution_id TEXT NOT NULL REFERENCES institutions(id),
    site_id TEXT NOT NULL REFERENCES institution_sites(id),
    person_id TEXT NOT NULL REFERENCES persons(id),
    project_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '有效',  -- 有效/撤销
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    decision_id INTEGER NOT NULL REFERENCES decisions(id)
);
-- 同一案件不可重复授权
CREATE UNIQUE INDEX IF NOT EXISTS ux_grants_case
    ON grants(case_id) WHERE status = '有效';
-- 同一“机构+地点+人员+项目”不可重复有效授权
CREATE UNIQUE INDEX IF NOT EXISTS ux_grants_subject_project
    ON grants(institution_id, site_id, person_id, project_code)
    WHERE status = '有效';

-- 案件事件日志
CREATE TABLE IF NOT EXISTS case_event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES cases(id),
    event TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    """当前 UTC 时间，秒级，便于测试与回放。"""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def connect(db_path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    # timeout 让写事务在锁竞争时等待而非立即失败
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value: str) -> Any:
    return json.loads(value)


def fetchall(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, tuple(params)))
