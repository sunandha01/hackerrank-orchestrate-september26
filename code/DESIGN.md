# DESIGN.md — Buy or Wait? Phase 2 design (no engine code)

Locked assumptions A–H from the Phase-2 prompt are treated as authoritative.
Section 3 corrects one stated invariant that samples contradict; the
contradiction is quoted in §8 (blocker B1) with the sample_id.

---

## 1. Ledger — what enters the 90-day timeline

### 1.1 Included cash flows

For each event of user `u`, decide `include`, `sign`, and `cash_date`:

| event_type | status | direction | include? | notes |
|---|---|---|---|---|
| `income` | `settled` | credit | **history only** | already reflected in `current_available_balance` |
| `income` | `scheduled` | credit | **yes**, +amount, on `settlement_date` | "next confirmed salary" and analogues — assumption A |
| `income` | `pending` | credit | **no** | assumption B (pending credits excluded) |
| `expense`, `subscription`, `debt_payment` | `settled` | debit | **history only** for recurrence inference; only pending/scheduled instances of the same recurring series enter the timeline directly | history informs the recurring-series projection |
| any | `pending` | debit | **yes**, −amount, on `settlement_date` | pending debits reserved |
| any | `scheduled` | debit | **yes**, −amount, on `settlement_date` | scheduled debits count |
| any | `cancelled` | any | **no** | dropped |
| any | `failed` | any | **no** | dropped |
| `investment_purchase` | `settled` | debit | **history only** | already priced into balance |
| `investment_sale` | `settled` | credit | **yes if `settlement_date > request_date`**, else history | cash-on-sale; recorded on `settlement_date` |
| `investment_valuation` | `unrealized` | non_cash | **no** | assumption B |
| `refund` | `settled` | credit | **history only** | reflected in balance |
| `refund` | `scheduled` | credit | **yes** on `settlement_date` | rare but treated symmetrically |

Only rows whose `cash_date ∈ (request_date, request_date + 90 days]` reach the
forward timeline. Rows on or before `request_date` are assumed baked into
`current_available_balance` and are used only for history / recurrence inference.

### 1.2 Recurring-series projection (conservative)

- A **series** is `(user_id, category, description, direction)`, restricted to
  `event_type ∈ {expense, subscription, debt_payment}` with `status=settled`.
- A series is **confirmed recurring** only if it has ≥ 3 historical occurrences
  spanning ≥ 60 days AND the median inter-arrival is in `[25, 35]` days.
  (No single-transaction inference — assumption per Phase-2 rule §1.)
- Its projected cadence is the median inter-arrival (rounded to `{28,30,31}`),
  and its projected amount is the **median of the trailing 6 occurrences**
  (median → conservative against 1-off spikes and short-tail noise).
- Its next occurrence is at `last_settled_date + median_cadence`; subsequent
  occurrences step forward until they exceed `request_date + 90 days`.
- Every projected occurrence carries the source series' category and the
  latest-observed `flexibility`, `minimum_allowed_amount`.
- Series with `flexibility=stoppable|reducible|reducible_or_stoppable` are
  projected **as debit** in the pre-change forecast; the ranker can later mark
  them stopped/reduced.
- Series whose next occurrence has a matching `scheduled` or `pending`
  concrete row on the ledger is **replaced** by that concrete row (dedupe).

Non-recurring settled expenses do not enter the future timeline.

### 1.3 `linked_event_id` collapse rules

Applied once, before ledger classification:

- `refund → expense`: if refund status is `settled` or `scheduled`, keep both
  the parent expense (history) and the refund credit (history/timeline).
- `expense → expense` (reissue): treat the child as authoritative. If the
  parent is later `cancelled` or `failed`, drop the parent.
- `investment_valuation → investment_purchase`: drop the valuation from cash
  flow (already excluded by assumption B); keep parent as history.
- `investment_sale → investment_purchase`: keep both. The sale is the cash
  event; the purchase is history.
- `debt_payment → debt_payment` where child is `scheduled` and parent is
  `failed`: parent dropped; child is the current obligation on its
  `settlement_date`.
- Cross-user links: none exist (verified in DATA_NOTES §5).

### 1.4 Missing `settlement_date`

