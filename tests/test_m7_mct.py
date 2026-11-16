"""M7 衔接规则中心：服务层全量测试（独立临时库，不触碰 M1-M6）。"""
import json
import pytest
from app.errors import DomainError
from app.modules.m7_mct import rulebook, service, snapshot, store, ticketing
from app.modules.m7_mct import rebook as rebook_service


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "mct.sqlite")
    monkeypatch.setattr(store, "crash_before_commit", None, raising=False)
    store.init_db()
    return tmp_path


def rule(airport="JFK", arr="T4", dep="T1", date_="2026-01-01", minutes=90):
    return {"airport": airport, "arrival_terminal": arr, "departure_terminal": dep,
            "effective_date": date_, "min_minutes": minutes}


def conn(arrival="2026-11-16T10:00:00+00:00", departure="2026-11-16T12:30:00+00:00",
         airport="JFK", arr="T4", dep="T1", key="C1"):
    return {"conn_key": key, "airport": airport, "arrival_terminal": arr,
            "departure_terminal": dep, "arrival_utc": arrival, "departure_utc": departure}


def ticket_payload(connections, idem="idem-1", order="ORD-1"):
    return {"order_no": order, "idempotency_key": idem, "connections": connections}


# ---------- 规则发布与选择 ----------

def test_publish_rule_versions_immutable(db):
    first = service.publish_rule(rule(minutes=90))
    assert first["rule_id"] == "MCT|JFK|T4|T1|2026-01-01" and first["status"] == "active"
    with pytest.raises(DomainError) as exc:
        service.publish_rule(rule(minutes=120))
    assert exc.value.code == "RULE_VERSION_EXISTS" and exc.value.status == 409


def test_publish_normalizes_codes_and_validates(db):
    published = service.publish_rule(rule(airport=" jfk ", arr=" t4", dep="t1"))
    assert published["airport"] == "JFK" and published["arrival_terminal"] == "T4"
    with pytest.raises(DomainError) as exc:
        service.publish_rule(rule(airport="JJ"))
    assert exc.value.code == "BAD_AIRPORT"
    with pytest.raises(DomainError) as exc:
        service.publish_rule(rule(minutes=0))
    assert exc.value.code == "BAD_MINUTES"


def test_rule_selection_picks_latest_effective(db):
    service.publish_rule(rule(date_="2026-01-01", minutes=90))
    service.publish_rule(rule(date_="2026-11-01", minutes=150))
    with store.connect() as db_conn:
        old = rulebook.resolve_rule(db_conn, "JFK", "T4", "T1", "2026-10-31")
        new = rulebook.resolve_rule(db_conn, "JFK", "T4", "T1", "2026-11-16")
    assert old["min_minutes"] == 90 and new["min_minutes"] == 150


def test_unknown_terminal_pair_has_no_rule(db):
    service.publish_rule(rule())
    with store.connect() as db_conn:
        with pytest.raises(DomainError) as exc:
            rulebook.resolve_rule(db_conn, "JFK", "T9", "T9", "2026-11-16")
    assert exc.value.code == "RULE_EXPIRED"


def test_withdraw_keeps_row_but_new_purchase_falls_back(db):
    published = service.publish_rule(rule())
    withdrawn = service.withdraw_rule(published["rule_id"])
    assert withdrawn["status"] == "withdrawn" and withdrawn["withdrawn_at"]
    # 行仍在，可回读
    assert service.list_rules(status="withdrawn")[0]["rule_id"] == published["rule_id"]
    with pytest.raises(DomainError) as exc:
        service.issue_ticket(ticket_payload([conn()]))
    assert exc.value.code == "RULE_EXPIRED"
    # 重复撤销拒绝
    with pytest.raises(DomainError) as exc:
        service.withdraw_rule(published["rule_id"])
    assert exc.value.code == "RULE_ALREADY_WITHDRAWN"


