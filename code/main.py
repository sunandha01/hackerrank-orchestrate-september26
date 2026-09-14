#!/usr/bin/env python3
"""Buy or Wait? — Phase 3 pre-change baseline engine.

Reads dataset/, writes root output.csv. No VLM, no spending-change search,
no plan ranking. Emits full_payment / wait / not_recommended only, per the
temporary policy in Phase 3.
"""
import csv
import json
import statistics
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, getcontext, ROUND_HALF_UP, ROUND_DOWN
from pathlib import Path

getcontext().prec = 28

REPO = Path(__file__).resolve().parent.parent
DATASET = REPO / "dataset"
OUTPUT = REPO / "output.csv"
PATCHES = Path(__file__).resolve().parent / "evidence_patches.json"
_PATCHES_CACHE = None

HORIZON_DAYS = 90

OUTPUT_COLUMNS = [
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]

MONTHS = ["January","February","March","April","May","June",
          "July","August","September","October","November","December"]

# ---------- IO ----------

def load(name):
    with open(DATASET / f"{name}.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_patches():
    if not PATCHES.exists():
        return {"images": [], "messages": []}
    with open(PATCHES, encoding="utf-8") as f:
        return json.load(f)


def apply_patches(events, patches):
    """Fill blank amounts from image patches. Blank amount is never treated as 0."""
    img_by_event = {p["event_id"]: p for p in patches.get("images", [])}
    msg_by_event = {p["event_id"]: p for p in patches.get("messages", [])}
    out = []
    for e in events:
        e = dict(e)
        if not e.get("amount") and e["event_id"] in img_by_event:
            p = img_by_event[e["event_id"]]
            e["amount"] = str(p["amount"])
            if p.get("currency"):
                e["currency"] = p["currency"]
        if e["event_id"] in msg_by_event:
            m = msg_by_event[e["event_id"]]
            if m.get("action") == "cancel":
                e["status"] = "cancelled"
            elif m.get("action") == "amend_amount":
                e["amount"] = str(m["amount"])
            elif m.get("action") == "amend_date":
                e["settlement_date"] = m["date"]
        out.append(e)
    return out

def parse_date(s):
    if not s:
        return None
    return datetime.strptime(s[:10], "%Y-%m-%d").date()

def parse_dec(s):
    if s is None or s == "":
        return None
    return Decimal(str(s))

# ---------- formatting ----------

def fmt_natural(x):
    if x is None:
        return ""
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    if d == d.to_integral_value():
        return str(int(d))
    s = f"{d.normalize():f}" if "E" in str(d.normalize()) else str(d.normalize())
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s

def fmt_plan(x):
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    if d == d.to_integral_value():
        return str(int(d))
    return str(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def fmt_money_display(x, ccy):
    """Format for decision_explanation. Thousands separators for non-IDR, none for IDR."""
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    if d == d.to_integral_value():
        n = int(d)
    else:
        n = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if ccy == "IDR":
        return str(n)
    if isinstance(n, int):
        return f"{n:,}"
    whole, frac = str(n).split(".")
    return f"{int(whole):,}.{frac}"

def pretty_date(d):
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"

# ---------- FX ----------

def build_rates(rows):
    by_pair = defaultdict(list)
    for r in rows:
        d = parse_date(r["rate_date"])
        by_pair[(r["from_currency"], r["to_currency"])].append((d, Decimal(r["rate"])))
    for k in by_pair:
        by_pair[k].sort()
    return by_pair

def latest_on_or_before(entries, d):
    ans = None
    for date_, rate in entries:
        if date_ <= d:
            ans = rate
        else:
            break
    if ans is None and entries:
        ans = entries[0][1]
    return ans

def rate_between(a, b, d, by_pair):
    if a == b:
        return Decimal(1)
    if (a, b) in by_pair:
        r = latest_on_or_before(by_pair[(a, b)], d)
        if r is not None:
            return r
    if (b, a) in by_pair:
        r = latest_on_or_before(by_pair[(b, a)], d)
        if r is not None:
            return Decimal(1) / r
    return None

def convert(amt, from_ccy, to_ccy, d, by_pair):
    if amt is None or from_ccy == to_ccy:
        return amt
    r = rate_between(from_ccy, to_ccy, d, by_pair)
    if r is not None:
        return amt * r
    for hub in ("USD", "EUR"):
        r1 = rate_between(from_ccy, hub, d, by_pair)
        r2 = rate_between(hub, to_ccy, d, by_pair)
        if r1 is not None and r2 is not None:
            return amt * r1 * r2
    raise ValueError(f"No FX route {from_ccy} -> {to_ccy} at {d}")

# ---------- Ledger ----------

def collapse_linked(events):
    """Drop parent when child is a same-type expense/debt_payment reissue."""
    by_id = {e["event_id"]: e for e in events}
    drop = set()
    for e in events:
        parent_id = e.get("linked_event_id") or ""
        if not parent_id:
            continue
        parent = by_id.get(parent_id)
        if parent is None:
            continue
        if e["event_type"] in ("expense", "debt_payment") and parent["event_type"] == e["event_type"]:
            drop.add(parent_id)
    return [e for e in events if e["event_id"] not in drop]

def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n % 2 == 1:
        return xs[n // 2]
    if isinstance(xs[0], Decimal):
        return (xs[n // 2 - 1] + xs[n // 2]) / Decimal(2)
    return (xs[n // 2 - 1] + xs[n // 2]) / 2


def find_recurring_series(user_events, request_date):
    """Per-description recurrence with monthly same-day cadence."""
    buckets = defaultdict(list)
    for e in user_events:
        if e["event_type"] not in ("expense", "subscription", "debt_payment", "income"):
            continue
        if e["status"] != "settled":
            continue
        s_date = parse_date(e.get("settlement_date"))
        if s_date is None or s_date > request_date:
            continue
        amt = parse_dec(e.get("amount"))
        if amt is None:
            continue
        key = (e["category"], e["description"], e["direction"], e["currency"])
        buckets[key].append((s_date, amt, e))
    series = []
    for key, occ in buckets.items():
        if len(occ) < 3:
            continue
        occ.sort(key=lambda t: t[0])
        span = (occ[-1][0] - occ[0][0]).days
        if span < 60:
            continue
        intervals = [(occ[i + 1][0] - occ[i][0]).days for i in range(len(occ) - 1)]
        median_int = statistics.median(intervals)
        if not (25 <= median_int <= 35):
            continue
        cadence = min([28, 30, 31], key=lambda c: abs(c - median_int))
        trailing = [o[1] for o in occ[-6:]]
        amt = _median(trailing)
        rep = occ[-1][2]
        series.append({
            "category": key[0], "description": key[1], "direction": key[2],
            "currency": key[3], "cadence": cadence, "amount": amt,
            "last_date": occ[-1][0],
            "rep_event_id": rep["event_id"],
            "flexibility": rep.get("flexibility", "fixed"),
            "min_allowed": parse_dec(rep.get("minimum_allowed_amount")),
            "event_type": rep["event_type"],
        })
    return series

def build_horizon_flows(user_events, home_ccy, by_pair, request_date):
    """Return (concrete_flows, projected_flows).

    concrete_flows: [(date, signed_home_amt)] — from pending/scheduled rows.
    projected_flows: [(date, signed_home_amt, series_dict)] — inferred recurring
    occurrences, carrying their series so the spending-change search can
    modify or drop them.
    """
    horizon_end = request_date + timedelta(days=HORIZON_DAYS)
    concrete = []
    concrete_keys = []
    for e in user_events:
        status = e["status"]
        etype = e["event_type"]
        direction = e["direction"]
        if status in ("cancelled", "failed"):
            continue
        if status == "unrealized" or direction == "non_cash":
            continue
        if status == "pending" and direction == "credit":
            continue
        if etype == "investment_valuation":
            continue
        if etype == "investment_purchase" and status == "settled":
            continue
        s_date = parse_date(e.get("settlement_date"))
        if s_date is None:
            s_date = parse_date(e.get("event_date"))
        if s_date is None or s_date <= request_date or s_date > horizon_end:
            continue
        amt = parse_dec(e.get("amount"))
        if amt is None:
            continue
        try:
            amt_home = convert(amt, e["currency"], home_ccy, s_date, by_pair)
        except ValueError:
            continue
        sign = 1 if direction == "credit" else -1
        concrete.append((s_date, sign * amt_home))
        concrete_keys.append((e["category"], e["description"], direction, s_date))
    projected = []
    for s in find_recurring_series(user_events, request_date):
        next_date = advance(s["last_date"], s["cadence"])
        while next_date <= horizon_end:
            if next_date > request_date:
                clash = False
                for ck in concrete_keys:
                    if (ck[0] == s["category"] and ck[1] == s["description"]
                            and ck[2] == s["direction"]
                            and abs((ck[3] - next_date).days) <= 3):
                        clash = True
                        break
                if not clash:
                    try:
                        amt_home = convert(s["amount"], s["currency"], home_ccy, next_date, by_pair)
                    except ValueError:
                        next_date = advance(next_date, s["cadence"])
                        continue
                    sign = 1 if s["direction"] == "credit" else -1
                    projected.append((next_date, sign * amt_home, s))
            next_date = advance(next_date, s["cadence"])
    return concrete, projected


def advance(d, cadence):
    if cadence in (30, 31):
        m = d.month + 1
        year = d.year + (m - 1) // 12
        month = (m - 1) % 12 + 1
        day = min(d.day, monthrange(year, month)[1])
        return date(year, month, day)
    return d + timedelta(days=cadence)

# ---------- Simulation ----------

def compute_cash_path(balance, flows, request_date):
    cash = [Decimal(0)] * (HORIZON_DAYS + 1)
    cash[0] = balance
    by_day = defaultdict(lambda: Decimal(0))
    for d, amt in flows:
        offset = (d - request_date).days
        if 0 < offset <= HORIZON_DAYS:
            by_day[offset] += amt
    for t in range(1, HORIZON_DAYS + 1):
        cash[t] = cash[t - 1] + by_day.get(t, Decimal(0))
    return cash

def compute_safe_and_earliest(cash, min_bal, req_amt):
    baseline_min = min(cash)
    slack = baseline_min - min_bal
    safe = max(Decimal(0), min(req_amt, slack))
    n = len(cash)
    tail_min = [Decimal(0)] * n
    tail_min[n - 1] = cash[n - 1]
    for i in range(n - 2, -1, -1):
        tail_min[i] = min(cash[i], tail_min[i + 1])
    earliest_offset = None
    for d in range(n):
        if tail_min[d] - req_amt >= min_bal:
            earliest_offset = d
            break
    return safe, earliest_offset

# ---------- Planner (Phase 4) ----------

def cash_path_from_flows(balance, flows2, request_date):
    """flows2: list of (date, signed_amt)."""
    cash = [Decimal(0)] * (HORIZON_DAYS + 1)
    cash[0] = balance
    by_day = defaultdict(lambda: Decimal(0))
    for d, amt in flows2:
        offset = (d - request_date).days
        if 0 < offset <= HORIZON_DAYS:
            by_day[offset] += amt
    for t in range(1, HORIZON_DAYS + 1):
        cash[t] = cash[t - 1] + by_day.get(t, Decimal(0))
    return cash


def plan_is_safe(cash, plan, request_date, min_bal):
    """plan = [(pay_date, pay_amount)]. Returns True iff min(shifted cash) >= min_bal."""
    by_off = defaultdict(lambda: Decimal(0))
    for pay_date, pay_amt in plan:
        offset = (pay_date - request_date).days
        if offset < 0:
            return False
        if offset > HORIZON_DAYS:
            continue  # payment beyond horizon — not visible in check
        by_off[offset] += pay_amt
    cum = Decimal(0)
    for t in range(HORIZON_DAYS + 1):
        cum += by_off.get(t, Decimal(0))
        if cash[t] - cum < min_bal:
            return False
    return True


def enumerate_candidates(request, profile, safe, earliest_offset, options):
    req_date = parse_date(request["request_date"])
    req_amt = parse_dec(request["requested_amount"])
    deadline = parse_date(request["desired_completion_date"])
    methods = set((profile.get("payment_methods_user_will_consider") or "").split("|"))
    allows_partial = request["allows_partial_payment"].strip().lower() == "true"
    max_inst = parse_dec(profile.get("max_installment_months"))

    cands = []

    if "full_payment" in methods:
        cands.append({
            "method": "full_payment",
            "plan": [(req_date, req_amt)],
            "total": req_amt, "n": 1, "start": req_date,
            "option_id": "",
            "completes": req_date <= deadline,
        })

    if (allows_partial and "partial_payment" in methods
            and safe > 0 and safe < req_amt and earliest_offset is not None):
        earliest_date = req_date + timedelta(days=earliest_offset)
        if earliest_date <= deadline:
            # Quantize safe down to 2dp so remainder+safe == req_amt at display precision.
            safe_q = safe.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if safe_q <= 0:
                pass
            else:
                remainder = req_amt - safe_q
                cands.append({
                    "method": "partial_payment",
                    "plan": [(req_date, safe_q), (earliest_date, remainder)],
                    "total": req_amt, "n": 2, "start": req_date,
                    "option_id": "",
                    "completes": True,
                })

    if "installments" in methods and max_inst is not None:
        max_inst_i = int(max_inst)
        for opt in options:
            if opt["payment_method"] != "installments":
                continue
            n = int(opt["number_of_payments"])
            if n > max_inst_i:
                continue
            first = parse_date(opt["first_payment_date"])
            freq = int(opt["payment_frequency_days"])
            pay_amt = parse_dec(opt["payment_amount"])
            total = parse_dec(opt["total_payable_amount"])
            plan = [(first + timedelta(days=k * freq), pay_amt) for k in range(n)]
            cands.append({
                "method": "installments",
                "plan": plan,
                "total": total, "n": n, "start": first,
                "option_id": opt["payment_option_id"],
                "completes": plan[-1][0] <= deadline,
            })

    if "full_payment" in methods and earliest_offset is not None and earliest_offset > 0:
        earliest_date = req_date + timedelta(days=earliest_offset)
        cands.append({
            "method": "wait",
            "plan": [(earliest_date, req_amt)],
            "total": req_amt, "n": 1, "start": earliest_date,
            "option_id": "",
            "completes": earliest_date <= deadline,
        })

    return cands


_METHOD_ORDER = {"full_payment": 0, "installments": 1, "partial_payment": 2, "wait": 3}


def rank_key(c, has_changes):
    return (
        0 if c["completes"] else 1,
        1 if has_changes else 0,
        c["total"],
        c["start"],
        c["n"],
        _METHOD_ORDER.get(c["method"], 9),
        c["option_id"] or "￿",
    )


def flexible_projected_occurrences(projected, request_date, profile):
    """Return list of (index, date, amount, series) for occurrences we may change."""
    protect = set((profile.get("expense_categories_to_protect") or "").split("|"))
    willing_reduce = set((profile.get("expense_categories_user_is_willing_to_reduce") or "").split("|"))
    willing_stop = set((profile.get("expense_categories_user_is_willing_to_stop") or "").split("|"))
    out = []
    for i, (d, amt, s) in enumerate(projected):
        if s["direction"] != "debit":
            continue
        cat = s["category"]
        if cat in protect:
            continue
        flex = s.get("flexibility", "fixed")
        can_stop = flex in ("stoppable", "reducible_or_stoppable") and cat in willing_stop
        can_reduce = flex in ("reducible", "reducible_or_stoppable") and cat in willing_reduce
        if not (can_stop or can_reduce):
            continue
        out.append({
            "idx": i, "date": d, "amount": amt, "series": s,
            "can_stop": can_stop, "can_reduce": can_reduce,
        })
    return out


def apply_changes_to_flows(concrete, projected, changes):
    """Rebuild flows with changes applied. changes = list of {kind,series_id,new_amount?}."""
    stopped_series = set()
    reduce_by_series = {}
    for ch in changes:
        if ch["kind"] == "stop":
            stopped_series.add(ch["series_id"])
        else:
            reduce_by_series[ch["series_id"]] = ch["new_amount"]
    flows = list(concrete)
    for d, amt, s in projected:
        sid = s["rep_event_id"]
        if sid in stopped_series:
            continue
        if sid in reduce_by_series:
            new_amt = reduce_by_series[sid]
            sign = 1 if s["direction"] == "credit" else -1
            # amt currently is signed home amount; use new_amt (home currency assumption)
            flows.append((d, sign * new_amt))
        else:
            flows.append((d, amt))
    return flows


def search_spending_changes(concrete, projected, balance, request_date, min_bal, candidates,
                            profile, home_ccy, by_pair):
    """Try up to 3 changes to make at least one candidate safe. Return list of changes or None."""
    flex = flexible_projected_occurrences(projected, request_date, profile)
    if not flex:
        return None
    # Aggregate by series to avoid targeting the same event twice
    by_series = defaultdict(list)
    for f in flex:
        by_series[f["series"]["rep_event_id"]].append(f)
    series_list = []
    for sid, items in by_series.items():
        # sum of amounts across in-horizon occurrences for this series (already signed)
        total_amt = sum((-a) for _, a, sr in projected if sr["rep_event_id"] == sid)
        s = items[0]["series"]
        # a "stop" removes total_amt from horizon; "reduce_to X" removes total_amt - X*n
        n_occ = sum(1 for _, _, sr in projected if sr["rep_event_id"] == sid)
        series_list.append({
            "sid": sid, "series": s, "total_savings_stop": total_amt,
            "n": n_occ, "can_stop": items[0]["can_stop"], "can_reduce": items[0]["can_reduce"],
            "min_allowed": s.get("min_allowed"), "current_amt": s["amount"],
        })
    # Order by biggest potential savings first
    series_list.sort(key=lambda x: -x["total_savings_stop"])
    series_list = series_list[:12]  # cap search space

    def actions_for(sitem):
        acts = []
        if sitem["can_stop"]:
            acts.append({"kind": "stop", "series_id": sitem["sid"], "series": sitem["series"]})
        if sitem["can_reduce"] and sitem["min_allowed"] is not None and sitem["min_allowed"] < sitem["current_amt"]:
            acts.append({
                "kind": "reduce_to", "series_id": sitem["sid"], "series": sitem["series"],
                "new_amount": sitem["min_allowed"],
            })
        return acts

    # Try combinations of size 1..3
    from itertools import combinations
    best = None
    best_rank = None
    for size in (1, 2, 3):
        for combo in combinations(series_list, size):
            for cross in _cross(combo, actions_for):
                # cross is a list of actions, one per series in combo
                # stop vs reduce_to already exclusive because per-series only one action
                flows = apply_changes_to_flows(concrete, list(projected), cross)
                cash2 = cash_path_from_flows(balance, flows, request_date)
                safe_cands = [c for c in candidates if plan_is_safe(cash2, c["plan"], request_date, min_bal)]
                if safe_cands:
                    for c in safe_cands:
                        rk = rank_key(c, True)
                        if best_rank is None or rk < best_rank:
                            best_rank = rk
                            best = (c, cross)
        if best is not None:
            return best
    return None


def _cross(combo, actions_for):
    if not combo:
        yield []
        return
    head, *rest = combo
    for act in actions_for(head):
        for tail in _cross(rest, actions_for):
            yield [act] + tail


def format_change(ch):
    if ch["kind"] == "stop":
        return f"stop:{ch['series_id']}"
    return f"reduce_to:{ch['series_id']}:{fmt_plan(ch['new_amount'])}"


def format_plan(candidate):
    parts = [f"{d.isoformat()}:{fmt_plan(a)}" for d, a in candidate["plan"]]
    return "|".join(parts)


# ---------- Per-request decision ----------

def decide(request, profile, events, by_pair, options=None):
    request_date = parse_date(request["request_date"])
    req_amt = parse_dec(request["requested_amount"])
    home_ccy = profile["home_currency"]
    balance = parse_dec(profile["current_available_balance"])
    min_bal = parse_dec(profile["minimum_balance_to_keep"])

    user_events = [e for e in events if e["user_id"] == profile["user_id"]]
    user_events = apply_patches(user_events, _PATCHES_CACHE)
    user_events = collapse_linked(user_events)
    concrete, projected = build_horizon_flows(user_events, home_ccy, by_pair, request_date)
    baseline_flows = concrete + [(d, a) for d, a, _ in projected]
    cash = cash_path_from_flows(balance, baseline_flows, request_date)
    safe, earliest_offset = compute_safe_and_earliest(cash, min_bal, req_amt)

    earliest_date_str = ""
    if earliest_offset is not None:
        earliest_date_str = (request_date + timedelta(days=earliest_offset)).isoformat()

    options = options or []
    cands = enumerate_candidates(request, profile, safe, earliest_offset, options)
    safe_cands = [c for c in cands if plan_is_safe(cash, c["plan"], request_date, min_bal)]

    chosen = None
    changes = []
    if safe_cands:
        chosen = min(safe_cands, key=lambda c: rank_key(c, False))
    else:
        result = search_spending_changes(concrete, projected, balance, request_date,
                                         min_bal, cands, profile, home_ccy, by_pair)
        if result is not None:
            chosen, changes = result

    if chosen is None:
        return _format_not_recommended(request, profile, safe, earliest_date_str)

    return _format_from_candidate(request, profile, safe, earliest_date_str,
                                  chosen, changes)


def _format_not_recommended(request, profile, safe, earliest_date_str):
    home_ccy = profile["home_currency"]
    min_bal = parse_dec(profile["minimum_balance_to_keep"])
    deadline = parse_date(request["desired_completion_date"])
    return {
        "request_id": request["request_id"],
        "amount_safe_to_pay": fmt_natural(safe),
        "affordability_status": "not_affordable",
        "recommended_payment_method": "not_recommended",
        "payment_plan": "none",
        "earliest_date_for_full_payment": "",
        "spending_changes_needed": "none",
        "decision_explanation": (
            f"Do not make this payment by {pretty_date(deadline)}. "
            f"None of the available options keeps the {home_ccy} "
            f"{fmt_money_display(min_bal, home_ccy)} minimum protected."
        ),
    }


def _format_from_candidate(request, profile, safe, earliest_date_str, chosen, changes):
    home_ccy = profile["home_currency"]
    min_bal = parse_dec(profile["minimum_balance_to_keep"])
    req_amt = parse_dec(request["requested_amount"])
    method = chosen["method"]

    # Status
    if method == "full_payment":
        if not changes and safe >= req_amt:
            status = "affordable_now"
        else:
            status = "affordable_with_plan"
    elif method == "wait":
        status = "affordable_later"
    else:
        status = "affordable_with_plan"

    plan_str = format_plan(chosen)
    changes_str = "|".join(format_change(c) for c in changes) if changes else "none"

    # Explanation
    if status == "affordable_now":
        explanation = (
            f"Pay {home_ccy} {fmt_money_display(req_amt, home_ccy)} today. "
            f"This leaves at least {home_ccy} {fmt_money_display(min_bal, home_ccy)} "
            f"available over the next 90 days."
        )
    elif method == "installments":
        n = chosen["n"]
        first = chosen["start"]
        pay_amt = chosen["plan"][0][1]
        explanation = (
            f"Use {n} installments of {home_ccy} {fmt_money_display(pay_amt, home_ccy)}, "
            f"starting {pretty_date(first)}. This leaves at least {home_ccy} "
            f"{fmt_money_display(min_bal, home_ccy)} available."
        )
    elif method == "partial_payment":
        today_amt = chosen["plan"][0][1]
        remainder = chosen["plan"][1][1]
        later_date = chosen["plan"][1][0]
        explanation = (
            f"Pay {home_ccy} {fmt_money_display(today_amt, home_ccy)} today and the "
            f"remaining {home_ccy} {fmt_money_display(remainder, home_ccy)} on "
            f"{pretty_date(later_date)}. This completes the full request and keeps "
            f"the {home_ccy} {fmt_money_display(min_bal, home_ccy)} minimum protected."
        )
    elif method == "wait":
        pay_date = chosen["plan"][0][0]
        explanation = (
            f"Pay {home_ccy} {fmt_money_display(req_amt, home_ccy)} in full on "
            f"{pretty_date(pay_date)}. Paying earlier would take the balance below "
            f"the {home_ccy} {fmt_money_display(min_bal, home_ccy)} minimum."
        )
    else:  # affordable_with_plan + full_payment with changes
        verbs = []
        for c in changes:
            desc = c["series"]["description"].lower()
            if c["kind"] == "stop":
                verbs.append(f"Stop the {desc}")
            else:
                verbs.append(
                    f"Reduce the {desc} to {home_ccy} "
                    f"{fmt_money_display(c['new_amount'], home_ccy)}"
                )
        verb_phrase = " and ".join(verbs) if verbs else ""
        explanation = (
            f"{verb_phrase}, then pay {home_ccy} {fmt_money_display(req_amt, home_ccy)} today. "
            f"This leaves at least {home_ccy} {fmt_money_display(min_bal, home_ccy)} available."
        )

    return {
        "request_id": request["request_id"],
        "amount_safe_to_pay": fmt_natural(safe),
        "affordability_status": status,
        "recommended_payment_method": method,
        "payment_plan": plan_str,
        "earliest_date_for_full_payment": earliest_date_str,
        "spending_changes_needed": changes_str,
        "decision_explanation": explanation,
    }

# ---------- Main ----------

def main():
    global _PATCHES_CACHE
    _PATCHES_CACHE = load_patches()
    profiles = {p["user_id"]: p for p in load("financial_profiles")}
    events = load("financial_events")
    rates = load("exchange_rates")
    requests_rows = load("requests")
    options_rows = load("request_payment_options")
    by_pair = build_rates(rates)

    events_by_user = defaultdict(list)
    for e in events:
        events_by_user[e["user_id"]].append(e)
    options_by_request = defaultdict(list)
    for o in options_rows:
        options_by_request[o["request_id"]].append(o)

    rows = []
    for r in requests_rows:
        profile = profiles[r["user_id"]]
        rows.append(decide(r, profile, events_by_user[r["user_id"]], by_pair,
                           options_by_request[r["request_id"]]))

    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for row in rows:
            w.writerow(row)
    print(f"wrote {len(rows)} rows to {OUTPUT}")

if __name__ == "__main__":
    main()
