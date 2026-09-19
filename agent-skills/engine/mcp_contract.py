"""Structured MCP availability and safety responses.

The response deliberately distinguishes a live result from a local snapshot.
No database result is ever synthesized from a deployment manifest.
"""
from __future__ import annotations

import datetime as _dt
import json

from .contracts import ContractError, validate_mcp_response


def _stamp():
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def response(*, ok, source, data, availability, redacted=False, profile=None, environment=None,
             error_code=None, error_message=None, as_of=None):
    item = {
        "ok": bool(ok),
        "source": str(source),
        "data_availability": str(availability),
        "as_of": as_of or _stamp(),
        "is_live": availability == "live",
        "redacted": bool(redacted),
        "data": data,
    }
    if profile is not None:
        item["profile"] = str(profile)
    if environment is not None:
        item["environment"] = str(environment)
    if error_code is not None:
        item["error_code"] = str(error_code)
    if error_message is not None:
        item["error_message"] = str(error_message)[:500]
    validate_mcp_response(item)
    return item


def text_response(**kwargs):
    return json.dumps(response(**kwargs), ensure_ascii=False, sort_keys=True)


def offline_database_unavailable(source="database-broker", profile=None, environment=None):
    return text_response(
        ok=False,
        source=source,
        data=None,
        availability="offline_unavailable",
        profile=profile,
        environment=environment,
        error_code="OFFLINE_DATABASE_UNAVAILABLE",
        error_message="当前无数据库连接，也没有可验证的离线数据库快照；未伪造查询结果。",
    )


def validate_no_fake_database_result(item):
    """Fail closed if an offline DB response claims to contain live rows."""
    if item.get("source") == "database-broker" and item.get("data_availability") != "live":
        if item.get("ok") or item.get("data") not in (None, [], {}):
            raise ContractError("离线数据库响应不能伪造可用查询结果")
    return True