def test_withdraw_old_version_does_not_block_new_version(db):
    """撤销规则不改旧票；新购回退到仍有效的其它版本。"""
    service.publish_rule(rule(date_="2026-01-01", minutes=90))
    service.publish_rule(rule(date_="2026-11-01", minutes=150))
    service.withdraw_rule("MCT|JFK|T4|T1|2026-11-01")
    receipt = service.issue_ticket(ticket_payload([conn()], idem="idem-fallback"))
    assert receipt["connections"][0]["rule_id"] == "MCT|JFK|T4|T1|2026-01-01"
    assert receipt["connections"][0]["required_minutes"] == 90


# ---------- 出票：新票按新规、整单快照、逐连接凭据 ----------

def test_issue_ticket_uses_current_rule_and_returns_evidence(db):
    service.publish_rule(rule(date_="2026-11-01", minutes=150))
    receipt = service.issue_ticket(ticket_payload([conn()], idem="idem-new"))
    connection = receipt["connections"][0]
    assert connection["wait_minutes"] == 150
    assert connection["required_minutes"] == 150
    assert connection["rule_id"] == "MCT|JFK|T4|T1|2026-11-01"
    assert connection["decision"] == "VALID" and connection["revision"] == 1
    assert receipt["snapshot_id"].startswith("SNAP|") and receipt["accepted"] is True
    # 快照内容与连接采用的规则一致
    assert snapshot.snapshot_rule_ids(receipt["snapshot_id"]) == [connection["rule_id"]]


def test_too_short_connection_is_recorded_not_silently_dropped(db):
    service.publish_rule(rule(minutes=160))
    receipt = service.issue_ticket(ticket_payload([conn()], idem="idem-short"))
    connection = receipt["connections"][0]
    assert connection["decision"] == "TOO_SHORT" and connection["reason_code"] == "TOO_SHORT"
    assert receipt["accepted"] is False


def test_entire_order_binds_single_snapshot_even_with_many_rules(db):
    service.publish_rule(rule(date_="2026-01-01", minutes=90))
    service.publish_rule(rule(arr="T4", dep="T4", date_="2026-01-01", minutes=60))
    connections = [conn(dep="T1", key="C1"),
                   conn(dep="T4", key="C2", departure="2026-11-16T12:00:00+00:00")]
    receipt = service.issue_ticket(ticket_payload(connections, idem="idem-multi"))
    rule_ids = sorted(row["rule_id"] for row in receipt["connections"])
    assert snapshot.snapshot_rule_ids(receipt["snapshot_id"]) == rule_ids
    assert len(set(row["rule_id"] for row in receipt["connections"])) == 2


def test_one_connection_without_rule_fails_whole_order(db):
    service.publish_rule(rule())
    bad = [conn(key="C1"), conn(arr="T8", dep="T8", key="C2")]
    with pytest.raises(DomainError) as exc:
        service.issue_ticket(ticket_payload(bad, idem="idem-partial"))
    assert exc.value.code == "RULE_EXPIRED"
    # 无半票残留
    assert service.list_rules() and not _any_ticket_rows()


def _any_ticket_rows() -> bool:
    with store.connect() as db_conn:
        return db_conn.execute("SELECT COUNT(*) FROM mct_tickets").fetchone()[0] > 0


# ---------- 幂等、重试、落库中断恢复 ----------

def test_retry_same_idempotency_key_does_not_duplicate(db):
    service.publish_rule(rule())
    first = service.issue_ticket(ticket_payload([conn()], idem="idem-retry"))
    second = service.issue_ticket(ticket_payload([conn()], idem="idem-retry"))
    assert first["ticket_no"] == second["ticket_no"]
    assert second["replayed"] is True and first["replayed"] is False
    with store.connect() as db_conn:
        assert db_conn.execute("SELECT COUNT(*) FROM mct_tickets").fetchone()[0] == 1
        assert db_conn.execute("SELECT COUNT(*) FROM mct_ticket_connections").fetchone()[0] == 1


def test_idempotency_key_reused_for_other_order_rejected(db):
    service.publish_rule(rule())
    service.issue_ticket(ticket_payload([conn()], idem="idem-x", order="ORD-A"))
    with pytest.raises(DomainError) as exc:
        service.issue_ticket(ticket_payload([conn()], idem="idem-x", order="ORD-B"))
    assert exc.value.code == "IDEMPOTENCY_KEY_CONFLICT"


