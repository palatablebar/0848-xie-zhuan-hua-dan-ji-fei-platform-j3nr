import json

from app.errors import DomainIssue
from app.review.policy import approval_allowed


def create_run(store, payload):
    row = store.create(payload["run_key"], payload["profile"], payload)
    return serialize(row, store)


def approve(store, run_id, decision, comment, expected_revision):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    payload = json.loads(row["payload"])
    if decision == "APPROVED" and not approval_allowed(payload):
        raise DomainIssue("GATE_BLOCKED", 409)
    if decision not in {"APPROVED", "HELD"}:
        raise DomainIssue("BAD_REQUEST")
    row, problem = store.review(run_id, decision, comment.strip(), expected_revision)
    if problem == "missing":
        raise DomainIssue("RUN_NOT_FOUND", 404)
    if problem == "stale":
        raise DomainIssue("STALE_REVIEW", 409)
    return serialize(row, store)


def serialize(row, store=None):
    if row is None:
        return None
    payload = json.loads(row["payload"])
    payload.update(run_id=row["id"], status=row["status"], revision=row["revision"],
                   comment=row["comment"], created_at=row["created_at"])
    base = payload.get("refund_base")
    if store is not None and base is not None:
        from app.refund.engine import apply_entries
        entries = store.refund_entries(row["id"])
        payload["refund"] = apply_entries(base, entries)
        payload["refund_entries"] = [{key: entry[key] for key in
                                      ("entry_id", "call_id", "seconds", "subscriber", "direction",
                                       "evidence", "note", "created_at")} for entry in entries]
    # 对账底稿是内部材料，只留在库里供补证复核，不对外返回。
    payload.pop("refund_base", None)
    return payload
