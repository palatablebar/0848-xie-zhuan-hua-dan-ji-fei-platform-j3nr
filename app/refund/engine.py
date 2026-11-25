"""退款对账：及时确认有依据的多收时长，把有争议的部分留作待补证。

口径约定：
- 应计时长始终复用 ``app.domain.calculate.merge_calls``，与批次计费规则完全一致。
- 可退时长 = 已计秒数 - 应计秒数；按同一用户同一方向合并后计算，不为负数。
- 同一话单号存在冲突版本时，只确认所有版本都能证实的最小多收（``baseline``），
  版本之间的差额（``ceiling - baseline``）计入待补证，不因整笔搁置而拖住可证部分。
- 任一版本无法证实多收时整笔待补证（``INVALID_VERSION`` / ``VERSION_CONTRADICTS_OVERCHARGE``）。
- 归属有争议（版本指向不同号码或方向）时，已确认时长记为「未归属」，绝不记到其他号码或方向；
  证据确认归属后才落到具体号码与方向。
- 补证只补登尚未确认的可退时长，并以可证多收为上限；同一证据重试不重复入账。
"""

from collections import OrderedDict

from app.domain.calculate import merge_calls
from app.domain.helpers import largest_remainder
from app.domain.prepare import prepare_rows

REASONS = {
    "INVALID_VERSION": "存在无法解析的版本，需补证",
    "VERSION_CONTRADICTS_OVERCHARGE": "另一版本未显示多收，需补证后才能确认",
    "VERSION_DELTA_UNPROVEN": "版本之间的差额尚待补证",
    "ATTRIBUTION_DISPUTED": "归属存在争议，需补证确定号码或方向",
    "AWAITING_EVIDENCE": "仍有待补证的多收时长",
}

NOTES = {
    "ATTRIBUTION_DISPUTED": "归属存在争议，已确认时长记为未归属，不记到其他号码或方向",
}

UNATTRIBUTED = "未归属"


def _cohort(row):
    return (row["subscriber"], row["direction"].upper())


def _ceil_seconds(call):
    return merge_calls([call])[1]


def _version(item):
    return {
        "line": item.get("_line", 0),
        "subscriber": item["subscriber"],
        "direction": item["direction"].upper(),
        "start_at": item["start_at"].isoformat(sep=" "),
        "end_at": item["end_at"].isoformat(sep=" "),
        "charged_seconds": item["charged_seconds"],
        "rate_per_min_cents": item["rate_per_min_cents"],
    }


def _claim(call_id, subscriber, direction, rate, baseline, ceiling, versions, flags, options):
    return {
        "call_id": call_id,
        "subscriber": subscriber,
        "direction": direction,
        "rate_per_min_cents": int(rate),
        "baseline_seconds": int(baseline),
        "ceiling_seconds": int(ceiling),
        "versions": versions,
        "flags": flags,
        "attribution_options": options,
    }


def _accepted_claims(prepared):
    """已采信行按同一用户同一方向合并后，把可证明的多收时长落到具体话单号。"""
    groups = OrderedDict()
    for row in prepared:
        groups.setdefault(_cohort(row), []).append(row)
    claims = []
    for (subscriber, direction), calls in groups.items():
        _, billable = merge_calls(calls)
        charged = sum(call["charged_seconds"] for call in calls)
        total_overcharge = max(0, charged - billable)
        if total_overcharge <= 0:
            continue
        own = [max(0, call["charged_seconds"] - _ceil_seconds(call)) for call in calls]
        weights = own if sum(own) > 0 else [call["charged_seconds"] for call in calls]
        if sum(weights) <= 0:
            weights = [1] * len(calls)
        for call, seconds in zip(calls, largest_remainder(total_overcharge, weights)):
            if seconds <= 0:
                continue
            version = _version(call)
            version["billable_seconds"] = call["charged_seconds"] - seconds
            version["overcharge_seconds"] = seconds
            claims.append(_claim(
                call["call_id"], subscriber, direction, call["rate_per_min_cents"],
                baseline=seconds, ceiling=seconds, versions=[version],
                flags={"single": True, "invalid": False, "contradicted": False,
                       "attribution_disputed": False},
                options=[{"subscriber": subscriber, "direction": direction}],
            ))
    return claims


