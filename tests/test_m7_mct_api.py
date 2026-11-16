"""M7 HTTP 接口测试。"""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.modules.m7_mct import store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "mct-api.sqlite")
    monkeypatch.setattr(store, "crash_before_commit", None, raising=False)
    store.init_db()
    return TestClient(app)


def rule_payload(**overrides):
    payload = {"airport": "JFK", "arrival_terminal": "T4", "departure_terminal": "T1",
               "effective_date": "2026-01-01", "min_minutes": 90}
    payload.update(overrides)
    return payload


def connection_payload(**overrides):
    payload = {"conn_key": "C1", "airport": "JFK", "arrival_terminal": "T4",
               "departure_terminal": "T1", "arrival_utc": "2026-11-16T10:00:00+00:00",
               "departure_utc": "2026-11-16T12:30:00+00:00"}
    payload.update(overrides)
    return payload


def test_mct_page_accessible(client):
    response = client.get("/mct")
    assert response.status_code == 200 and "panel" in response.text


def test_publish_list_withdraw_rule_flow(client):
    published = client.post("/api/mct/rules", json=rule_payload())
    assert published.status_code == 200 and published.json()["status"] == "active"
    rule_id = published.json()["rule_id"]

    duplicate = client.post("/api/mct/rules", json=rule_payload(min_minutes=120))
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "RULE_VERSION_EXISTS"

    listing = client.get("/api/mct/rules?airport=jfk&status=active")
    assert listing.status_code == 200 and len(listing.json()["rules"]) == 1

    withdrawn = client.post(f"/api/mct/rules/{rule_id}/withdraw")
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
    assert client.get("/api/mct/rules?status=withdrawn").json()["rules"][0]["rule_id"] == rule_id


def test_ticket_issue_retry_and_read_back(client):
    client.post("/api/mct/rules", json=rule_payload())
    payload = {"order_no": "ORD-9", "idempotency_key": "idem-api-1",
               "connections": [connection_payload()]}
    first = client.post("/api/mct/tickets", json=payload)
    assert first.status_code == 200
    ticket_no = first.json()["ticket_no"]
    assert first.json()["connections"][0]["decision"] == "VALID"

    retry = client.post("/api/mct/tickets", json=payload)
    assert retry.status_code == 200 and retry.json()["ticket_no"] == ticket_no
    assert retry.json()["replayed"] is True

    read_back = client.get(f"/api/mct/tickets/{ticket_no}")
    assert read_back.status_code == 200
    connection = read_back.json()["connections"][0]
    assert connection["wait_minutes"] == 150 and connection["rule_id"].startswith("MCT|")
    assert "required_minutes" in connection and "decision" in connection


def test_rebook_changed_and_unchanged_over_http(client):
    client.post("/api/mct/rules", json=rule_payload())
    client.post("/api/mct/rules", json=rule_payload(effective_date="2026-11-10", min_minutes=180))
    ticket_no = client.post("/api/mct/tickets", json={
        "order_no": "ORD-R", "idempotency_key": "idem-rbk-api",
        "connections": [connection_payload()]}).json()["ticket_no"]

    changed = client.post("/api/mct/tickets/rebook", json={
        "ticket_no": ticket_no, "idempotency_key": "rbk-api-1",
        "connections": [connection_payload(departure_utc="2026-11-16T12:00:00+00:00")]})
    assert changed.status_code == 200
    body = changed.json()
    assert body["changed_connections"] == ["C1"] and body["revision"] == 2
    assert body["connections"][0]["rule_id"] == "MCT|JFK|T4|T1|2026-11-10"
    assert body["connections"][0]["decision"] == "TOO_SHORT"

    unchanged = client.post("/api/mct/tickets/rebook", json={
        "ticket_no": ticket_no, "idempotency_key": "rbk-api-2",
        "connections": [connection_payload(departure_utc="2026-11-16T12:00:00+00:00")]})
    assert unchanged.json()["changed_connections"] == []
    assert unchanged.json()["connections"][0]["revision"] == 2


def test_expired_rule_rejected_on_new_purchase_and_rebook(client):
    published = client.post("/api/mct/rules", json=rule_payload()).json()
    ticket_no = client.post("/api/mct/tickets", json={
        "order_no": "ORD-E", "idempotency_key": "idem-exp",
        "connections": [connection_payload()]}).json()["ticket_no"]
    client.post(f"/api/mct/rules/{published['rule_id']}/withdraw")

    new_purchase = client.post("/api/mct/tickets", json={
        "order_no": "ORD-E2", "idempotency_key": "idem-exp-2",
        "connections": [connection_payload()]})
    assert new_purchase.status_code == 422
    assert new_purchase.json()["error"]["code"] == "RULE_EXPIRED"

    rebook = client.post("/api/mct/tickets/rebook", json={
        "ticket_no": ticket_no, "idempotency_key": "rbk-exp",
        "connections": [connection_payload(departure_utc="2026-11-16T12:00:00+00:00")]})
    assert rebook.status_code == 422 and rebook.json()["error"]["code"] == "RULE_EXPIRED"
    # 旧票回读不变
    assert client.get(f"/api/mct/tickets/{ticket_no}").json()["revision"] == 1


def test_bad_inputs_return_domain_errors(client):
    bad_airport = client.post("/api/mct/rules", json=rule_payload(airport="XX"))
    assert bad_airport.status_code == 422 and bad_airport.json()["error"]["code"] == "BAD_AIRPORT"
    missing = client.post("/api/mct/tickets", json={"order_no": "O", "idempotency_key": "k",
                                                    "connections": []})
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "EMPTY_CONNECTIONS"
    assert client.get("/api/mct/tickets/TKT|nope").status_code == 404
