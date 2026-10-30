"""Frozen per-run phase quotas and fenced, replay-safe provider accounting."""

from copy import deepcopy
from uuid import uuid4

VERSION = "analysis-budget-v1"
SHARES = {
    "answer_extraction": 35,
    "source_extraction": 25,
    "context": 25,
    "verification": 15,
}


def allocate(total):
    if type(total) is not int or total < 0:
        raise ValueError("Budget must be a nonnegative integer")
    values = {key: total * share // 100 for key, share in SHARES.items()}
    order = sorted(SHARES, key=lambda key: -(total * SHARES[key] % 100))
    for key in order[: total - sum(values.values())]:
        values[key] += 1
    return values


def freeze_budget(requests, tokens, *, answer_workload=None):
    request_caps, token_caps = allocate(requests), allocate(tokens)
    if answer_workload is not None:
        if any(
            type(answer_workload.get(field)) is not int or answer_workload[field] < 0
            for field in ("requests", "tokens")
        ):
            raise ValueError("Invalid answer workload")
        for values, field in ((request_caps, "requests"), (token_caps, "tokens")):
            extraction = values["answer_extraction"] + values["source_extraction"]
            values["answer_extraction"] = min(extraction, answer_workload[field])
            values["source_extraction"] = extraction - values["answer_extraction"]
    return {
        "version": VERSION,
        "allocation_policy": "planned-answers-extraction-reserve-v1"
        if answer_workload is not None
        else "fixed-phase-reserve-v1",
        "requests": requests,
        "tokens": tokens,
        "phases": {
            key: {"requests": request_caps[key], "tokens": token_caps[key]}
            for key in SHARES
        },
    }


def freeze_run_budget(requests, tokens, responses):
    from app.intelligence.semantics import SYSTEM, chunks

    # Text caches save provider calls; each response still has its own observations.
    texts = {
        chunk["text"]
        for response in responses
        if not response["error"]
        for chunk in chunks(response["text"])
    }
    return freeze_budget(
        requests,
        tokens,
        answer_workload={
            "requests": len(texts),
            "tokens": sum(estimate_tokens([SYSTEM, text], 8192) for text in texts),
        },
    )


def normalize_usage(value):
    if not isinstance(value, dict):
        return None
    values = [value.get(key) for key in ("prompt_tokens", "completion_tokens")]
    if not all(type(item) is int and item >= 0 for item in values):
        return None
    return sum(values)


def estimate_tokens(messages, max_output):
    # Byte count + output cap + framing margin is deliberately conservative.
    # It is a reservation, not a claim about the provider's tokenizer or invoice.
    return sum(len(value.encode("utf-8")) for value in messages) + max_output + 4096


def usage_fields(value):
    return (
        {key: value[key] for key in ("prompt_tokens", "completion_tokens")}
        if normalize_usage(value) is not None
        else {}
    )


def recover_reservations(ledger, lease_version):
    current = deepcopy(ledger)
    for row in current["receipts"].values():
        if row["state"] == "reserved" and row["lease_version"] != lease_version:
            row.update(state="unknown", reported_tokens=None)
    return current


def budget_status(ledger):
    policy = ledger["policy"]
    phases = {}
    for phase, cap in policy["phases"].items():
        rows = [row for row in ledger["receipts"].values() if row["phase"] == phase]
        reported = sum(row.get("reported_tokens") or 0 for row in rows)
        unknown = sum(
            row["reserved_tokens"] for row in rows if row["state"] == "unknown"
        )
        pending = sum(
            row["reserved_tokens"] for row in rows if row["state"] == "reserved"
        )
        charged = reported + unknown + pending
        phases[phase] = {
            "request_limit": cap["requests"],
            "token_limit": cap["tokens"],
            "requests": len(rows),
            "reported_tokens": reported,
            "unknown_usage_reserved_tokens": unknown,
            "in_flight_reserved_tokens": pending,
            "charged_tokens": charged,
            "over_limit": charged > cap["tokens"],
        }
    return {
        "version": VERSION,
        "request_limit": policy["requests"],
        "token_limit": policy["tokens"],
        "requests": sum(row["requests"] for row in phases.values()),
        "charged_tokens": sum(row["charged_tokens"] for row in phases.values()),
        "phases": phases,
    }


def reserve(ledger, policy, phase, estimated_tokens, subject_id):
    if phase not in SHARES or type(estimated_tokens) is not int or estimated_tokens < 1:
        raise ValueError("Invalid reservation")
    current = (
        deepcopy(ledger) if ledger else {"policy": deepcopy(policy), "receipts": {}}
    )
    if current["policy"] != policy:
        raise ValueError("Frozen budget changed")
    status = budget_status(current)
    scoped = status["phases"][phase]
    if (
        status["requests"] >= policy["requests"]
        or scoped["requests"] >= scoped["request_limit"]
    ):
        return current, None, "phase_request_limit"
    if (
        status["charged_tokens"] + estimated_tokens > policy["tokens"]
        or scoped["charged_tokens"] + estimated_tokens > scoped["token_limit"]
    ):
        return current, None, "phase_token_limit"
    receipt = uuid4().hex
    current["receipts"][receipt] = {
        "phase": phase,
        "subject_id": subject_id,
        "reserved_tokens": estimated_tokens,
        "state": "reserved",
    }
    return current, receipt, None


def settle(ledger, receipt, usage):
    current = deepcopy(ledger)
    row = current["receipts"][receipt]
    reported = normalize_usage(usage)
    state = "reported" if reported is not None else "unknown"
    if row["state"] != "reserved":
        if row["state"] != state or row.get("reported_tokens") != reported:
            raise ValueError("Conflicting usage settlement")
        return current
    row.update(state=state, reported_tokens=reported)
    return current