- `settlement_date` is null on exactly 10 rows, all `non_cash` /
  `investment_valuation` / `unrealized`. Those are already excluded from cash
  flow by assumption B, so no fallback is needed for cash rows.
- If a cash row (`direction ∈ {debit, credit}`) is ever seen with a null
  `settlement_date`, the fallback is `event_date` (assumption A extension).
  This is logged.

### 1.5 De-duplication

Two rows for the same `(user_id, description, category, amount, settlement_date, direction)`
with `status=settled` are treated as the same event; the one with the lower
`event_id` wins. `linked_event_id` chains take precedence over this
similarity check.

### 1.6 Foreign-currency conversion

- Everything in the forecast is normalised to the user's `home_currency`.
- For a row with `currency != home_currency`, convert the amount using the FX
  rule in assumption D: latest `rate_date ≤ settlement_date` for the
  `(from, to)` pair, then inverse, then USD/EUR 2-hop cross.
- Route selection is deterministic: (1) direct pair → (2) reciprocal →
  (3) 2-hop via USD → (4) 2-hop via EUR. First successful route wins.
- Any fallback beyond direct is logged with `{event_id, from, to, route}`.

---

## 2. Formulas

Let `R` = request; `U` = user profile; `min_bal = U.minimum_balance_to_keep`;
`req_date = R.request_date`; `req_amt = R.requested_amount`;
`horizon = [req_date, req_date + 90 days]`.

### 2.1 Daily cash

Let `flows = [ (cash_date, signed_amount_home) ]` from §1 (scheduled,
pending, and projected recurring flows only; NOT applying any candidate
plan). Define the **baseline daily cash path**:

```
cash[req_date] = U.current_available_balance
cash[t]        = cash[t-1] + Σ signed_amount for flows with cash_date == t,
                 for t = req_date+1 .. req_date+90
```

`baseline_min = min(cash[t] for t in horizon)`.

### 2.2 `safe(plan)`

A `plan` is a list of `(pay_date, pay_amount)` outflows in home currency
(all pay_dates in `horizon`, all pay_amounts > 0). Define the **plan cash
path**:

```
cash_plan[t] = cash[t] − Σ pay_amount for plan entries with pay_date <= t
```

`safe(plan) ⇔ min(cash_plan[t] for t in horizon) ≥ min_bal`.

Optional spending changes modify `flows` by dropping or reducing specific
projected occurrences of stoppable/reducible series that fall inside the
horizon; then `cash[t]` is recomputed.

### 2.3 `amount_safe_to_pay` (pre-change, §G)

The largest `x ∈ [0, req_amt]` such that the single-payment plan
`[(req_date, x)]` is safe against the **pre-change** baseline. Closed-form:

```
slack = baseline_min − min_bal        # non-negative iff baseline already safe
amount_safe_to_pay = clip(slack, 0, req_amt)
```

(Because the plan's only outflow is on `req_date`, subtracting `x` shifts the
entire path down by `x`, so the min shifts by `x` too.)

### 2.4 `earliest_date_for_full_payment` (pre-change, §G)

The first `d ∈ horizon` for which `[(d, req_amt)]` is safe against the
pre-change baseline. Because a lump-sum at `d` only affects `cash[t]` for
`t ≥ d`:

```
tail_min[d] = min(cash[t] for t ≥ d in horizon)
earliest    = min { d : tail_min[d] − req_amt ≥ min_bal }
```

If no such `d` exists in horizon, `earliest = ""` (empty).

Rule cross-check: `earliest = req_date` iff `amount_safe_to_pay = req_amt`.

### 2.5 Cap

`0 ≤ amount_safe_to_pay ≤ req_amt` is enforced by the `clip(…, 0, req_amt)`
above.

### 2.6 Rounding and money math

- Money in `Decimal` throughout; only cast to `float` when writing CSV.
- Balance comparisons use a tolerance of `1e-6` in home currency.
- Output `amount_safe_to_pay` with the same significant digits as
  `req_amt` (see sample_requests: 25256, 17229139.20, 12510645.00 —
  matches the ledger scale, no artificial rounding).
- FX-converted values retain 2 decimal places for currencies with
  minor units and 0 decimals for IDR (heuristic: `IDR` has no
  sub-unit in practice on this dataset).

---

## 3. Status × method state machine (corrected against samples)

