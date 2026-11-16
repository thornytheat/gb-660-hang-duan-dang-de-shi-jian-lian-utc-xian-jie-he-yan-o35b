"""出票接口：新购联程按当前有效规则验收，整单绑定同一规则快照。

可靠性口径：
- idempotency_key 唯一约束 + 事务，重试不重复出票；
- 落库中断（crash_before_commit 钩子）整体回滚，恢复后重试得到同一票号；
- 规则解析、快照建绑、票与连接凭据在同一事务内完成，不会出现半票；
- 发布与出票并发时只认已提交的 active 规则版本（BEGIN IMMEDIATE）。
"""
import sqlite3
from hashlib import sha256
from app.modules.m5_connections.windows import waiting_minutes
from app.modules.m5_connections.rules import connection_result
from . import contracts, snapshot
from .contracts import MAX_CONNECTION_MINUTES, fail, now_iso
from .store import commit, connect, init_db


def _normalise_connections(raw_connections: list[dict]) -> list[dict]:
    if not raw_connections:
        fail("EMPTY_CONNECTIONS", "至少需要一个连接")
    normalised = []
    seen = set()
    for index, raw in enumerate(raw_connections, 1):
        conn_key = str(raw.get("conn_key") or f"C{index}").strip()
        if conn_key in seen:
            fail("DUPLICATE_CONN_KEY", f"连接键重复：{conn_key}")
        seen.add(conn_key)
        arrival = contracts.utc_instant(raw["arrival_utc"])
        departure = contracts.utc_instant(raw["departure_utc"])
        normalised.append({
            "conn_key": conn_key,
            "airport": contracts.airport(raw["airport"]),
            "arrival_terminal": contracts.terminal(raw["arrival_terminal"]),
            "departure_terminal": contracts.terminal(raw["departure_terminal"]),
            "arrival_utc": arrival,
            "departure_utc": departure,
        })
    return normalised


def _evaluate(conn: dict, rule: dict) -> dict:
    # 复用 M5 既有口径；本模块不改航段时刻解析，等待时长完全来自调用方时刻。
    wait = waiting_minutes(conn["arrival_utc"], conn["departure_utc"])
    result = connection_result(wait, rule["min_minutes"], MAX_CONNECTION_MINUTES)
    decision = result["status"]
    return {
        "conn_key": conn["conn_key"],
        "airport": conn["airport"],
        "arrival_terminal": conn["arrival_terminal"],
        "departure_terminal": conn["departure_terminal"],
        "arrival_utc": conn["arrival_utc"].isoformat(),
        "departure_utc": conn["departure_utc"].isoformat(),
        "wait_minutes": wait,
        "rule_id": rule["rule_id"],
        "required_minutes": rule["min_minutes"],
        "decision": decision,
        "reason_code": "" if decision == "VALID" else decision,
        "revision": 1,
    }


def _ticket_no(idempotency_key: str) -> str:
    return "TKT|" + sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


def _read_ticket(db: sqlite3.Connection, ticket_no: str) -> dict:
    ticket = db.execute("SELECT * FROM mct_tickets WHERE ticket_no = ?", (ticket_no,)).fetchone()
    conns = db.execute(
        "SELECT * FROM mct_ticket_connections WHERE ticket_no = ? ORDER BY conn_key", (ticket_no,)
    ).fetchall()
    return {
        "ticket_no": ticket["ticket_no"],
        "order_no": ticket["order_no"],
        "idempotency_key": ticket["idempotency_key"],
        "snapshot_id": ticket["snapshot_id"],
        "revision": ticket["revision"],
        "issued_at": ticket["issued_at"],
        "accepted": all(row["decision"] == "VALID" for row in conns),
        "connections": [dict(row) for row in conns],
    }


def issue_ticket(payload: dict) -> dict:
    init_db()
    order_no = str(payload.get("order_no") or "").strip()
    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if not order_no:
        fail("EMPTY_ORDER_NO", "order_no 必填")
    if not idempotency_key:
        fail("EMPTY_IDEMPOTENCY_KEY", "idempotency_key 必填")
    connections = _normalise_connections(payload.get("connections") or [])
    ticket_no = _ticket_no(idempotency_key)

    db = connect()
    try:
        # IMMEDIATE 立即拿写锁：与规则发布并发时，两边串行化，快照只绑定已提交版本。
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT * FROM mct_tickets WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            if existing["order_no"] != order_no:
                fail("IDEMPOTENCY_KEY_CONFLICT", "幂等键已用于其它订单", 409)
            db.rollback()
            receipt = _read_ticket(db, existing["ticket_no"])
            receipt["replayed"] = True
            return receipt

        rules = snapshot.resolve_for_connections(db, connections)
        snapshot_id = snapshot.get_or_create_snapshot(db, [rule["rule_id"] for rule in rules])
        evaluated = [_evaluate(conn, rule) for conn, rule in zip(connections, rules)]

        db.execute(
            "INSERT INTO mct_tickets VALUES (?, ?, ?, ?, 1, ?)",
            (ticket_no, order_no, idempotency_key, snapshot_id, now_iso()),
        )
        db.executemany(
            "INSERT INTO mct_ticket_connections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            [(ticket_no, row["conn_key"], row["airport"], row["arrival_terminal"],
              row["departure_terminal"], row["arrival_utc"], row["departure_utc"],
              row["wait_minutes"], row["rule_id"], row["required_minutes"],
              row["decision"], row["reason_code"]) for row in evaluated],
        )
        commit(db)  # 故障注入点：抛异常则整体回滚，无半票残留
    except sqlite3.IntegrityError:
        db.rollback()
        # 并发下唯一约束兜底：语义等同幂等重放，返回既有票而非报错
        db_ro = connect()
        try:
            existing = db_ro.execute(
                "SELECT * FROM mct_tickets WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is None:
                raise
            if existing["order_no"] != order_no:
                fail("IDEMPOTENCY_KEY_CONFLICT", "幂等键已用于其它订单", 409)
            receipt = _read_ticket(db_ro, existing["ticket_no"])
        finally:
            db_ro.close()
        receipt["replayed"] = True
        return receipt
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    db = connect()
    try:
        receipt = _read_ticket(db, ticket_no)
        receipt["replayed"] = False
    finally:
        db.close()
    return receipt


def read_ticket(ticket_no: str) -> dict:
    """回读：每个连接的时长、采用规则与判定，附整单快照与修订版本。"""
    init_db()
    with connect() as db:
        row = db.execute("SELECT * FROM mct_tickets WHERE ticket_no = ?", (ticket_no,)).fetchone()
        if row is None:
            fail("TICKET_NOT_FOUND", "票号不存在", 404)
        receipt = _read_ticket(db, ticket_no)
    receipt["replayed"] = False
    return receipt
