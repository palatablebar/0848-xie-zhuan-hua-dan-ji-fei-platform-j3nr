from decimal import Decimal

from app.domain.calculate import calculate_rows
from app.domain.prepare import prepare_rows
from app.refund.engine import apply_entries, build_base
from tests.domain_cases import CLEAN


def case(rows):
    items, issues = prepare_rows(rows)
    assert issues == []
    return items


def base_for(rows):
    return build_base({"conflicts": []}, case(rows))


def base_from_conflicts(rows):
    return build_base({"conflicts": [{"key": rows[0]["call_id"], "rows": rows}]}, [])


def clean_row(**changes):
    item = dict(CLEAN[0])
    item.update(changes)
    return item


# 应计口径


def test_refund_reuses_billing_merge_rule():
    prepared = case(CLEAN)
    refund = apply_entries(build_base({"conflicts": []}, prepared), [])
    billed = sum(Decimal(str(item["billable_seconds"])) for item in calculate_rows(prepared))
    assert refund["total_overcharge_seconds"] == 0
    assert billed == Decimal("420")


def test_single_overcharge_is_confirmed_immediately():
    rows = [clean_row(call_id="R1", end_at="2026-11-20 10:02:00", charged_seconds="240")]
    refund = apply_entries(base_for(rows), [])
    claim = refund["claims"][0]
    assert claim["call_id"] == "R1"
    assert claim["confirmed_seconds"] == 120
    assert claim["pending_seconds"] == 0
    assert claim["status"] == "CONFIRMED"


def test_confirmed_amount_uses_integer_minutes():
    rows = [clean_row(call_id="R1", end_at="2026-11-20 10:02:00", charged_seconds="240",
                      rate_per_min_cents="25")]
    refund = apply_entries(base_for(rows), [])
    assert refund["confirmed_amount_cents"] == 120 * 25 // 60


def test_refund_never_goes_negative_on_undercharge():
    rows = [clean_row(call_id="R1", charged_seconds="30")]
    refund = apply_entries(base_for(rows), [])
    assert refund["claims"] == []
    assert refund["confirmed_seconds"] == 0


def test_merged_group_allocates_overcharge_to_individual_calls():
    rows = [clean_row(call_id="M1", charged_seconds="120"),
            clean_row(call_id="M2", charged_seconds="180")]
    refund = apply_entries(base_for(rows), [])
    # A|OUT 合并应计 120，已计 300，多收 180，落到具体话单号。
    assert refund["total_overcharge_seconds"] == 180
    assert sum(claim["confirmed_seconds"] for claim in refund["claims"]) == 180
    assert all(claim["call_id"] in {"M1", "M2"} for claim in refund["claims"])


def test_overlap_only_overcharge_is_still_allocated():
    # 两段完全重叠，单条各自不多收，合并后多收 60 秒。
    start = "2026-11-20 10:00:00"
    rows = [clean_row(call_id="O1", start_at=start, end_at="2026-11-20 10:01:00", charged_seconds="60"),
            clean_row(call_id="O2", start_at=start, end_at="2026-11-20 10:01:00", charged_seconds="60")]
    refund = apply_entries(base_for(rows), [])
    assert refund["total_overcharge_seconds"] == 60
    assert sum(claim["confirmed_seconds"] for claim in refund["claims"]) == 60


# 同一话单号冲突版本：只确认两个版本都能证实的部分


def test_conflict_confirms_minimum_proven_overcharge():
    rows = [clean_row(call_id="D2", charged_seconds="240"),
            clean_row(call_id="D2", charged_seconds="300")]
    refund = apply_entries(base_from_conflicts(rows), [])
    claim = refund["claims"][0]
    # 单条话单应计 120：较保守版本多收 120，较激进版本多收 180。
    # 两个版本都能证实的多收为 120，其余 60 待补证；整笔不因差额被搁置。
    assert claim["confirmed_seconds"] == 120
    assert claim["pending_seconds"] == 60
    assert claim["status"] == "PENDING"
    assert "VERSION_DELTA_UNPROVEN" in claim["pending_reasons"]


def test_conflict_where_one_version_shows_no_overcharge_stays_pending():
    rows = [clean_row(call_id="D3", charged_seconds="240"),
            clean_row(call_id="D3", charged_seconds="60")]
    refund = apply_entries(base_from_conflicts(rows), [])
    claim = refund["claims"][0]
    assert claim["confirmed_seconds"] == 0
    assert claim["pending_seconds"] > 0
    assert "VERSION_CONTRADICTS_OVERCHARGE" in claim["pending_reasons"]