def _conflict_claims(conflicts, prepared):
    """同一话单号的多个版本：确认各版本都能证实的多收，差额与归属争议留待补证。"""
    groups = OrderedDict()
    for row in prepared:
        groups.setdefault(_cohort(row), []).append(row)
    baseline_billable = {key: merge_calls(calls)[1] for key, calls in groups.items()}
    claims = []
    for group in conflicts:
        rows = [dict(row, _line=row.get("_line", index))
                for index, row in enumerate(group["rows"], start=1)]
        typed, _ = prepare_rows(rows)
        by_line = {item.get("_line"): item for item in typed}
        versions, overcharges, options, rates = [], [], [], []
        invalid = 0
        for row in rows:
            item = by_line.get(row.get("_line"))
            if item is None:
                invalid += 1
                continue
            key = _cohort(item)
            marginal = max(0, merge_calls(groups.get(key, []) + [item])[1]
                           - baseline_billable.get(key, 0))
            overcharge = max(0, item["charged_seconds"] - marginal)
            version = _version(item)
            version["billable_seconds"] = marginal
            version["overcharge_seconds"] = overcharge
            versions.append(version)
            overcharges.append(overcharge)
            rates.append(item["rate_per_min_cents"])
            option = {"subscriber": item["subscriber"], "direction": key[1]}
            if option not in options:
                options.append(option)
        # 所有版本都给出多收证据时才确认最小可证额，否则整笔待补证。
        corroborated = bool(overcharges) and not invalid and all(value > 0 for value in overcharges)
        baseline = min(overcharges) if corroborated else 0
        ceiling = max(overcharges) if overcharges else 0
        if ceiling <= 0:
            # 任何版本都看不出多收，不产生可退条目，也不记为待补证。
            continue
        attribution_disputed = len(options) > 1
        subscriber = None if attribution_disputed else (options[0]["subscriber"] if options else "")
        direction = None if attribution_disputed else (options[0]["direction"] if options else "")
        claims.append(_claim(
            group["key"], subscriber, direction, min(rates) if rates else 0,
            baseline=baseline, ceiling=ceiling, versions=versions,
            flags={"single": False, "invalid": bool(invalid),
                   "contradicted": not corroborated,
                   "attribution_disputed": attribution_disputed},
            options=options or [{"subscriber": "", "direction": ""}],
        ))
    return claims


def build_base(intake, prepared):
    """生成退款对账底稿。只依赖采信与整理结果，不改变应计口径。"""
    claims = _accepted_claims(prepared) + _conflict_claims(intake.get("conflicts", []), prepared)
    claims.sort(key=lambda claim: (claim["subscriber"] or "", claim["direction"] or "", claim["call_id"]))
    return {"claims": claims}


def _bookings(entries):
    """汇总补证入账。按记录标识去重，重复证据不重复计入。"""
    booked, attribution, seen = {}, {}, set()
    for entry in entries:
        marker = entry.get("entry_id") or (entry["call_id"], entry.get("evidence", ""))
        if marker in seen:
            continue
        seen.add(marker)
        call_id = entry["call_id"]
        booked[call_id] = booked.get(call_id, 0) + int(entry["seconds"])
        if entry.get("subscriber") and entry.get("direction"):
            attribution.setdefault(call_id, []).append(
                {"subscriber": entry["subscriber"], "direction": entry["direction"].upper()})
    return booked, attribution


def resolved_attribution(raw, attribution):
    """补证证据能确认的唯一归属；落在版本范围内才有效。"""
    choices = attribution.get(raw["call_id"], [])
    unique = {(item["subscriber"], item["direction"]) for item in choices}
    options = {(item["subscriber"], item["direction"]) for item in raw["attribution_options"]
               if item["subscriber"] and item["direction"]}
    if len(unique) == 1:
        (choice,) = unique
        if choice in options:
            return choice
    return None


def auto_confirmed(claim):
    """不依赖补证即可确认的时长：所有版本都能证实的多收。"""
    return claim["baseline_seconds"]


def _reasons(claim):
    """待补证原因：解释为何仍有未确认的可退时长。"""
    if claim["pending_seconds"] <= 0:
        return []
    reasons = []
    if claim["flags"]["invalid"]:
        reasons.append("INVALID_VERSION")
    if claim["flags"]["contradicted"]:
        reasons.append("VERSION_CONTRADICTS_OVERCHARGE")
    elif claim["ceiling_seconds"] > claim["baseline_seconds"]:
        reasons.append("VERSION_DELTA_UNPROVEN")
    if claim["flags"]["attribution_disputed"] and claim["attribution"] is None:
        reasons.append("ATTRIBUTION_DISPUTED")
    if not reasons:
        reasons.append("AWAITING_EVIDENCE")
    return list(OrderedDict.fromkeys(reasons))


