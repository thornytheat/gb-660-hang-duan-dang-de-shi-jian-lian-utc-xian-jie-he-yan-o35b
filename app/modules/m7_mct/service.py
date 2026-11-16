"""M7 服务门面：规则发布/撤销/查询、出票、改签、回读。"""
from . import rebook as rebook_service
from . import rulebook, ticketing


def publish_rule(payload: dict) -> dict:
    return rulebook.publish_rule(payload)


def withdraw_rule(rule_id: str) -> dict:
    return rulebook.withdraw_rule(rule_id)


def list_rules(airport: str | None = None, status: str | None = None) -> list[dict]:
    return rulebook.list_rules(airport, status)


def issue_ticket(payload: dict) -> dict:
    return ticketing.issue_ticket(payload)


def read_ticket(ticket_no: str) -> dict:
    return ticketing.read_ticket(ticket_no)


def rebook(payload: dict) -> dict:
    return rebook_service.rebook(payload)
