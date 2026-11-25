"""补证入账：只补登尚未确认的可退时长，归属必须落在该话单号的版本范围内。"""

import json

from app.errors import DomainIssue
from app.intake.normalize import canonical
from app.refund.engine import apply_entries, claim_status


def _raw_claim(base, call_id):
    for claim in base.get("claims", []):
        if claim["call_id"] == call_id:
            return claim
    return None


def _options(raw):
    return {(item["subscriber"], item["direction"].upper())
            for item in raw.get("attribution_options", [])
            if item.get("subscriber") and item.get("direction")}


def submit(store, run_id, body):
    run = store.get(run_id)
    if run is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    payload = json.loads(run["payload"])
    base = payload.get("refund_base") or {"claims": []}
    call_id = canonical(body.get("call_id", ""))
    raw = _raw_claim(base, call_id)
    if raw is None:
        raise DomainIssue("REFUND_CLAIM_UNKNOWN", 404)

    try:
        seconds = int(canonical(body.get("seconds", "")))
    except (TypeError, ValueError):
        raise DomainIssue("REFUND_BAD_AMOUNT")
    if seconds <= 0:
        raise DomainIssue("REFUND_BAD_AMOUNT")

    subscriber = canonical(body.get("subscriber", ""))
    direction = canonical(body.get("direction", "")).upper()
    if raw["flags"]["attribution_disputed"]:
        if (subscriber, direction) not in _options(raw):
            raise DomainIssue("REFUND_ATTRIBUTION")
    elif (subscriber, direction) != (raw["subscriber"], raw["direction"]):
        raise DomainIssue("REFUND_ATTRIBUTION")

    evidence = canonical(body.get("evidence", ""))
    if not evidence:
        raise DomainIssue("REFUND_EVIDENCE_REQUIRED")

    entries = store.refund_entries(run_id)
    # 同一话单号已入账的归属必须一致，避免时长被拆分到不同号码或方向。
    for entry in entries:
        if entry["call_id"] != call_id:
            continue
        if (entry["subscriber"], entry["direction"]) != (subscriber, direction):
            raise DomainIssue("REFUND_ATTRIBUTION")
        if entry["evidence"] == evidence:
            # 同一证据是同一笔补证的重试：返回既有记录，不重复入账。
            return {"entry": entry, "created": False, "refund": apply_entries(base, entries)}

    claim = claim_status(base, entries, call_id)
    if claim is None or claim["pending_seconds"] <= 0:
        raise DomainIssue("REFUND_NOT_PENDING", 409)

    # 只补登尚未确认的部分，以可证多收为上限。
    seconds = min(seconds, claim["pending_seconds"])
    entry, created = store.add_refund_entry(
        run_id, call_id, seconds, subscriber, direction, evidence,
        canonical(body.get("note", "")),
    )
    return {"entry": entry, "created": created,
            "refund": apply_entries(base, store.refund_entries(run_id))}