Only these `(affordability_status, recommended_payment_method)` pairs are
legal. Each row lists the plan shape and the invariants that must hold.

**Ruling accepted (2026-09-13, B1 resolved):**
- `wait` → `payment_plan = <earliest_date_for_full_payment>:<requested_amount>`
  — a single future payment of the full requested amount. Never `none`.
- `not_recommended` → `payment_plan = none`.
- `affordable_with_plan + full_payment` is legal when spending changes make
  full payment safe today (samples: request_06, 11, 21).
- `amount_safe_to_pay` and `earliest_date_for_full_payment` stay
  **pre-change**. They are never recomputed after cuts are applied.

| status | method | plan shape | invariants |
|---|---|---|---|
| `affordable_now` | `full_payment` | `req_date:req_amt` | `amount_safe_to_pay = req_amt`; `earliest = req_date`; `full_payment ∈ user.methods`; no changes |
| `affordable_with_plan` | `full_payment` | `req_date:req_amt` | needs `spending_changes_needed ≠ none`; with-changes safe today for req_amt; `amount_safe_to_pay < req_amt` (pre-change, unchanged after cuts); `full_payment ∈ user.methods` |
| `affordable_with_plan` | `partial_payment` | `req_date:S \| earliest:R−S` | `S = amount_safe_to_pay`; `R = req_amt`; `0 < S < R`; `earliest ≤ desired_completion_date`; `allows_partial_payment=true`; `partial_payment ∈ user.methods`; exactly 2 payments summing to `req_amt`; changes = none |
| `affordable_with_plan` | `installments` | one option verbatim | plan dates `= first_payment_date + k·frequency_days`, `k=0..n−1`; each amount `= payment_amount`; `number_of_payments ≤ max_installment_months`; `installments ∈ user.methods`; all payment dates safe against pre-change baseline; may or may not require changes (if required, they are attached but still bounded by 3, flexible-only, stop/reduce mutually exclusive per event) |
| `affordable_later` | `wait` | `earliest:req_amt` | **plan is one payment of `req_amt` on `earliest`, never `none`** (B1 resolved); `earliest > req_date`; `earliest ∈ horizon`; `full_payment ∈ user.methods`; changes = none |
| `not_affordable` | `not_recommended` | `none` | fallback; `earliest = ""`; changes = none |

Global invariants:

- `plan_dates ⊆ horizon`.
- `Σ plan_amount == req_amt` for full/partial/wait plans;
  `Σ plan_amount == total_payable_amount` for installments.
- `amount_safe_to_pay` and `earliest` are always the pre-change values
  regardless of the chosen method.
- Spending changes: ≤ 3 items; each targets an event with per-event
  `flexibility ∈ {reducible, stoppable, reducible_or_stoppable}` AND its
  category is in the profile's `willing_to_reduce` list (for `reduce_to`) or
  `willing_to_stop` list (for `stop`). `stop` and `reduce_to` may not both
  target the same `event_id`. `reduce_to:<event_id>:<new_amount>` must satisfy
  `new_amount ≥ minimum_allowed_amount` for that event.
- User methods intersect the option's `payment_method`. A method not in
  `payment_methods_user_will_consider` is ineligible even if safe.

---

## 4. Candidate generation and ranking

### 4.1 Candidate set (per request)

The set is small and enumerable:

1. **Full-today** `(req_date, req_amt)` — always considered.
2. **Partial-two-step** `(req_date, S) + (earliest, req_amt−S)` — considered
   iff `allows_partial_payment=true`, `0 < S < req_amt`,
   `earliest ≤ desired_completion_date`.
3. **Each installment option** — full schedule from
   `request_payment_options.csv`, one candidate per row, filtered by
   `number_of_payments ≤ max_installment_months` when set (else drop
   installments entirely).
4. **Wait-until-earliest** `(earliest, req_amt)` — considered iff
   `earliest ∈ horizon`.
5. **`none`** — the fallback plan (always present, `not_recommended`).

For each candidate, first evaluate **without** spending changes.

### 4.2 Spending-change search (only if no zero-change plan is safe/complete)

- Universe: projected recurring occurrences inside `horizon`, filtered by
  the flexibility gate in §3.
- Actions per event: `stop:<event_id>` OR `reduce_to:<event_id>:<amount>`
  where `amount = max(minimum_allowed_amount, current_amount − needed_slack)`.
