#!/usr/bin/env python3
"""Score our engine against sample_requests.csv (25 solved examples).

Runs the same decision logic used by code/main.py, but on the sample rows,
and compares the produced fields to the published ones. Samples are NOT a
lookup table — we recompute everything from raw data.
"""
import csv
import sys
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "code"))

import main as _main_mod  # noqa: E402
from main import decide, build_rates, load, parse_dec, load_patches  # noqa: E402
from collections import defaultdict

_main_mod._PATCHES_CACHE = load_patches()


def eq_amt(a, b):
    try:
        return Decimal(a) == Decimal(b)
    except Exception:
        return a == b


def main():
    profiles = {p["user_id"]: p for p in load("financial_profiles")}
    events = load("financial_events")
    rates = load("exchange_rates")
    samples = load("sample_requests")
    options_rows = load("request_payment_options")
    by_pair = build_rates(rates)
    events_by_user = defaultdict(list)
    for e in events:
        events_by_user[e["user_id"]].append(e)
    options_by_request = defaultdict(list)
    for o in options_rows:
        options_by_request[o["request_id"]].append(o)

    keys = [
        "amount_safe_to_pay",
        "affordability_status",
        "recommended_payment_method",
        "payment_plan",
        "earliest_date_for_full_payment",
    ]
    tally = {k: 0 for k in keys}
    diffs = []
    for s in samples:
        got = decide(s, profiles[s["user_id"]], events_by_user[s["user_id"]], by_pair,
                     options_by_request[s["request_id"]])
        row = {"request_id": s["request_id"]}
        for k in keys:
            want = s[k]
            g = got[k]
            if k == "amount_safe_to_pay":
                same = eq_amt(want, g)
            else:
                same = (want or "") == (g or "")
            if same:
                tally[k] += 1
            row[k] = ("=" if same else f"want={want!r} got={g!r}")
        diffs.append(row)

    print(f"scored {len(samples)} samples")
    for k in keys:
        print(f"  {k}: {tally[k]}/{len(samples)}")
    print()
    print("MISMATCH TABLE (only fields that differ):")
    header = ["request_id"] + keys
    widths = [max(len(str(row.get(h, ""))) for row in diffs + [{h: h}]) for h in header]
    line = " | ".join(f"{h:{w}}" for h, w in zip(header, widths))
    print(line)
    print("-" * len(line))
    for row in diffs:
        cells = [row["request_id"]]
        any_diff = False
        for k in keys:
            cell = row[k]
            if cell != "=":
                any_diff = True
            cells.append(cell)
        if any_diff:
            print(" | ".join(f"{c:{w}}" for c, w in zip(cells, widths)))


if __name__ == "__main__":
    main()
