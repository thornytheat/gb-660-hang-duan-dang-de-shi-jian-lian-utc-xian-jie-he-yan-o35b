"""规则发布、撤销与按机场/航站楼/生效日选规则。

规则内容不可变：同一自然键（机场+航站楼对+生效日）只能发布一次；
撤销只把 status 置为 withdrawn，行与历史快照保留，绝不删除。
"""
import sqlite3
from . import contracts
from .contracts import fail, now_iso
from .store import connect, init_db


def _row_to_rule(row: sqlite3.Row) -> dict:
    return {
        "rule_id": row["rule_id"],
        "airport": row["airport"],
        "arrival_terminal": row["arrival_terminal"],
        "departure_terminal": row["departure_terminal"],
        "effective_date": row["effective_date"],
        "min_minutes": row["min_minutes"],
        "status": row["status"],
        "published_at": row["published_at"],
        "withdrawn_at": row["withdrawn_at"],
    }


def publish_rule(payload: dict) -> dict:
    """发布一条 MCT 规则版本。同自然键重复发布（含已撤销）一律拒绝。"""
    init_db()
    code = contracts.airport(payload["airport"])
    arr_t = contracts.terminal(payload["arrival_terminal"])
    dep_t = contracts.terminal(payload["departure_terminal"])
    effective = contracts.iso_date(payload["effective_date"])
    minutes = contracts.positive_minutes(payload.get("min_minutes"), "min_minutes")
    rule_id = f"MCT|{code}|{arr_t}|{dep_t}|{effective}"
    with connect() as db:
        existing = db.execute(
            "SELECT status FROM mct_rule_versions WHERE rule_id = ?", (rule_id,)
        ).fetchone()
        if existing is not None:
            fail("RULE_VERSION_EXISTS", f"规则版本已存在（状态 {existing['status']}），版本内容不可变", 409)
        try:
            db.execute(
                "INSERT INTO mct_rule_versions VALUES (?, ?, ?, ?, ?, ?, 'active', ?, NULL)",
                (rule_id, code, arr_t, dep_t, effective, minutes, now_iso()),
            )
        except sqlite3.IntegrityError:
            # 并发发布同自然键：唯一约束兜底，转译为稳定的领域错误码
            fail("RULE_VERSION_EXISTS", "规则版本已存在（并发写入被唯一约束拒绝），版本内容不可变", 409)
        row = db.execute("SELECT * FROM mct_rule_versions WHERE rule_id = ?", (rule_id,)).fetchone()
    return _row_to_rule(row)


def withdraw_rule(rule_id: str) -> dict:
    """撤销规则：不删行、不改旧票；新购选规则时跳过 withdrawn。"""
    init_db()
    with connect() as db:
        row = db.execute("SELECT * FROM mct_rule_versions WHERE rule_id = ?", (rule_id,)).fetchone()
        if row is None:
            fail("RULE_NOT_FOUND", "规则版本不存在", 404)
        if row["status"] == "withdrawn":
            fail("RULE_ALREADY_WITHDRAWN", "规则已撤销，重复撤销被拒绝", 409)
        db.execute(
            "UPDATE mct_rule_versions SET status = 'withdrawn', withdrawn_at = ? WHERE rule_id = ?",
            (now_iso(), rule_id),
        )
        row = db.execute("SELECT * FROM mct_rule_versions WHERE rule_id = ?", (rule_id,)).fetchone()
    return _row_to_rule(row)


def get_rule(rule_id: str) -> dict:
    init_db()
    with connect() as db:
        row = db.execute("SELECT * FROM mct_rule_versions WHERE rule_id = ?", (rule_id,)).fetchone()
    if row is None:
        fail("RULE_NOT_FOUND", "规则版本不存在", 404)
    return _row_to_rule(row)


def list_rules(airport: str | None = None, status: str | None = None) -> list[dict]:
    init_db()
    sql = "SELECT * FROM mct_rule_versions WHERE 1=1"
    params: list = []
    if airport:
        sql += " AND airport = ?"
        params.append(contracts.airport(airport))
    if status:
        if status not in ("active", "withdrawn"):
            fail("BAD_STATUS", "status 仅支持 active/withdrawn")
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY airport, arrival_terminal, departure_terminal, effective_date"
    with connect() as db:
        return [_row_to_rule(row) for row in db.execute(sql, params).fetchall()]


def resolve_rule(db: sqlite3.Connection, airport: str, arr_terminal: str,
                 dep_terminal: str, on_date: str, *, allow_withdrawn: bool = False) -> dict:
    """在给定事务/连接内选规则：生效日 <= on_date 的最新 active 版本。

    仅精确航站楼对匹配；无匹配抛 RULE_EXPIRED（新购/改签重验遇到撤销或缺失均由此出口拒绝）。
    """
    sql = ("SELECT * FROM mct_rule_versions "
           "WHERE airport = ? AND arrival_terminal = ? AND departure_terminal = ? "
           "AND effective_date <= ?")
    if not allow_withdrawn:
        sql += " AND status = 'active'"
    sql += " ORDER BY effective_date DESC, published_at DESC LIMIT 1"
    row = db.execute(sql, (airport, arr_terminal, dep_terminal, on_date)).fetchone()
    if row is None:
        fail(
            "RULE_EXPIRED",
            f"{airport} {arr_terminal}->{dep_terminal} 在 {on_date} 无有效规则版本（缺失或已撤销）",
        )
    return _row_to_rule(row)