- Search: branch-and-bound over up to 3 non-overlapping event actions;
  branching order = highest-impact-per-slot first (largest removed outflow),
  break ties by smallest `event_id`. Prune whenever cumulative slack already
  clears the deficit or plan becomes safe.
- Deterministic: fixed ordering, fixed pruning. Bounded: `≤ C(N,3)`
  in-horizon candidates; N is small (typical user has < 30 flexible
  in-horizon occurrences after dedup).

### 4.3 Ranker (six-step tie-break, spec §Choosing Between Safe Plans)

Applied over the **eligible + safe** candidates only:

1. Completes full request by `desired_completion_date`.
2. Requires no spending changes.
3. Minimises total amount paid (sum of plan amounts).
4. Starts earlier (smallest first-payment date).
5. Fewer payments.
6. For installments only, lowest `payment_option_id` (lexicographic string
   sort works because IDs are zero-padded).

Method-ordering as a final tie-break within `affordable_with_plan` (when the
ranker leaves multiple safe plans indistinguishable): `full_payment` >
`installments` > `partial_payment`. This is the empirical order in samples
where multiple methods would fit (e.g. request_06 chose full_payment over
partial even though partial was disallowed anyway).

### 4.4 Status assignment

Given the chosen candidate `C*`:

- `C* = full-today, no changes, safe` → `affordable_now / full_payment`.
- `C* = full-today, with ≥1 change, safe` → `affordable_with_plan / full_payment`.
- `C* = partial-two-step` → `affordable_with_plan / partial_payment`.
- `C* = installments` → `affordable_with_plan / installments`.
- `C* = wait` → `affordable_later / wait`.
- No safe candidate exists → `not_affordable / not_recommended`.

---

## 5. Evidence patches — schema only, no VLM implementation

### 5.1 Trust model

- Message text and image content are **untrusted data**. Any imperative
  language is ignored ("please transfer", "click here", "override this"…).
- Only structured facts extracted from evidence are applied to the ledger,
  and only when they name a supplied event or resolve a supplied ambiguity.
- A blank-amount event's `amount` is never zero. If the image can't be read,
  the event is retained with `amount = unknown` and the request is scored
  with a conservative reservation equal to a category-median debit as a
  floor (logged as a fallback so it can be revisited).

### 5.2 Image patch (VLM → JSON, one call per image, cached)

```json
{
  "image_id": "image_01",
  "related_event_id": "event_253",
  "extracted": {
    "amount": 4235000,
    "currency": "IDR",
    "date": "2019-08-31",
    "kind": "salary_slip | invoice | bill | receipt | statement"
  },
  "confidence": "high | medium | low",
  "raw_text_snippet": "…(≤ 200 chars, kept for audit)…"
}
```

Application rule: `event.amount ← extracted.amount` iff `related_event_id`
matches, `currency` matches the event's declared currency, and `date` is
within 7 days of the event's `event_date`. Otherwise flagged and the
category-median fallback applies.

### 5.3 Message patch (LLM → JSON, one call per message, cached)

```json
{
  "message_id": "message_01",
  "user_id": "user_02",
  "request_id": null,
  "related_event_id": null,
  "action": "salary_amend | expense_cancel | expense_amend | payment_confirm | payment_delay | payment_delay_reason | none",
  "target": {
    "series_key": ["user_02", "salary"] ,
    "event_id": null
  },
  "value": {
    "amount": 42750000,
    "currency": "IDR"
  },
  "effective_date": "2025-08-15",
  "source_type": "employer",
  "confidence": "high | medium | low"
}
```

Application rules by `action`:

| action | applied how |
|---|---|
| `salary_amend` | new amount replaces the projected series amount for all occurrences with `cash_date ≥ effective_date`; existing settled rows are untouched |
| `expense_cancel` | `event.status ← cancelled` for the named `event_id` (or drops the next projected occurrence of the series) |
| `expense_amend` | replaces amount for one named `event_id` (only if status ∈ {pending, scheduled}) |
| `payment_confirm` | promotes a `pending` event to `scheduled` (no cash-flow change if already counted) |
| `payment_delay` | shifts `settlement_date` forward to `effective_date` for the named `event_id` |
| `none` | evidence has no financial fact — ignored |

