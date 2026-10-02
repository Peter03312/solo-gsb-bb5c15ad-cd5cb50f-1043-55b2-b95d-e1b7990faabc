"""SQLite 存储层：连接管理、建表、初始化与演示数据。

写入全部串行化（进程内写锁 + 立即事务），保证“更正一次提交、
失败回滚不留部分改动”；开启外键约束与 WAL。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import allergens as al

SCHEMA = """
CREATE TABLE IF NOT EXISTS allergen (
    code        TEXT PRIMARY KEY,
    name_zh     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS material_lot (
    lot_no      TEXT PRIMARY KEY,
    material    TEXT NOT NULL,
    allergens   TEXT NOT NULL DEFAULT '[]',  -- JSON 数组，过敏原代码
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS line (
    line_id     TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch (
    batch_no         TEXT PRIMARY KEY,
    line_id          TEXT NOT NULL REFERENCES line(line_id),
    seq              INTEGER NOT NULL,
    material_lots    TEXT NOT NULL DEFAULT '[]',   -- JSON 数组，原料批号
    declared         TEXT NOT NULL DEFAULT '[]',   -- JSON 数组，声明过敏原
    cleaned          TEXT NOT NULL DEFAULT '[]',   -- JSON 数组，本次清洁确认清除的过敏原
    status           TEXT NOT NULL DEFAULT 'ACTIVE', -- ACTIVE=有效 / VOIDED=已作废（保留可查，批次号不可复用）
    voided_reason    TEXT,                          -- 作废原因（作废时非空，只追加不改）
    voided_at        TEXT,                          -- 作废时间
    created_at       TEXT NOT NULL,
    UNIQUE(line_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_batch_line_seq ON batch(line_id, seq);
-- idx_batch_status 引用作废功能新增的 status 列；为避免旧库在建表阶段
-- 引用尚未迁移的列，该索引在 initialize() 完成列迁移后通过 _migrate_indexes 幂等创建。

CREATE TABLE IF NOT EXISTS batch_material (
    batch_no  TEXT NOT NULL REFERENCES batch(batch_no),
    lot_no    TEXT NOT NULL REFERENCES material_lot(lot_no),
    PRIMARY KEY (batch_no, lot_no)
);

CREATE INDEX IF NOT EXISTS idx_batch_material_lot ON batch_material(lot_no);

-- 原料批号待检锁定：每批号一行当前状态；只在“实际变化”时更新
CREATE TABLE IF NOT EXISTS material_lot_lock (
    lot_no      TEXT PRIMARY KEY REFERENCES material_lot(lot_no),
    locked      INTEGER NOT NULL,              -- 1=待检锁定中，0=已解除
    reason      TEXT NOT NULL,                 -- 最近一次“实际状态变化”填写的原因
    version     INTEGER NOT NULL DEFAULT 0,    -- 实际状态变化次数（重复提交相同状态不递增）
    updated_at  TEXT NOT NULL
);

-- 批号锁定/解除的只追加历史（每次实际变化一条）
CREATE TABLE IF NOT EXISTS material_lot_lock_version (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_no      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    locked      INTEGER NOT NULL,              -- 本次变化后的状态
    action      TEXT NOT NULL,                 -- LOCK / UNLOCK
    reason      TEXT NOT NULL,
    changed_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lot_version_lot
    ON material_lot_lock_version(lot_no, version);

-- 每批“出货资格”当前快照：仅由该批直接使用的原料批号锁定状态决定，
-- 不沿产线残留传递；批号锁定状态实际变化即递增（即便资格结论未翻转）
CREATE TABLE IF NOT EXISTS shipment_eligibility (
    batch_no      TEXT PRIMARY KEY REFERENCES batch(batch_no),
    version       INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL,               -- SHIPPABLE / BLOCKED
    locked_lots   TEXT NOT NULL DEFAULT '[]',  -- JSON：直接使用且当前锁定的原料批号
    reasons       TEXT NOT NULL DEFAULT '[]',  -- JSON：资格原因条目
    updated_at    TEXT NOT NULL
);

-- 出货资格历史版本（只追加）
CREATE TABLE IF NOT EXISTS shipment_eligibility_version (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no    TEXT NOT NULL,
    version     INTEGER NOT NULL,
    status      TEXT NOT NULL,
    locked_lots TEXT NOT NULL DEFAULT '[]',
    reasons     TEXT NOT NULL DEFAULT '[]',
    trigger     TEXT NOT NULL,   -- REGISTER / LOT_LOCK / LOT_UNLOCK / CORRECT_BATCH / UPGRADE_INIT
    changed_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ship_version_batch
    ON shipment_eligibility_version(batch_no, version);

-- 每个批次当前裁决快照
CREATE TABLE IF NOT EXISTS adjudication (
    batch_no      TEXT PRIMARY KEY REFERENCES batch(batch_no),
    version       INTEGER NOT NULL DEFAULT 0,
    actual        TEXT NOT NULL DEFAULT '[]',   -- JSON：实际可能含有的过敏原
    undeclared    TEXT NOT NULL DEFAULT '[]',   -- JSON：实际含但未声明
    disposition   TEXT NOT NULL,                -- RELEASE / HOLD
    reasons       TEXT NOT NULL DEFAULT '[]',   -- JSON：原因条目
    sources       TEXT NOT NULL DEFAULT '{}',   -- JSON：过敏原 -> [来源说明]
    decided_at    TEXT NOT NULL
);

-- 裁决历史版本（只追加，用于审计与查询）
CREATE TABLE IF NOT EXISTS adjudication_version (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no      TEXT NOT NULL,
    line_id       TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    version       INTEGER NOT NULL,
    actual        TEXT NOT NULL DEFAULT '[]',
    undeclared    TEXT NOT NULL DEFAULT '[]',
    disposition   TEXT NOT NULL,
    reasons       TEXT NOT NULL DEFAULT '[]',
    sources       TEXT NOT NULL DEFAULT '{}',
    trigger       TEXT NOT NULL,                -- REGISTER / CORRECT_BATCH / CORRECT_LOT / VOID_BATCH / MOVE_BATCH（line_id/seq 为该版裁决当时的归属，换线不改写历史）
    decided_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_version_batch
    ON adjudication_version(batch_no, version);

-- 出货核验单：一张单据关联多个已登记生产批次，逐批保存裁决快照
CREATE TABLE IF NOT EXISTS verification (
    verification_no  TEXT PRIMARY KEY,
    status           TEXT NOT NULL,             -- DRAFT / PENDING_REVIEW / CONFIRMED
    revision         INTEGER NOT NULL DEFAULT 1, -- 单据修订号：创建=1，逐批快照写入（重新核验/修订批次清单）或确认写入确认快照时 +1；用于修订的乐观并发校验
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    confirmed_at     TEXT
);

-- 核验单逐批快照：创建/重新核验时记录当时的裁决版本与结论、出货资格版本与状态
CREATE TABLE IF NOT EXISTS verification_batch (
    verification_no      TEXT NOT NULL REFERENCES verification(verification_no),
    batch_no             TEXT NOT NULL REFERENCES batch(batch_no),
    position             INTEGER NOT NULL,      -- 按提交顺序保序
    snapshot_version     INTEGER NOT NULL,
    snapshot_disposition TEXT NOT NULL,         -- RELEASE / HOLD
    snapshot_actual      TEXT NOT NULL DEFAULT '[]',
    snapshot_undeclared  TEXT NOT NULL DEFAULT '[]',
    snapshot_reasons     TEXT NOT NULL DEFAULT '[]',
    snapshot_ship_version INTEGER,              -- 出货资格版本（旧库升级前创建的单据为 NULL）
    snapshot_ship_status  TEXT,                 -- SHIPPABLE / BLOCKED
    snapshot_locked_lots  TEXT NOT NULL DEFAULT '[]',
    snapshot_ship_reasons TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (verification_no, batch_no)
);

CREATE INDEX IF NOT EXISTS idx_vbatch_batch ON verification_batch(batch_no);

-- 历次确认（只追加；单据转待复核/重新核验后可再次确认，历史确认不覆盖）
CREATE TABLE IF NOT EXISTS verification_confirmation (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    verification_no  TEXT NOT NULL REFERENCES verification(verification_no),
    confirmed_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vconf_verification
    ON verification_confirmation(verification_no);

-- 每次确认时逐批快照的不可变副本（裁决版本 + 出货资格版本）
CREATE TABLE IF NOT EXISTS verification_confirmation_batch (
    confirmation_id      INTEGER NOT NULL REFERENCES verification_confirmation(id),
    verification_no      TEXT NOT NULL REFERENCES verification(verification_no),
    position             INTEGER NOT NULL,
    batch_no             TEXT NOT NULL,
    snapshot_version     INTEGER NOT NULL,
    snapshot_disposition TEXT NOT NULL,
    snapshot_actual      TEXT NOT NULL DEFAULT '[]',
    snapshot_undeclared  TEXT NOT NULL DEFAULT '[]',
    snapshot_reasons     TEXT NOT NULL DEFAULT '[]',
    snapshot_ship_version INTEGER,
    snapshot_ship_status  TEXT,
    snapshot_locked_lots  TEXT NOT NULL DEFAULT '[]',
    snapshot_ship_reasons TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_vcb_confirmation
    ON verification_confirmation_batch(confirmation_id);

-- 装车交接（整单发运）：凭唯一交接号对一张“仍为已确认且修订号匹配”的出货核验单
-- 登记整单发运。交接记录只追加、不可变——后续更正、待复核或清单修订一律不改写它。
CREATE TABLE IF NOT EXISTS shipment_handover (
    handover_no           TEXT PRIMARY KEY,
    verification_no       TEXT NOT NULL REFERENCES verification(verification_no),
    -- 交接时冻结的单号与修订号：之后单据修订号再推进也不影响本记录
    verification_revision INTEGER NOT NULL,
    -- 交接所依据的“最近一次确认”
    confirmation_id       INTEGER NOT NULL REFERENCES verification_confirmation(id),
    confirmed_at          TEXT NOT NULL,
    batch_count           INTEGER NOT NULL,
    handed_over_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_handover_verification
    ON shipment_handover(verification_no);

-- 交接时逐批冻结的“最近一次确认快照”不可变副本（裁决版本/结论 + 出货资格版本/状态）。
-- 交接要求当前裁决/出货资格版本与之一致且结论分别为 RELEASE / SHIPPABLE。
CREATE TABLE IF NOT EXISTS shipment_handover_batch (
    handover_no           TEXT NOT NULL REFERENCES shipment_handover(handover_no),
    position              INTEGER NOT NULL,      -- 按确认清单顺序保序
    batch_no              TEXT NOT NULL REFERENCES batch(batch_no),
    confirmation_id       INTEGER NOT NULL REFERENCES verification_confirmation(id),
    snapshot_version      INTEGER NOT NULL,
    snapshot_disposition  TEXT NOT NULL,         -- RELEASE / HOLD
    snapshot_actual       TEXT NOT NULL DEFAULT '[]',
    snapshot_undeclared   TEXT NOT NULL DEFAULT '[]',
    snapshot_reasons      TEXT NOT NULL DEFAULT '[]',
    snapshot_ship_version INTEGER NOT NULL,
    snapshot_ship_status  TEXT NOT NULL,         -- SHIPPABLE / BLOCKED
    snapshot_locked_lots  TEXT NOT NULL DEFAULT '[]',
    snapshot_ship_reasons TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (handover_no, batch_no)
);

CREATE INDEX IF NOT EXISTS idx_hbatch_batch ON shipment_handover_batch(batch_no);

-- 每批全局唯一的“已发运”占用：一旦在某交接记录中发运，即不可再被任何交接占用。
-- PRIMARY KEY(batch_no) 在数据库层兜底“任一批次已在其他交接记录中发运则整单拒绝”。
CREATE TABLE IF NOT EXISTS shipment_handover_occupancy (
    batch_no       TEXT PRIMARY KEY REFERENCES batch(batch_no),
    handover_no    TEXT NOT NULL REFERENCES shipment_handover(handover_no),
    position       INTEGER NOT NULL,
    handed_over_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hoccupancy_handover
    ON shipment_handover_occupancy(handover_no);
"""

# 初始化空库时写入的参考原料批号（不影响裁决的演示样例）
SEED_MATERIAL_LOTS = [
    ("RM-MILK-001", "全脂奶粉", ["milk"]),
    ("RM-FLOUR-001", "小麦粉", ["gluten"]),
    ("RM-NUT-001", "花生酱", ["peanut"]),
    ("RM-CACAO-001", "可可块", []),
]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: str) -> Any:
    return json.loads(value)


class Store:
    """线程安全的 SQLite 封装。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._write_lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """立即开启写事务；异常回滚，正常提交。进程内串行。

        注意：SQLite 的 executescript 会隐式提交事务，因此本上下文内
        只允许 execute/executemany；DDL 建表请在 autocommit 状态下执行。
        """
        assert self._conn is not None
        conn = self._conn
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                # 事务可能已被隐式提交（理论上不应发生），忽略重复回滚
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        assert self._conn is not None
        with self._write_lock:
            yield self._conn

    def is_fresh(self) -> bool:
        """库中是否尚无过敏原字典（即是否需要初始化）。"""
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='allergen'"
        )
        if cur.fetchone()["n"] == 0:
            return True
        cur = self._conn.execute("SELECT COUNT(*) AS n FROM allergen")
        return cur.fetchone()["n"] == 0


# 旧库（早于批号锁定功能）升级时需要补齐的列：
# (表名, 列名, 列定义)。逐列检查 sqlite_master，幂等添加。
_MIGRATION_COLUMNS = [
    ("batch", "status", "TEXT NOT NULL DEFAULT 'ACTIVE'"),
    ("batch", "voided_reason", "TEXT"),
    ("batch", "voided_at", "TEXT"),
    ("verification_batch", "snapshot_ship_version", "INTEGER"),
    ("verification_batch", "snapshot_ship_status", "TEXT"),
    ("verification_batch", "snapshot_locked_lots", "TEXT NOT NULL DEFAULT '[]'"),
    ("verification_batch", "snapshot_ship_reasons", "TEXT NOT NULL DEFAULT '[]'"),
    ("verification_confirmation_batch", "snapshot_ship_version", "INTEGER"),
    ("verification_confirmation_batch", "snapshot_ship_status", "TEXT"),
    ("verification_confirmation_batch", "snapshot_locked_lots", "TEXT NOT NULL DEFAULT '[]'"),
    ("verification_confirmation_batch", "snapshot_ship_reasons", "TEXT NOT NULL DEFAULT '[]'"),
    ("verification", "revision", "INTEGER NOT NULL DEFAULT 1"),
]


def _migrate_indexes(conn) -> None:
    """旧库升级：补齐后续版本新增的索引（IF NOT EXISTS，幂等）。"""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_batch_status ON batch(status)")


def _migrate_columns(conn) -> None:
    for table, column, decl in _MIGRATION_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _backfill_eligibility(store: Store) -> None:
    """旧库升级：为缺少出货资格记录的既有批次补 v1 快照（按当前锁定状态计算）。

    仅在无任何出货资格记录时执行，幂等；不改变既有核验单状态
    （旧单据的出货资格快照列保持 NULL，确认时按“版本不符”要求重新核验）。
    """
    from datetime import UTC, datetime

    with store.write() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM batch").fetchone()["n"]
        if total == 0:
            return
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM shipment_eligibility"
        ).fetchone()["n"]
        if existing > 0:
            return
        now = datetime.now(UTC).isoformat()
        rows = conn.execute(
            """
            SELECT b.batch_no AS batch_no,
                   COALESCE(json_group_array(ml.lot_no), '[]') AS lot_list
            FROM batch b
            JOIN batch_material bm ON bm.batch_no = b.batch_no
            JOIN material_lot_lock ml ON ml.lot_no = bm.lot_no AND ml.locked = 1
            GROUP BY b.batch_no
            """
        ).fetchall()
        locked_by_batch = {r["batch_no"]: loads(r["lot_list"]) for r in rows}
        for r in conn.execute("SELECT batch_no FROM batch ORDER BY batch_no").fetchall():
            locked_lots = locked_by_batch.get(r["batch_no"], [])
            status = "BLOCKED" if locked_lots else "SHIPPABLE"
            reasons: list[Any]
            if locked_lots:
                reasons = [
                    {
                        "code": "MATERIAL_LOT_LOCKED",
                        "lot_no": lot,
                        "message": f"直接使用的原料批号 {lot} 处于待检锁定状态，不得通过出货核验。",
                    }
                    for lot in locked_lots
                ]
            else:
                reasons = [
                    {
                        "code": "SHIP_ELIGIBLE",
                        "message": "直接使用的原料批号均未处于待检锁定状态。",
                    }
                ]
            conn.execute(
                """INSERT INTO shipment_eligibility
                   (batch_no, version, status, locked_lots, reasons, updated_at)
                   VALUES (?, 1, ?, ?, ?, ?)""",
                (r["batch_no"], status, dumps(locked_lots), dumps(reasons), now),
            )
            conn.execute(
                """INSERT INTO shipment_eligibility_version
                   (batch_no, version, status, locked_lots, reasons, trigger, changed_at)
                   VALUES (?, 1, ?, ?, ?, 'UPGRADE_INIT', ?)""",
                (r["batch_no"], status, dumps(locked_lots), dumps(reasons), now),
            )


def initialize(store: Store, seed_demo: bool = False) -> None:
    """幂等初始化：建表、写入过敏原字典；全新库可附带演示原料批号。

    建表 DDL 在 autocommit 状态下执行（executescript 会隐式提交，
    不能放在写事务内）；字典与演示数据写入放在单个事务中。
    """
    from datetime import UTC, datetime

    assert store._conn is not None
    conn = store._conn
    fresh = store.is_fresh()
    # DDL：autocommit 状态执行，IF NOT EXISTS 保证幂等；
    # 旧库升级时也必须执行，以便补齐后续版本新增的表与列。
    conn.executescript(SCHEMA)
    _migrate_columns(conn)
    # 新索引引用迁移后新增的列：必须在列迁移补齐后再建（旧库升级场景）
    _migrate_indexes(conn)
    _backfill_eligibility(store)
    with store.write():
        conn.executemany(
            "INSERT OR IGNORE INTO allergen(code, name_zh) VALUES (?, ?)",
            list(al.ALLERGENS.items()),
        )
        # 演示数据仅在全新空库写入；过敏原字典每次幂等补齐
        if seed_demo and fresh:
            now = datetime.now(UTC).isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO line(line_id, created_at) VALUES (?, ?)",
                ("L1", now),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO material_lot"
                "(lot_no, material, allergens, updated_at) VALUES (?, ?, ?, ?)",
                [
                    (lot_no, material, dumps(allergen_codes), now)
                    for lot_no, material, allergen_codes in SEED_MATERIAL_LOTS
                ],
            )
