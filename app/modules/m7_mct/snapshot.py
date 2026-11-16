"""整单规则快照：一次出票/改签涉及的全部规则绑定同一快照。

发布与出票并发时，规则在单个事务内逐条解析（只认提交后的 active 版本），
快照内容哈希相同则复用同一 snapshot_id，保证“整单绑定同一规则快照”。
"""
import json
import sqlite3
from hashlib import sha256
from . import rulebook
from .contracts import now_iso
from .store import connect, init_db


def _connection_date(arrival_utc) -> str:
    # 连接日按到达时刻的 UTC 日期取（半开日期口径，与 docs 数据字典一致）。
    if hasattr(arrival_utc, "date"):
        return arrival_utc.date().isoformat()
    return str(arrival_utc)[:10]


def resolve_for_connections(db: sqlite3.Connection, connections: list[dict]) -> list[dict]:
    """事务内解析每个连接的规则；任一连接无有效规则则整单失败，不产生半票。"""
    resolved = []
    for conn in connections:
        on_date = _connection_date(conn["arrival_utc"])
        rule = rulebook.resolve_rule(
            db, conn["airport"], conn["arrival_terminal"],
            conn["departure_terminal"], on_date,
        )
        resolved.append(rule)
    return resolved


def snapshot_hash(rule_ids: list[str]) -> str:
    body = json.dumps(sorted(rule_ids), separators=(",", ":"), ensure_ascii=False)
    return sha256(body.encode("utf-8")).hexdigest()


def get_or_create_snapshot(db: sqlite3.Connection, rule_ids: list[str]) -> str:
    """调用方必须持有事务。同内容哈希复用快照，绝不重复建行。"""
    content_hash = snapshot_hash(rule_ids)
    existing = db.execute(
        "SELECT snapshot_id FROM mct_snapshots WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    if existing is not None:
        return existing["snapshot_id"]
    snapshot_id = f"SNAP|{content_hash[:12]}"
    db.execute(
        "INSERT INTO mct_snapshots (snapshot_id, content_hash, created_at) VALUES (?, ?, ?)",
        (snapshot_id, content_hash, now_iso()),
    )
    db.executemany(
        "INSERT INTO mct_snapshot_items (snapshot_id, rule_id) VALUES (?, ?)",
        [(snapshot_id, rule_id) for rule_id in sorted(set(rule_ids))],
    )
    return snapshot_id


def snapshot_rule_ids(snapshot_id: str) -> list[str]:
    init_db()
    with connect() as db:
        rows = db.execute(
            "SELECT rule_id FROM mct_snapshot_items WHERE snapshot_id = ? ORDER BY rule_id",
            (snapshot_id,),
        ).fetchall()
    return [row["rule_id"] for row in rows]