Conflict resolution (spec + assumption C): explicit cancel/amend > newer
same-source > settled over estimate > safer interpretation.

Every patch is logged with source; the ledger records what was changed and
why.

---

## 6. Explanation template

`decision_explanation` is generated from a small set of structured slots.
No LLM prose. Templates below (Python f-string sketch):

- `affordable_now / full_payment`
  `Pay {currency} {req_amt} today. This leaves at least {currency} {min_bal} available over the next 90 days.`

- `affordable_with_plan / installments`
  `Use {n} installments of {currency} {pay_amt}, starting {first_pretty}. This leaves at least {currency} {min_bal} available.`

- `affordable_with_plan / partial_payment`
  `Pay {currency} {safe_today} today and the remaining {currency} {remainder} on {earliest_pretty}. This completes the full request and keeps the {currency} {min_bal} minimum protected.`

- `affordable_with_plan / full_payment (with changes)`
  `{Change verb phrase}, then pay {currency} {req_amt} today. This leaves at least {currency} {min_bal} available.`
  where the change verb phrase is joined by `and`: `Stop the {desc}` /
  `Reduce the {desc} to {currency} {new_amount}`.

- `affordable_later / wait`
  `Pay {currency} {req_amt} in full on {earliest_pretty}. Paying earlier would take the balance below the {currency} {min_bal} minimum.`

- `not_affordable / not_recommended`
  `Do not make this payment by {deadline_pretty}. None of the available options keeps the {currency} {min_bal} minimum protected.`

Slots come only from: `currency = profile.home_currency`,
`min_bal = profile.minimum_balance_to_keep`, `req_amt = request.requested_amount`,
`safe_today = amount_safe_to_pay`, `remainder = req_amt − safe_today`,
`earliest_pretty = strftime("%-d %B %Y", earliest)`,
`deadline_pretty = strftime("%-d %B %Y", desired_completion_date)`,
`n = number_of_payments`, `pay_amt = payment_amount`,
`first_pretty = strftime("%-d %B %Y", first_payment_date)`, and per-change
`desc = event.description` (mapped through a small stopword list, e.g.
`"Family streaming plan"`).

Amounts are formatted with the same conventions as `sample_requests.csv`:
thousands separators for INR/ZAR/EUR/USD, no separators for IDR (matches
observed style: `ZAR 25,256`, `IDR 46,018,000`).

---

## 7. Sample probes — 5 samples across the state machine

Each probe walks the design against the actual joined evidence. If the
design produces the published output, the probe passes; if not, it becomes a
blocker in §8.

### 7.1 request_01 — `affordable_now / full_payment`

- Profile: home ZAR, bal 58,481.10, min 18,000, methods = `full_payment`.
- Ledger (horizon 2024-03-03 .. 2024-05-31): +1 scheduled income 23,320 on
  2024-03-15; +1 pending expense 567.60 on 2024-03-05; ~10 projected
  recurring debits (rent 5,148 on 2024-04-02, groceries series, subscriptions).
- Baseline `min(cash[t])` in horizon comfortably above 18,000 (opening cash
  minus recurring floor stays well over min).
- `slack ≥ 25,256` → `amount_safe_to_pay = 25,256` = `req_amt`.
- `earliest = 2024-03-03`. Full-today safe with no changes.
- Method eligibility: user considers only `full_payment` → other candidates
  dropped. `Ranker` picks full-today → `affordable_now / full_payment /
  2024-03-03:25256 / earliest=2024-03-03 / changes=none`. ✅ matches sample.

### 7.2 request_02 — `affordable_with_plan / installments`

- Profile: home IDR, bal 60,383,889.20, min 29,158,400, methods =
  `partial_payment|installments`, `max_installment_months=7`.
- Ledger: +1 pending debit 1,651,100 on 2025-08-08; recurring rent
  ~7,200,000/month, utilities, subscriptions; scheduled/upcoming salary from
  employer message → amount patch to 42,750,000 IDR effective 2025-08-15
  (message_01). Result: baseline `min(cash[t])` sits low around late Aug but
  recovers after the 15-Sep salary.
- Pre-change `amount_safe_to_pay`: closed-form → 17,229,139.20 (matches
  sample exactly).