def test_conflict_never_attributes_to_another_subscriber():
    rows = [clean_row(call_id="D5", charged_seconds="240", subscriber="A"),
            clean_row(call_id="D5", charged_seconds="300", subscriber="B")]
    refund = apply_entries(base_from_conflicts(rows), [])
    claim = refund["claims"][0]
    # 两版本都显示多收，可证 120 秒照常确认；但归属有争议，不能记到 A 或 B 名下。
    assert claim["attribution_disputed"] is True
    assert claim["confirmed_seconds"] == 120
    assert claim["attribution"] is None
    labels = {item["label"] for item in refund["by_attribution"]}
    assert labels == {"未归属"}
    assert "A|OUT" not in labels and "B|OUT" not in labels
    assert claim["notes"] == ["ATTRIBUTION_DISPUTED"]


def test_conflict_claim_balances():
    rows = [clean_row(call_id="D6", charged_seconds="240"),
            clean_row(call_id="D6", charged_seconds="300")]
    refund = apply_entries(base_from_conflicts(rows), [])
    assert all(check["ok"] for check in refund["checks"])
    assert (refund["confirmed_seconds"] + refund["pending_seconds"]
            == refund["total_overcharge_seconds"])


# 补证入账：只补登未确认部分，重试不重复入账


def test_supplement_fills_pending_delta():
    rows = [clean_row(call_id="S1", charged_seconds="240"),
            clean_row(call_id="S1", charged_seconds="300")]
    base = base_from_conflicts(rows)
    before = apply_entries(base, [])
    assert before["claims"][0]["pending_seconds"] == 60
    after = apply_entries(base, [{"call_id": "S1", "seconds": 60, "subscriber": "A",
                                  "direction": "OUT", "evidence": "e"}])
    claim = after["claims"][0]
    assert claim["confirmed_seconds"] == 180
    assert claim["pending_seconds"] == 0
    assert claim["status"] == "CONFIRMED"


def test_supplement_cannot_exceed_provable_overcharge():
    rows = [clean_row(call_id="S2", end_at="2026-11-20 10:02:00", charged_seconds="240")]
    base = build_base({"conflicts": []}, case(rows))
    after = apply_entries(base, [{"call_id": "S2", "seconds": 9999, "subscriber": "A",
                                  "direction": "OUT", "evidence": "e"}])
    assert after["claims"][0]["confirmed_seconds"] == 120
    assert after["confirmed_seconds"] <= after["total_overcharge_seconds"]


def test_retried_evidence_does_not_double_book():
    rows = [clean_row(call_id="S3", charged_seconds="240"),
            clean_row(call_id="S3", charged_seconds="300")]
    base = base_from_conflicts(rows)
    entry = {"call_id": "S3", "seconds": 60, "subscriber": "A", "direction": "OUT", "evidence": "same"}
    once = apply_entries(base, [entry])
    twice = apply_entries(base, [entry, entry])
    assert once["confirmed_seconds"] == twice["confirmed_seconds"] == 180


def test_supplement_resolves_disputed_attribution_to_a_version():
    rows = [clean_row(call_id="S4", charged_seconds="240", subscriber="A"),
            clean_row(call_id="S4", charged_seconds="300", subscriber="B")]
    base = base_from_conflicts(rows)
    before = apply_entries(base, [])
    assert before["claims"][0]["attribution"] is None
    assert {item["label"] for item in before["by_attribution"]} == {"未归属"}
    after = apply_entries(base, [{"call_id": "S4", "seconds": 60, "subscriber": "B",
                                  "direction": "OUT", "evidence": "proof"}])
    claim = after["claims"][0]
    assert claim["confirmed_seconds"] == 180
    assert claim["attribution"] == "B|OUT"
    labels = {item["label"] for item in after["by_attribution"]}
    assert labels == {"B|OUT"}


def test_attribution_resolution_does_not_double_count_auto_confirmed():
    rows = [clean_row(call_id="S5", charged_seconds="240", subscriber="A"),
            clean_row(call_id="S5", charged_seconds="300", subscriber="B")]
    base = base_from_conflicts(rows)
    before = apply_entries(base, [])
    after = apply_entries(base, [{"call_id": "S5", "seconds": 60, "subscriber": "A",
                                  "direction": "OUT", "evidence": "proof"}])
    # 归属由证据锁定到 A，时长不因此翻倍：可证 120 + 补证 60 = 180。
    assert after["claims"][0]["confirmed_seconds"] == before["claims"][0]["confirmed_seconds"] + 60
    assert after["total_overcharge_seconds"] == before["total_overcharge_seconds"]