def test_crash_before_commit_leaves_no_partial_ticket_and_retry_recovers(db):
    service.publish_rule(rule())

    def boom():
        raise RuntimeError("simulated storage outage")

    store.crash_before_commit = boom
    with pytest.raises(RuntimeError, match="simulated storage outage"):
        service.issue_ticket(ticket_payload([conn()], idem="idem-crash"))
    store.crash_before_commit = None
    assert not _any_ticket_rows()
    with store.connect() as db_conn:
        assert db_conn.execute("SELECT COUNT(*) FROM mct_snapshots").fetchone()[0] == 0

    # 恢复后同请求重试：成功且只出一张票
    receipt = service.issue_ticket(ticket_payload([conn()], idem="idem-crash"))
    assert receipt["replayed"] is False and receipt["accepted"] is True
    again = service.issue_ticket(ticket_payload([conn()], idem="idem-crash"))
    assert again["replayed"] is True and again["ticket_no"] == receipt["ticket_no"]


# ---------- 改签：变动重验、未变保留、过期拒绝 ----------

def _issued_with_two_versions(db):
    service.publish_rule(rule(date_="2026-01-01", minutes=90))
    service.publish_rule(rule(date_="2026-11-01", minutes=150))
    receipt = service.issue_ticket(ticket_payload([conn()], idem="idem-rebook"))
    return receipt


def test_rebook_changed_connection_revalidates_under_new_rule(db):
    receipt = _issued_with_two_versions(db)
    original = receipt["connections"][0]
    assert original["rule_id"].endswith("2026-11-01") and original["required_minutes"] == 150

    # 机场再上调：发布 2026-11-10 生效的 180 分钟
    service.publish_rule(rule(date_="2026-11-10", minutes=180))
    changed = {**conn(), "departure_utc": "2026-11-16T12:00:00+00:00"}  # 等待降到 120
    result = service.rebook({"ticket_no": receipt["ticket_no"], "idempotency_key": "rbk-1",
                             "connections": [changed]})
    row = result["connections"][0]
    assert result["changed_connections"] == ["C1"]
    assert row["rule_id"] == "MCT|JFK|T4|T1|2026-11-10"
    assert row["required_minutes"] == 180 and row["wait_minutes"] == 120
    assert row["decision"] == "TOO_SHORT" and row["revision"] == 2
    assert result["revision"] == 2


def test_rebook_unchanged_connection_keeps_original_credential(db):
    receipt = _issued_with_two_versions(db)
    original = receipt["connections"][0]
    # 提交完全相同的连接：未变，凭据原样
    result = service.rebook({"ticket_no": receipt["ticket_no"], "idempotency_key": "rbk-keep",
                             "connections": [conn()]})
    row = result["connections"][0]
    assert result["changed_connections"] == []
    assert row["rule_id"] == original["rule_id"]
    assert row["required_minutes"] == original["required_minutes"]
    assert row["decision"] == original["decision"] and row["revision"] == 1


def test_rebook_mixed_changed_and_unchanged_connections(db):
    service.publish_rule(rule(date_="2026-01-01", minutes=90))
    service.publish_rule(rule(arr="T4", dep="T4", date_="2026-01-01", minutes=60))
    connections = [conn(dep="T1", key="C1"),
                   conn(dep="T4", key="C2", departure="2026-11-16T12:00:00+00:00")]
    receipt = service.issue_ticket(ticket_payload(connections, idem="idem-mix"))
    kept_rule = next(r for r in receipt["connections"] if r["conn_key"] == "C2")["rule_id"]

    service.publish_rule(rule(date_="2026-11-10", minutes=180))
    changed_c1 = {**conn(dep="T1", key="C1"), "departure_utc": "2026-11-16T13:00:00+00:00"}
    result = service.rebook({"ticket_no": receipt["ticket_no"], "idempotency_key": "rbk-mix",
                             "connections": [changed_c1, conn(dep="T4", key="C2",
                                                              departure="2026-11-16T12:00:00+00:00")]})
    rows = {row["conn_key"]: row for row in result["connections"]}
    assert result["changed_connections"] == ["C1"]
    assert rows["C1"]["rule_id"] == "MCT|JFK|T4|T1|2026-11-10" and rows["C1"]["revision"] == 2
    assert rows["C2"]["rule_id"] == kept_rule and rows["C2"]["revision"] == 1