- `earliest_date_for_full_payment` (lump-sum of 46,018,000): first date
  after the 15-Sep salary makes the tail min pass floor → **2025-09-15**.
  ✅ matches.
- Eligible plans:
  - full-today: safe? `amount_safe_to_pay = 17.2M < 46M` → unsafe; also,
    `full_payment ∉ user.methods` → dropped.
  - partial-two-step: `allows_partial_payment=false` → dropped.
  - installments: `payment_option_05` (3×15.95M @ 30d, 2025-08-08) with
    `n=3 ≤ 7`; verify each installment date safe under baseline (needs the
    Aug-15 salary raise for the 8-Sep payment). `payment_option_07` (18×) is
    `n=18 > 7` → dropped.
  - wait: `full_payment ∉ user.methods` → dropped.
- Ranker: only one eligible plan → `installments / payment_option_05 verbatim`.
- Status = `affordable_with_plan`. ✅ matches sample.

### 7.3 request_19 — `affordable_with_plan / partial_payment`

- Profile: home INR, min 92,800, methods include `partial_payment`
  (verified in probe), `allows_partial_payment=true`.
- Pre-change `amount_safe_to_pay = 28,820`; `earliest = 2024-09-15`
  (sample). Requested 39,660; deadline 2024-09-??-ish, `earliest ≤ deadline`.
- Candidates:
  - full-today: safe = 28,820 < 39,660 → unsafe without changes; try
    spending changes — samples say no changes were needed here.
  - partial-two-step: safe today S = 28,820; remainder 10,840 on 2024-09-15;
    `S + remainder = 39,660 = req_amt`. Safe over horizon → candidate.
  - installments: whatever the option table shows; ranker will compare.
  - wait: `earliest ≤ deadline`; would leave user without partial usage of
    today's slack; ranker will rank it against partial.
- Ranker: partial completes by deadline (rank 1 tie with installments and
  wait); no changes (tie); minimises total = `req_amt` for partial and wait,
  vs `total_payable > req_amt` for any installment → installments lose on
  rank 3; partial vs wait tie on rank 3, tie on rank 4 (partial starts
  earlier on `req_date` < earliest), so **partial wins**.
- Plan: `2024-09-04:28820|2024-09-15:10840`. ✅ matches sample.

### 7.4 request_03 — `affordable_later / wait`

- Profile: home IDR, bal 5,810,300, min 2,668,700, methods = `full_payment|
  partial_payment|installments`, `max_installment_months=2`,
  `allows_partial_payment=false`.
- Blank-amount `event_253` (August 2019 net salary) → VLM patch supplies
  the amount from `image_01.png`. This settled income is history and
  already in balance if the patch matches; otherwise it affects the
  history-derived recurring pattern but not future timeline directly.
- Pre-change: `amount_safe_to_pay = 873,000` (pre-change slack); requested
  5,491,000. Deadline 2019-11-15; horizon reaches 2019-12-02.
- `earliest_date_for_full_payment`: first date after enough future salaries
  accumulate → **2019-11-15** (the first mid-month salary that lifts tail
  min above `min + req_amt`).
- Candidates:
  - full-today: unsafe.
  - partial: `allows_partial_payment=false` → dropped.
  - installments: `payment_option_09` (n=21) and `_10` (n=24) both violate
    `max_installment_months=2` → dropped.
  - wait: `earliest = 2019-11-15 > req_date`, `∈ horizon`,
    `full_payment ∈ user.methods` → candidate. Plan = `2019-11-15:5491000`.
- Ranker: only wait remains → `affordable_later / wait /
  2019-11-15:5491000 / earliest=2019-11-15 / changes=none`. ✅ matches.

### 7.5 request_05 — `not_affordable / not_recommended`

- Profile: home ZAR (probe), methods = `full_payment|partial_payment|
  installments`, `allows_partial_payment=false`.
- Pre-change: `amount_safe_to_pay = 737` (small slack); requested 15,488;
  `earliest = ""` (never safe in 90 days).
- Candidates:
  - full-today: `737 < 15,488` → unsafe without changes; spending-change
    search over user's reducible/stoppable events yields insufficient
    slack (< 14,750 total in-horizon) → no full-today plan is safe.
  - partial: `allows_partial_payment=false` → dropped.
  - installments: enumerate options; require all installment dates to be
    safe against baseline. Each installment (>~1,300) drives tail below
    min → all unsafe.
  - wait: `earliest = ""` → dropped.