# 工作台接口：可查已确认时长与待补证原因，补证只补登未确认部分


def run_payload(client, profile="dirty"):
    return client.post("/api/runs", json={"profile": profile}).get_json()


def test_workbench_exposes_confirmed_and_pending(client):
    run = run_payload(client)
    refund = run["refund"]
    assert refund["confirmed_seconds"] + refund["pending_seconds"] == refund["total_overcharge_seconds"]
    assert all("pending_reasons" in claim for claim in refund["claims"])
    assert any(claim["pending_seconds"] > 0 for claim in refund["claims"])


def test_workbench_pending_reason_is_readable(client):
    run = run_payload(client)
    reasons = {item["code"] for item in run["refund"]["pending_reasons"]}
    assert reasons
    catalog = client.get("/api/catalog").get_json()
    assert set(catalog["refund_reasons"]) >= {"VERSION_CONTRADICTS_OVERCHARGE",
                                              "VERSION_DELTA_UNPROVEN",
                                              "ATTRIBUTION_DISPUTED"}


def test_supplement_requires_evidence(client):
    run = run_payload(client)
    call_id = next(claim["call_id"] for claim in run["refund"]["claims"]
                   if claim["pending_seconds"] > 0 and not claim["attribution_disputed"])
    response = client.post(f"/api/runs/{run['run_id']}/refund",
                           json={"call_id": call_id, "seconds": 10,
                                 "subscriber": "A", "direction": "OUT", "evidence": ""})
    assert response.status_code == 400
    assert response.get_json()["code"] == "REFUND_EVIDENCE_REQUIRED"


def test_supplement_unknown_call_is_rejected(client):
    run = run_payload(client)
    response = client.post(f"/api/runs/{run['run_id']}/refund",
                           json={"call_id": "NOPE", "seconds": 10, "subscriber": "A",
                                 "direction": "OUT", "evidence": "e"})
    assert response.status_code == 404
    assert response.get_json()["code"] == "REFUND_CLAIM_UNKNOWN"


def test_supplement_cannot_be_attributed_outside_versions(client):
    run = run_payload(client)
    call_id = next(claim["call_id"] for claim in run["refund"]["claims"]
                   if claim["pending_seconds"] > 0)
    response = client.post(f"/api/runs/{run['run_id']}/refund",
                           json={"call_id": call_id, "seconds": 10,
                                 "subscriber": "ZZ", "direction": "IN", "evidence": "e"})
    assert response.status_code == 400
    assert response.get_json()["code"] == "REFUND_ATTRIBUTION"


def test_supplement_updates_confirmed_total_and_is_idempotent(client):
    run = run_payload(client)
    claim = next(item for item in run["refund"]["claims"] if item["pending_seconds"] > 0)
    body = {"call_id": claim["call_id"], "seconds": 30,
            "subscriber": claim["attribution_options"][0]["subscriber"],
            "direction": claim["attribution_options"][0]["direction"], "evidence": "ticket-9"}
    first = client.post(f"/api/runs/{run['run_id']}/refund", json=body)
    assert first.status_code == 200
    assert first.get_json()["created"] is True
    confirmed = first.get_json()["refund"]["confirmed_seconds"]
    second = client.post(f"/api/runs/{run['run_id']}/refund", json=body)
    assert second.status_code == 200
    assert second.get_json()["created"] is False
    assert second.get_json()["refund"]["confirmed_seconds"] == confirmed
    reloaded = client.get(f"/api/runs/{run['run_id']}").get_json()
    assert reloaded["refund"]["confirmed_seconds"] == confirmed
    assert len(reloaded["refund_entries"]) == 1


def test_confirmed_claim_is_not_pending_for_supplement(client):
    run = run_payload(client)
    claim = next(item for item in run["refund"]["claims"] if item["pending_seconds"] == 0)
    response = client.post(f"/api/runs/{run['run_id']}/refund",
                           json={"call_id": claim["call_id"], "seconds": 10,
                                 "subscriber": claim["subscriber"],
                                 "direction": claim["direction"], "evidence": "later"})
    assert response.status_code == 409
    assert response.get_json()["code"] == "REFUND_NOT_PENDING"