def test_rebook_rejects_expired_rule_version(db):
    receipt = _issued_with_two_versions(db)
    service.withdraw_rule("MCT|JFK|T4|T1|2026-11-01")
    service.withdraw_rule("MCT|JFK|T4|T1|2026-01-01")
    changed = {**conn(), "departure_utc": "2026-11-16T12:00:00+00:00"}
    with pytest.raises(DomainError) as exc:
        service.rebook({"ticket_no": receipt["ticket_no"], "idempotency_key": "rbk-expired",
                        "connections": [changed]})
    assert exc.value.code == "RULE_EXPIRED"
    # 整单回滚：票仍停在修订 1，旧凭据不变
    untouched = service.read_ticket(receipt["ticket_no"])
    assert untouched["revision"] == 1
    assert untouched["connections"][0]["rule_id"] == "MCT|JFK|T4|T1|2026-11-01"


def test_rebook_unknown_connection_and_missing_ticket_rejected(db):
    receipt = _issued_with_two_versions(db)
    with pytest.raises(DomainError) as exc:
        service.rebook({"ticket_no": receipt["ticket_no"], "idempotency_key": "rbk-u",
                        "connections": [{**conn(), "conn_key": "NOPE"}]})
    assert exc.value.code == "UNKNOWN_CONNECTION"
    with pytest.raises(DomainError) as exc:
        service.rebook({"ticket_no": "TKT|ghost", "idempotency_key": "rbk-g",
                        "connections": [conn()]})
    assert exc.value.code == "TICKET_NOT_FOUND" and exc.value.status == 404


def test_rebook_is_idempotent_and_crash_recoverable(db, monkeypatch):
    receipt = _issued_with_two_versions(db)
    service.publish_rule(rule(date_="2026-11-10", minutes=180))
    changed = {**conn(), "departure_utc": "2026-11-16T12:00:00+00:00"}
    payload = {"ticket_no": receipt["ticket_no"], "idempotency_key": "rbk-crash",
               "connections": [changed]}

    def boom():
        raise RuntimeError("storage outage during rebook")

    store.crash_before_commit = boom
    with pytest.raises(RuntimeError):
        service.rebook(payload)
    store.crash_before_commit = None
    assert service.read_ticket(receipt["ticket_no"])["revision"] == 1

    recovered = service.rebook(payload)
    assert recovered["revision"] == 2
    replay = service.rebook(payload)
    assert replay["revision"] == 2 and replay["replayed"] is True
    with store.connect() as db_conn:
        assert db_conn.execute("SELECT COUNT(*) FROM mct_rebooks").fetchone()[0] == 1


# ---------- 旧票不受规则变化影响 ----------

def test_old_ticket_keeps_issuance_promise_after_rule_hike(db):
    """已出票联程始终按出票承诺核验：回读不随新规改变。"""
    service.publish_rule(rule(date_="2026-01-01", minutes=90))
    receipt = service.issue_ticket(ticket_payload([conn()], idem="idem-old"))
    old = receipt["connections"][0]
    assert old["required_minutes"] == 90 and old["decision"] == "VALID"

    service.publish_rule(rule(date_="2026-11-01", minutes=180))  # 机场上调
    service.withdraw_rule("MCT|JFK|T4|T1|2026-01-01")            # 旧版撤销
    reread = service.read_ticket(receipt["ticket_no"])
    kept = reread["connections"][0]
    assert kept["rule_id"] == old["rule_id"]
    assert kept["required_minutes"] == 90 and kept["decision"] == "VALID"
    # 新购则按新规，150 分钟等待 < 180 判不足
    new_ticket = service.issue_ticket(ticket_payload([conn()], idem="idem-new-era"))
    assert new_ticket["connections"][0]["required_minutes"] == 180
    assert new_ticket["connections"][0]["decision"] == "TOO_SHORT"


# ---------- 并发：发布与出票 ----------

