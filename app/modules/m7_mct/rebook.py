"""改签接口：只有变动连接按当前有效规则重验，未变连接保留出票凭据。

- 变动判定按航站楼对与两端时刻比较，航段时刻解析仍由调用方负责；
- 变动连接选不到 active 规则（撤销/缺失）即 RULE_EXPIRED 整单拒绝；
- 未变连接不动行、不换规则，rule_id/required_minutes/decision 原样保留；
- 每次成功改签产生新修订号与新快照，原凭据行随修订留痕，不清除。
"""
import sqlite3
import sqlite3
from hashlib import sha256
from . import contracts, snapshot, ticketing
from .contracts import fail, now_iso
from .store import commit, connect, init_db


def _is_changed(stored: sqlite3.Row, candidate: dict) -> bool:
    return (
        stored["arrival_terminal"] != candidate["arrival_terminal"]
        or stored["departure_terminal"] != candidate["departure_terminal"]
        or stored["arrival_utc"] != candidate["arrival_utc"].isoformat()
        or stored["departure_utc"] != candidate["departure_utc"].isoformat()
    )


def _rebook_id(idempotency_key: str) -> str:
    return "RBK|" + sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


def rebook(payload: dict) -> dict:
    init_db()
    ticket_no = str(payload.get("ticket_no") or "").strip()
    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if not ticket_no:
        fail("EMPTY_TICKET_NO", "ticket_no 必填")
    if not idempotency_key:
        fail("EMPTY_IDEMPOTENCY_KEY", "idempotency_key 必填")

    raw_changes = payload.get("connections") or []
    candidates: dict[str, dict] = {}
    for index, raw in enumerate(raw_changes, 1):
        conn_key = str(raw.get("conn_key") or f"C{index}").strip()
        if conn_key in candidates:
            fail("DUPLICATE_CONN_KEY", f"连接键重复：{conn_key}")
        arrival = contracts.utc_instant(raw["arrival_utc"])
        departure = contracts.utc_instant(raw["departure_utc"])
        candidates[conn_key] = {
            "conn_key": conn_key,
            "airport": contracts.airport(raw["airport"]),
            "arrival_terminal": contracts.terminal(raw["arrival_terminal"]),
            "departure_terminal": contracts.terminal(raw["departure_terminal"]),
            "arrival_utc": arrival,
            "departure_utc": departure,
        }

    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        ticket = db.execute("SELECT * FROM mct_tickets WHERE ticket_no = ?", (ticket_no,)).fetchone()
        if ticket is None:
            fail("TICKET_NOT_FOUND", "票号不存在", 404)

        prior = db.execute(
            "SELECT idempotency_key FROM mct_rebooks WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if prior is not None:
            db.rollback()
            receipt = ticketing.read_ticket(ticket_no)
            receipt["replayed"] = True
            return receipt

        stored_rows = db.execute(
            "SELECT * FROM mct_ticket_connections WHERE ticket_no = ? ORDER BY conn_key", (ticket_no,)
        ).fetchall()
        stored = {row["conn_key"]: row for row in stored_rows}
        unknown = sorted(set(candidates) - set(stored))
        if unknown:
            fail("UNKNOWN_CONNECTION", f"票中不存在的连接键：{','.join(unknown)}")

        changed_keys: list[str] = []
        new_rules: list[dict] = []
        evaluations: dict[str, dict] = {}
        for conn_key, candidate in candidates.items():
            row = stored[conn_key]
            if not _is_changed(row, candidate):
                continue  # 未变连接：保留原凭据，不重新选规则、不重新判定
            changed_keys.append(conn_key)
            on_date = candidate["arrival_utc"].isoformat()[:10]
            # 拒绝过期版本：撤销或缺规则在此抛 RULE_EXPIRED，整单回滚
            rule = db.execute(
                "SELECT * FROM mct_rule_versions "
                "WHERE airport = ? AND arrival_terminal = ? AND departure_terminal = ? "
                "AND effective_date <= ? AND status = 'active' "
                "ORDER BY effective_date DESC, published_at DESC LIMIT 1",
                (candidate["airport"], candidate["arrival_terminal"],
                 candidate["departure_terminal"], on_date),
            ).fetchone()
            if rule is None:
                fail("RULE_EXPIRED",
                     f"变动连接 {conn_key} 在 {on_date} 无有效规则版本（缺失或已撤销）")
            new_rules.append(dict(rule))
            evaluations[conn_key] = ticketing._evaluate(candidate, dict(rule))

        # 快照同时覆盖：变动连接采用的新规则 + 未变连接沿用的原规则
        unchanged_rule_ids = [stored[key]["rule_id"] for key in stored if key not in changed_keys]
        all_rule_ids = [rule["rule_id"] for rule in new_rules] + unchanged_rule_ids
        new_snapshot_id = snapshot.get_or_create_snapshot(db, all_rule_ids)
        new_revision = ticket["revision"] + 1

        for conn_key, evaluation in evaluations.items():
            db.execute(
                "UPDATE mct_ticket_connections SET airport = ?, arrival_terminal = ?, "
                "departure_terminal = ?, arrival_utc = ?, departure_utc = ?, wait_minutes = ?, "
                "rule_id = ?, required_minutes = ?, decision = ?, reason_code = ?, revision = ? "
                "WHERE ticket_no = ? AND conn_key = ?",
                (evaluation["airport"], evaluation["arrival_terminal"],
                 evaluation["departure_terminal"], evaluation["arrival_utc"],
                 evaluation["departure_utc"], evaluation["wait_minutes"],
                 evaluation["rule_id"], evaluation["required_minutes"],
                 evaluation["decision"], evaluation["reason_code"], new_revision,
                 ticket_no, conn_key),
            )
        db.execute(
            "UPDATE mct_tickets SET snapshot_id = ?, revision = ? WHERE ticket_no = ?",
            (new_snapshot_id, new_revision, ticket_no),
        )
        db.execute(
            "INSERT INTO mct_rebooks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_rebook_id(idempotency_key), ticket_no, idempotency_key, new_revision,
             new_snapshot_id, ",".join(changed_keys), now_iso()),
        )
        commit(db)
    except sqlite3.IntegrityError:
        db.rollback()
        # 并发同幂等键：后提交者按重放处理，返回当前票态
        receipt = ticketing.read_ticket(ticket_no)
        receipt["replayed"] = True
        return receipt
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    receipt = ticketing.read_ticket(ticket_no)
    receipt["replayed"] = False
    receipt["changed_connections"] = changed_keys
    return receipt
