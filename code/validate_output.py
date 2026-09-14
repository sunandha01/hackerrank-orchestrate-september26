#!/usr/bin/env python3
"""Structural validator for output.csv against DESIGN.md §3 invariants."""
import csv
import sys
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATASET = REPO / "dataset"
OUTPUT = REPO / "output.csv"

REQUIRED_COLS = [
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]

STATUSES = {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
METHODS = {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}

def load(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def load_requests():
    return {r["request_id"]: r for r in load(DATASET / "requests.csv")}


def check():
    errors = []
    reqs = load_requests()
    if not OUTPUT.exists():
        print(f"FAIL: {OUTPUT} missing")
        sys.exit(1)
    with open(OUTPUT, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        if header != REQUIRED_COLS:
            errors.append(f"header mismatch:\n  got: {header}\n  want: {REQUIRED_COLS}")
        rows = list(csv.DictReader(open(OUTPUT, newline="", encoding="utf-8")))

    if len(rows) != len(reqs):
        errors.append(f"row count {len(rows)} != {len(reqs)} eval requests")

    seen = set()
    for row in rows:
        rid = row["request_id"]
        if rid in seen:
            errors.append(f"{rid}: duplicate row")
        seen.add(rid)
        if rid not in reqs:
            errors.append(f"{rid}: not in dataset/requests.csv")
            continue
        req = reqs[rid]
        req_amt = Decimal(req["requested_amount"])
        req_date = req["request_date"]

        try:
            safe = Decimal(row["amount_safe_to_pay"])
        except Exception:
            errors.append(f"{rid}: amount_safe_to_pay not numeric: {row['amount_safe_to_pay']!r}")
            continue
        if not (Decimal(0) <= safe <= req_amt):
            errors.append(f"{rid}: amount_safe_to_pay {safe} out of [0, {req_amt}]")

        status = row["affordability_status"]
        method = row["recommended_payment_method"]
        plan = row["payment_plan"]
        earliest = row["earliest_date_for_full_payment"]
        changes = row["spending_changes_needed"]

        if status not in STATUSES:
            errors.append(f"{rid}: bad affordability_status {status!r}")
        if method not in METHODS:
            errors.append(f"{rid}: bad recommended_payment_method {method!r}")

        # affordable_now => date=request_date AND method=full_payment
        if status == "affordable_now":
            if method != "full_payment":
                errors.append(f"{rid}: affordable_now must use full_payment (got {method})")
            if earliest != req_date:
                errors.append(f"{rid}: affordable_now requires earliest_date=request_date (got {earliest})")

        # wait => plan is one payment of requested_amount on earliest_date
        if method == "wait":
            if status != "affordable_later":
                errors.append(f"{rid}: wait must be affordable_later (got {status})")
            if plan == "none" or "|" in plan:
                errors.append(f"{rid}: wait plan must be single payment, got {plan!r}")
            else:
                try:
                    d, a = plan.split(":")
                    if d != earliest:
                        errors.append(f"{rid}: wait plan date {d} != earliest_date {earliest}")
                    if Decimal(a) != req_amt:
                        errors.append(f"{rid}: wait plan amount {a} != requested_amount {req_amt}")
                except Exception:
                    errors.append(f"{rid}: wait plan format bad: {plan!r}")

        # not_recommended => plan=none
        if method == "not_recommended":
            if plan != "none":
                errors.append(f"{rid}: not_recommended requires plan=none (got {plan!r})")
            if status != "not_affordable":
                errors.append(f"{rid}: not_recommended must be not_affordable (got {status})")

        # partial_payment => two payments summing to req_amt (only if produced this turn)
        if method == "partial_payment":
            parts = plan.split("|")
            if len(parts) != 2:
                errors.append(f"{rid}: partial_payment needs 2 payments, got {plan!r}")
            else:
                try:
                    total = sum(Decimal(p.split(":")[1]) for p in parts)
                    if total != req_amt:
                        errors.append(f"{rid}: partial plan sum {total} != req_amt {req_amt}")
                except Exception:
                    errors.append(f"{rid}: partial plan format bad: {plan!r}")

        # spending_changes_needed shape
        if changes != "none":
            actions = changes.split("|")
            if len(actions) > 3:
                errors.append(f"{rid}: >3 spending changes")
            targets = []
            for a in actions:
                if a.startswith("stop:"):
                    targets.append(a.split(":", 1)[1])
                elif a.startswith("reduce_to:"):
                    targets.append(a.split(":")[1])
                else:
                    errors.append(f"{rid}: bad change {a!r}")
            if len(set(targets)) != len(targets):
                errors.append(f"{rid}: stop/reduce_to on same event")

    if errors:
        print(f"VALIDATOR: {len(errors)} error(s)")
        for e in errors[:50]:
            print(f"  {e}")
        sys.exit(1)
    print(f"VALIDATOR: OK ({len(rows)} rows)")

if __name__ == "__main__":
    check()
