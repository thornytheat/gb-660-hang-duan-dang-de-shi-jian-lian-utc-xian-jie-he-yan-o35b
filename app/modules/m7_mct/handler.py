from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from app.web import page
from . import service

router = APIRouter()

ROWS = [
    {"key": "MCT|JFK|T4|T1|2026-01-01", "status": "active", "value": "120"},
    {"key": "MCT|JFK|T4|T1|2026-11-01", "status": "active", "value": "150"},
    {"key": "MCT|PVG|T1|T2|2025-06-01", "status": "withdrawn", "value": "90"},
]


@router.get("/mct", response_class=HTMLResponse)
def view(request: Request):
    return page(request, "m7.html", "衔接规则中心", "/api/mct/rules", ROWS)


# ---- 规则发布 / 撤销 / 查询 ----

@router.post("/api/mct/rules")
def publish(payload: dict):
    return service.publish_rule(payload)


@router.post("/api/mct/rules/{rule_id}/withdraw")
def withdraw(rule_id: str):
    return service.withdraw_rule(rule_id)


@router.get("/api/mct/rules")
def list_rules(airport: str | None = None, status: str | None = None):
    return {"rules": service.list_rules(airport, status)}


# ---- 出票 / 改签 / 回读 ----

@router.post("/api/mct/tickets")
def issue(payload: dict):
    return service.issue_ticket(payload)


@router.get("/api/mct/tickets/{ticket_no}")
def read(ticket_no: str):
    return service.read_ticket(ticket_no)


@router.post("/api/mct/tickets/rebook")
def rebook(payload: dict):
    return service.rebook(payload)
