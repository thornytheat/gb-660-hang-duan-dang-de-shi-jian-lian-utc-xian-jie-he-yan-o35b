"""M7 衔接规则中心持久层。

独立于 app.store：只服务 MCT 版本规则、快照与出票/改签凭据。
不提供任何 DELETE 路径——撤销仅置状态，旧规则与旧票凭据永久留存（不扩展清除）。
"""
import sqlite3
from app.config import DATABASE_PATH

# 故障注入点：测试可 monkeypatch 为抛异常的函数，模拟落库中断。
crash_before_commit = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS mct_rule_versions (
    rule_id          TEXT PRIMARY KEY,
    airport          TEXT NOT NULL,
    arrival_terminal TEXT NOT NULL,
    departure_terminal TEXT NOT NULL,
    effective_date   TEXT NOT NULL,
    min_minutes      INTEGER NOT NULL CHECK (min_minutes > 0),
    status           TEXT NOT NULL CHECK (status IN ('active', 'withdrawn')),
    published_at     TEXT NOT NULL,
    withdrawn_at     TEXT,
    UNIQUE (airport, arrival_terminal, departure_terminal, effective_date)
);
CREATE TABLE IF NOT EXISTS mct_snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mct_snapshot_items (
    snapshot_id TEXT NOT NULL REFERENCES mct_snapshots(snapshot_id),
    rule_id     TEXT NOT NULL REFERENCES mct_rule_versions(rule_id),
    PRIMARY KEY (snapshot_id, rule_id)
);
CREATE TABLE IF NOT EXISTS mct_tickets (
    ticket_no        TEXT PRIMARY KEY,
    order_no         TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    snapshot_id      TEXT NOT NULL REFERENCES mct_snapshots(snapshot_id),
    revision         INTEGER NOT NULL DEFAULT 1,
    issued_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mct_ticket_connections (
    ticket_no        TEXT NOT NULL REFERENCES mct_tickets(ticket_no),
    conn_key         TEXT NOT NULL,
    airport          TEXT NOT NULL,
    arrival_terminal TEXT NOT NULL,
    departure_terminal TEXT NOT NULL,
    arrival_utc      TEXT NOT NULL,
    departure_utc    TEXT NOT NULL,
    wait_minutes     INTEGER NOT NULL,
    rule_id          TEXT NOT NULL,
    required_minutes INTEGER NOT NULL,
    decision         TEXT NOT NULL,
    reason_code      TEXT NOT NULL DEFAULT '',
    revision         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (ticket_no, conn_key)
);
CREATE TABLE IF NOT EXISTS mct_rebooks (
    rebook_id        TEXT PRIMARY KEY,
    ticket_no        TEXT NOT NULL REFERENCES mct_tickets(ticket_no),
    idempotency_key  TEXT NOT NULL UNIQUE,
    revision         INTEGER NOT NULL,
    snapshot_id      TEXT NOT NULL REFERENCES mct_snapshots(snapshot_id),
    changed_conn_keys TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
"""


def connect():
    from pathlib import Path
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    with connect() as db:
        db.executescript(SCHEMA)


def commit(db):
    """统一提交入口，crash_before_commit 钩子用于落库中断恢复测试。"""
    if crash_before_commit is not None:
        crash_before_commit()
    db.commit()