def test_concurrent_publish_and_ticket_each_land_once(db):
    service.publish_rule(rule(date_="2026-01-01", minutes=90))
    service.issue_ticket(ticket_payload([conn()], idem="idem-base"))

    results, errors = [], []

    def publish_hike():
        try:
            results.append(service.publish_rule(rule(date_="2026-12-01", minutes=200)))
        except Exception as exc:  # 同键并发第二次必被唯一约束拒绝
            errors.append(exc)

    import threading
    threads = [threading.Thread(target=publish_hike) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 1 and len(errors) == 3
    assert all(getattr(exc, "code", "") == "RULE_VERSION_EXISTS" for exc in errors)


def test_concurrent_rebooks_with_same_idempotency_collapse_to_one(db):
    receipt = _issued_with_two_versions(db)
    service.publish_rule(rule(date_="2026-11-10", minutes=180))
    changed = {**conn(), "departure_utc": "2026-11-16T12:00:00+00:00"}
    payload = {"ticket_no": receipt["ticket_no"], "idempotency_key": "rbk-race",
               "connections": [changed]}
    outcomes = []

    def do_rebook():
        try:
            outcomes.append(service.rebook(payload))
        except Exception as exc:
            outcomes.append(exc)

    import threading
    threads = [threading.Thread(target=do_rebook) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(isinstance(item, dict) for item in outcomes)
    assert all(item["revision"] == 2 for item in outcomes)
    with store.connect() as db_conn:
        assert db_conn.execute("SELECT COUNT(*) FROM mct_rebooks").fetchone()[0] == 1


def test_concurrent_tickets_with_same_idempotency_collapse_to_one(db):
    service.publish_rule(rule())
    outcomes = []

    def buy():
        try:
            outcomes.append(service.issue_ticket(ticket_payload([conn()], idem="idem-race")))
        except Exception as exc:
            outcomes.append(exc)

    import threading
    threads = [threading.Thread(target=buy) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ticket_nos = {item["ticket_no"] for item in outcomes if isinstance(item, dict)}
    assert len(ticket_nos) == 1
    with store.connect() as db_conn:
        assert db_conn.execute("SELECT COUNT(*) FROM mct_tickets").fetchone()[0] == 1
        assert db_conn.execute("SELECT COUNT(*) FROM mct_ticket_connections").fetchone()[0] == 1


# ---------- 回读 ----------

def test_read_back_reports_duration_rule_and_decision_per_connection(db):
    service.publish_rule(rule(minutes=90))
    service.publish_rule(rule(arr="T4", dep="T4", minutes=60))
    issued = service.issue_ticket(ticket_payload([
        conn(key="C1"),
        conn(key="C2", arr="T4", dep="T4", departure="2026-11-16T11:20:00+00:00"),
    ], idem="idem-read"))
    back = service.read_ticket(issued["ticket_no"])
    assert {row["conn_key"] for row in back["connections"]} == {"C1", "C2"}
    first = next(row for row in back["connections"] if row["conn_key"] == "C1")
    assert first["wait_minutes"] == 150 and first["rule_id"].startswith("MCT|")
    assert {"wait_minutes", "rule_id", "required_minutes", "decision"} <= set(first)
    assert back["snapshot_id"] == issued["snapshot_id"]


def test_read_missing_ticket_returns_404(db):
    with pytest.raises(DomainError) as exc:
        service.read_ticket("TKT|missing")
    assert exc.value.code == "TICKET_NOT_FOUND" and exc.value.status == 404


def test_snapshot_content_hash_is_deterministic(db):
    assert snapshot.snapshot_hash(["a", "b"]) == snapshot.snapshot_hash(["b", "a"])
    assert snapshot.snapshot_hash(["a"]) != snapshot.snapshot_hash(["a", "b"])


def test_list_rules_filters_by_airport_and_status(db):
    service.publish_rule(rule(airport="JFK"))
    service.publish_rule(rule(airport="LHR", arr="T3", dep="T5"))
    service.withdraw_rule("MCT|LHR|T3|T5|2026-01-01")
    assert len(service.list_rules(airport="jfk")) == 1
    assert {row["status"] for row in service.list_rules(status="withdrawn")} == {"withdrawn"}
    assert len(service.list_rules(airport="JFK", status="active")) == 1