- No safe candidate → `not_affordable / not_recommended / none / "" /
  none`, `amount_safe_to_pay = 737` (pre-change, unchanged). ✅ matches.

---

## 8. Open questions (blockers after locked assumptions)

### B1 — `wait` plan is non-empty; contradicts Phase-2 §3 invariant

**Sample evidence:** `request_03` shows
`recommended_payment_method=wait`, `payment_plan="2019-11-15:5491000"`.
All 6 wait samples (`request_03, 04, 08, 13, 18, 23`) follow the same
pattern: a single payment of `requested_amount` on
`earliest_date_for_full_payment`. The Phase-2 prompt asserts
`wait ⇒ payment_plan=none`. **Design uses the sample-derived rule
(one future payment on `earliest`).** Confirm this is the intended contract
before we serialise output.

### B2 — Recurrence-inference thresholds

The design uses ≥ 3 occurrences over ≥ 60 days with median inter-arrival
in `[25, 35]` days. Samples don't reveal the exact threshold used by the
organizer. **Sample evidence that would settle it:** any sample where the
90-day floor becomes binding around a borderline series (e.g. a series
with 2 occurrences 32 days apart) — we'd back out the threshold from
whether the projection included that series. None of the 5 probes hit
this boundary cleanly. To be re-checked once the engine runs.

### B3 — FX date-matching rule

Assumption D says "latest `rate_date ≤ settlement_date`". Rates are
monthly on the 15th; some samples with cross-currency events would let us
back-check whether the organizer used the same-month 15th or the previous
15th when settlement falls before the 15th. **Sample evidence:** any
sample whose baseline min pivots around the 15th of a month with a
non-home-currency event on either side. Will confirm when the engine runs
on samples 6–25.

### B4 — Salary "amend from message" application scope

For `request_02`, `message_01` raises salary to 42,750,000 effective
2025-08-15. Design applies this to all `income` occurrences with
`cash_date ≥ 2025-08-15`. But the message text says "next payslip", which
might be interpreted as *only the immediately-next* paycheck (not all
future ones). **Sample evidence:** `request_02`'s
`amount_safe_to_pay=17,229,139.20` and `earliest=2025-09-15` should
distinguish. Reproducing this exact figure at engine time will tell us
which interpretation is correct.

### B5 — Full-payment-with-changes vs partial-payment

Both are `affordable_with_plan`. Samples show request_06/11/21 use
full_payment with changes rather than partial; request_19 uses partial
with no changes. The design ranks by (completes-by-deadline, no-changes,
lower-total, earlier-start, fewer-payments, option_id). No-changes beats
with-changes only after "completes by deadline", which both satisfy. So
partial wins over full-with-changes when both are safe. **Sample
evidence:** request_11 has `allows_partial_payment=false`, so partial was
ineligible; request_06 same; request_21 same. So the "with-changes"
outcome only occurs when partial is disallowed. Design consistent.
Confirming here so nothing is silently ignored.

---

## Appendix A — Ordering of the pipeline

```
load CSVs
  → apply linked_event_id collapse
  → apply image patches (VLM) to blank-amount events
  → apply message patches (LLM) to ledger series
  → convert non-home-currency events to home currency
  → per request:
       build baseline horizon flows (no plan, no changes)
       compute amount_safe_to_pay, earliest_date_for_full_payment
       enumerate candidates (§4.1); test each without changes
       if none safe/complete: run spending-change search (§4.2)
       rank eligible+safe candidates (§4.3); assign status (§4.4)
       generate decision_explanation (§6)
  → validator (§3 invariants + §5.1 blank-amount safeguard)
  → write output.csv
  → write evaluation/usage_report.md
```

## Appendix B — Determinism

- All sorts use fully-specified keys.
- All Decimal comparisons use a tolerance of `1e-6` in home currency.
- FX route selection is priority-ordered; ties within a priority are
  impossible because pair + rate_date is unique.
- Spending-change search is DFS with a fixed branching order
  (highest-impact first, then smallest `event_id`).
- Every VLM/LLM call is cached on `(image_id or message_id, model_version)`
  and read-through.