def _notes(claim):
    """已确认但归属仍未坐实时提示归属争议；不占用待补证时长。"""
    if claim["confirmed_seconds"] > 0 and claim["flags"]["attribution_disputed"] \
            and claim["attribution"] is None:
        return ["ATTRIBUTION_DISPUTED"]
    return []


def apply_entries(base, entries):
    """把已补证记录并入底稿。确认额单调、以可证多收为上限，重复提交不重复入账。"""
    booked, attribution = _bookings(entries)
    claims = []
    for raw in base.get("claims", []):
        resolved = resolved_attribution(raw, attribution)
        base_auto = auto_confirmed(raw)
        extra = min(booked.get(raw["call_id"], 0), max(0, raw["ceiling_seconds"] - base_auto))
        confirmed = min(raw["ceiling_seconds"], base_auto + extra)
        pending = raw["ceiling_seconds"] - confirmed

        if raw["flags"]["attribution_disputed"]:
            # 归属有争议：未坐实时记为「未归属」，绝不记到其他号码或方向。
            bucket = resolved
        else:
            bucket = (raw["subscriber"], raw["direction"]) if confirmed > 0 else None
        claims.append({
            "call_id": raw["call_id"],
            "subscriber": raw["subscriber"],
            "direction": raw["direction"],
            "rate_per_min_cents": raw["rate_per_min_cents"],
            "ceiling_seconds": raw["ceiling_seconds"],
            "baseline_seconds": raw["baseline_seconds"],
            "confirmed_seconds": confirmed,
            "pending_seconds": pending,
            "attribution": None if bucket is None else "|".join(bucket),
            "attribution_disputed": raw["flags"]["attribution_disputed"],
            "attribution_options": raw["attribution_options"],
            "flags": raw["flags"],
            "versions": raw["versions"],
            "status": "CONFIRMED" if pending == 0 else "PENDING",
            "_buckets": [(bucket, confirmed)],
        })

    for claim in claims:
        claim["pending_reasons"] = _reasons(claim)
        claim["reasons"] = [REASONS[code] for code in claim["pending_reasons"]]
        claim["notes"] = _notes(claim)
        claim["note_texts"] = [NOTES.get(code, REASONS.get(code, code)) for code in claim["notes"]]

    by_attribution = OrderedDict()
    for claim in claims:
        for attribution_key, seconds in claim.pop("_buckets"):
            if seconds <= 0:
                continue
            label = UNATTRIBUTED if attribution_key is None else "|".join(attribution_key)
            by_attribution[label] = by_attribution.get(label, 0) + seconds

    total = sum(claim["ceiling_seconds"] for claim in claims)
    confirmed = sum(claim["confirmed_seconds"] for claim in claims)
    pending = sum(claim["pending_seconds"] for claim in claims)
    amount = sum(claim["confirmed_seconds"] * claim["rate_per_min_cents"] // 60 for claim in claims)

    reason_counts = OrderedDict()
    for claim in claims:
        for code in claim["pending_reasons"]:
            reason_counts[code] = reason_counts.get(code, 0) + 1

    allowed = {UNATTRIBUTED}
    for raw in base.get("claims", []):
        for option in raw["attribution_options"]:
            if option["subscriber"] and option["direction"]:
                allowed.add(option["subscriber"] + "|" + option["direction"])
    checks = [
        {"id": "refund_total", "label": "可退时长守恒", "ok": confirmed + pending == total},
        {"id": "refund_ceiling", "label": "确认不超可证多收",
         "ok": all(claim["confirmed_seconds"] <= claim["ceiling_seconds"] for claim in claims)},
        {"id": "refund_attribution", "label": "争议时长不记其他归属",
         "ok": all(label in allowed for label in by_attribution)},
    ]
    return {
        "claims": claims,
        "confirmed_seconds": confirmed,
        "pending_seconds": pending,
        "total_overcharge_seconds": total,
        "confirmed_amount_cents": amount,
        "by_attribution": [{"label": label, "seconds": value}
                           for label, value in by_attribution.items()],
        "pending_reasons": [{"code": code, "label": REASONS.get(code, code), "count": count}
                            for code, count in reason_counts.items()],
        "checks": checks,
        "all_checks": all(item["ok"] for item in checks),
    }


def claim_status(base, entries, call_id):
    """某话单号已补证后的对账条目（供补证入账时校验待补证上限）。"""
    for claim in apply_entries(base, entries)["claims"]:
        if claim["call_id"] == call_id:
            return claim
    return None
