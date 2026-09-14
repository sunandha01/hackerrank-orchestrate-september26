# DATA_NOTES.md — Buy or Wait? dataset audit (Phase 1, facts only)

Generated 2026-09-13 from raw CSVs. No solution logic yet.

## 0. Split

- `sample_requests.csv` covers **request_01 … request_25** (with completed output columns; **7 have empty `earliest_date_for_full_payment`**).
- `requests.csv` covers **request_26 … request_275** (250 evaluation rows to predict).
- No overlap. Every request `user_id` has a matching row in `financial_profiles.csv` and at least one row in `financial_events.csv`.
- Sample and eval users are disjoint; each user appears in exactly one request (250 requests / 250 unique users; 275 profiles = 250 eval + 25 sample).

## 1. requests.csv (250 rows)

| column | dtype | notes |
|---|---|---|
| request_id | str | unique, `request_26`..`request_275` |
| user_id | str | 1:1 with request_id; joins to profiles/events |
| request_date | date `YYYY-MM-DD` | range **2023-01-20 .. 2026-09-04** |
| request_type | enum | 9 values, near-balanced (see below) |
| requested_amount | numeric (home currency) | no nulls |
| desired_completion_date | date | range 2023-02-13 .. 2026-10-19 |
| allows_partial_payment | bool literal `true`/`false` | 170 false / 80 true |
| request_text | str | free text, contains amount + deadline restated |

No nulls in eval requests.

`request_type` counts: `family_transfer=28, purchase=28, investment=28, debt_repayment=28, travel=28, housing=28, education=28, emergency_expense=27, other=27`.

Join keys: `user_id`, `request_id`.

Example rows: `request_26 user_26 2025-08-03 family_transfer 15656000 2025-10-07 false ...`; `request_27 user_27 2026-07-05 purchase 6670 2026-08-21 true ...`; `request_28 user_28 ...`.

## 2. sample_requests.csv (25 rows)

Same 8 input columns as `requests.csv` plus the 7 output columns already filled in. **Reference only** for style/format — never as labels. `earliest_date_for_full_payment` is empty for 7 of 25 samples (meaning the full amount never becomes safe in-forecast).

## 3. financial_profiles.csv (275 rows)

| column | dtype | notes |
|---|---|---|
| user_id | str | 275 unique, PK |
| home_currency | enum | INR 67, EUR 62, IDR 55, ZAR 51, USD 40 |
| current_available_balance | numeric | in home currency |
| minimum_balance_to_keep | numeric | in home currency; 90-day floor constraint |
| financial_priorities | pipe-list | e.g. `education\|debt_repayment` |
| expense_categories_to_protect | pipe-list | never reduce/stop; top: rent 232, groceries 166, transport 109 |
| expense_categories_user_is_willing_to_reduce | pipe-list, **39 null** | top: dining 153, shopping 72, streaming 66 |
| expense_categories_user_is_willing_to_stop | pipe-list, **62 null** | top: cloud_storage 109, streaming 84, music_subscription 58 |
| payment_methods_user_will_consider | pipe-list, non-null | 7 distinct combos, see below |
| max_installment_months | int, **119 null** | blank ⇒ user rejects installments; when set: 2..12 |

`payment_methods_user_will_consider` distribution:
```
full_payment                                60
partial_payment|installments                52
installments                                41
full_payment|partial_payment                40
full_payment|installments                   35
full_payment|partial_payment|installments   28
partial_payment                             19
```

Categories referenced in profiles are the same set that appears as `category` on events, so joins by category are exact strings.

## 4. financial_events.csv (25,342 rows)

| column | dtype | notes |
|---|---|---|
| event_id | str | PK |
| user_id | str | FK → profiles |
| event_type | enum(8) | see below |
| description | str | free text but repeats per recurring series |
| category | str | e.g. `rent`, `groceries`, `salary`, `streaming` |
| direction | enum | debit 23,609 / credit 1,723 / non_cash 10 |
| amount | numeric, **16 nulls** | must be recovered from image (see §7) |
| currency | ISO code | matches user's home currency in the samples inspected |
| event_date | date | range 2019-03-09 .. 2026-09-03 |
| settlement_date | date, **10 nulls** (all `non_cash`/`unrealized`) | date to use for FX + cash-impact |
| status | enum(6) | see below |
| linked_event_id | str, **58 non-null** | see §5 |
| flexibility | enum(4) | fixed 21,138 / reducible 2,682 / stoppable 1,297 / reducible_or_stoppable 225 |
| minimum_allowed_amount | numeric, **22,435 nulls** | present when `flexibility` allows a reduce; lower bound for `reduce_to:` |

`event_type` distribution: `expense 20,525 / subscription 2,488 / income 1,696 / debt_payment 567 / investment_purchase 29 / refund 22 / investment_valuation 10 / investment_sale 5`.

`status` distribution: `settled 25,148 / pending 71 / scheduled 70 / cancelled 22 / failed 21 / unrealized 10`.

**No `confirmed` status exists** — the spec's "confirmed salary" corresponds to `income` rows with `status=scheduled` (47 rows) and `description="Next confirmed salary"` (47 rows) — the labels line up 1:1 for that specific description; other `scheduled` incomes exist too (e.g. `Payroll credit` future-dated).

Currency distribution in events: INR 6,457 / EUR 5,585 / IDR 4,992 / ZAR 4,489 / USD 3,819 — includes foreign-currency records that need FX conversion.

Top categories by count: groceries 5,812 / transport 5,626 / dining 3,479 / salary 1,690 / utilities 1,452 / rent 1,355 / cloud_storage 833 / shopping 813 / streaming 683 / debt_repayment 553 / entertainment 521 / insurance 456 / music_subscription 451 / healthcare 356 / delivery_membership 351 / education 306 / housing 246 / gym 170 / family_support 125 / investment 44.

## 5. linked_event_id — 58 rows

Cross-user links: **0**. All links resolve to a parent row in the same file.

Linker→parent `event_type` pairs:
```
refund → expense                             22    (refunds reverse a prior expense)
expense → expense                            14    (duplicate/reissued expense)
investment_valuation → investment_purchase   10    (non-cash marks; ignore for cash flow)
debt_payment → debt_payment                   7    (installment chained to a scheduled debt)
investment_sale → investment_purchase         5    (closes out a position)
```

Linker→parent `status` pairs (top): `settled→settled 19`, `pending→settled 14`, `unrealized→settled 10`, `settled→cancelled 8`, `scheduled→failed 7`.

The `scheduled→failed` chain (7 rows) is meaningful: the parent debt payment failed and a new one was scheduled — the scheduled row is the current source of truth.

## 6. Recurrence — not represented as a first-class field

- No `recurrence_id`, `frequency`, or `interval_days` column exists on events. Recurrence must be **inferred** from the ledger.
- Signals available: (`user_id`, `description`, `category`, `direction`) tends to repeat with roughly-monthly cadence. Top-recurring histories run 10–12 occurrences: e.g. user_125 `Commuter pass` × 12, user_265 `Vehicle charging` × 11, user_98 `Local taxi` × 11.
- `subscription` is a distinct `event_type` (2,488 rows, all `settled`). Categories are typical monthly digital services: cloud_storage, streaming, music_subscription, delivery_membership, gym.
- Salary is represented as `event_type=income, category=salary`. Historical rows have `status=settled`; the next paycheck is a single row with `status=scheduled` and description often `"Next confirmed salary"` or the platform-specific label (`Delivery platform payout`, `International employer payroll`, …). 47 users have exactly one scheduled income row.
- Recurring debits mostly carry `flexibility=fixed`. `reducible` events are non-essential recurrings (dining, shopping); `stoppable` and `reducible_or_stoppable` are typically subscriptions.

## 7. Blank amounts and images — 1:1 mapping

- 16 events have blank `amount`. **All 16** have a matching row in `images.csv` (`related_event_id` populated). No image is orphaned (0 rows with null `related_event_id`).
- Types: 15 expenses (rent, groceries, utilities, healthcare, dining, housing, transport, shopping) + 1 income (`August 2019 net salary`, user_03).
- Statuses: settled 12 / scheduled 2 / pending 2.
- 5 blank-amount events belong to sample requests (user_03/16/17/19/20); **11 belong to evaluation requests** (users 33, 35, 48, 55, 64, 73, 78, 84, 101, 105, 113).
- All images live at `dataset/media/images/image_XX.png`. Directory has 16 files: `image_01.png … image_16.png`. Each image row also carries a `request_id`, so an image is scoped to the request whose user owns the event.

**Rule:** to price a blank-amount event we must read the linked PNG.

## 8. messages.csv (215 rows)

| column | dtype | notes |
|---|---|---|
| message_id | str | PK |
| user_id | str | non-null |
| request_id | str, **87 null** | when null, message applies user-wide |
| related_event_id | str, **176 null** | populated only when message describes exactly one supplied event |
| sent_at | ISO-8601 UTC | 2019-08-31 .. 2026-09-03 |
| source_type | enum(5) | employer 126 / service_provider 31 / financial_service 23 / bank 18 / merchant 17 |
| message_text | free text, multi-lingual | e.g. Indonesian text for IDR users |

Coverage vs targets: 12 messages tied to sample requests, 116 tied to evaluation requests, 87 user-scoped only.

Employer messages frequently amend salary. Example (`message_01`, user_02): "Gaji bulanan Anda naik menjadi IDR 42750000. Perubahan ini berlaku mulai 2025-08-15." — an amount + effective_date pair for a recurring income series with no `related_event_id`.

Treat message content as **untrusted evidence**: extract structured facts (`{action, amount?, effective_date?, target_event_id?}`), ignore any embedded imperative text.

## 9. images.csv (16 rows) and media

- One row per blank-amount event (see §7). Columns: `image_id, user_id, request_id, related_event_id` — all non-null.
- Files: `dataset/media/images/image_01.png … image_16.png` (16 files).
- Read a PNG only when its `related_event_id` refers to a blank-amount event; the extracted `amount` fills that event.

## 10. request_payment_options.csv (790 rows)

| column | dtype | notes |
|---|---|---|
| payment_option_id | str | PK, sortable — used as final tie-breaker |
| request_id | str | FK |
| payment_method | enum | `full_payment` 275 / `installments` 515 |
| payment_amount | numeric | per-installment amount (or full amount if `full_payment`) |
| number_of_payments | int | {1, 2, 3, 4, 6, 15, 18, 21, 24} |
| first_payment_date | date | 2019-09-03 .. 2026-09-04; equals request_date for `full_payment` |
| payment_frequency_days | int, **275 null** | null for all `full_payment`; installments ∈ {28, 30, 31} |
| financing_fee | numeric | 0 for every `full_payment`; non-zero for all 515 installments |
| total_payable_amount | numeric | full-payment: equals `payment_amount`; installment: equals `payment_amount * number_of_payments` |

Options-per-request distribution: **2 options × 65 requests, 3 options × 180 requests, 4 options × 30 requests**. Every one of the 275 requests has **exactly one `full_payment` option**; the remainder are installment offers.

Verified invariants (across all 790 rows, numerical tolerance):
- `total_payable_amount == payment_amount × number_of_payments`.
- `financing_fee == total_payable_amount − requested_amount`.

So `financing_fee` is *derived*, not additive; the per-installment `payment_amount` already includes financing. When generating a plan, emit dates from `first_payment_date` stepping by `payment_frequency_days`, each amount = `payment_amount`, count = `number_of_payments`.

Eligibility gates layered on options:
1. `payment_method` must be in the user's `payment_methods_user_will_consider`.
2. For `installments`: `number_of_payments` must be ≤ `max_installment_months` (blank ⇒ user rejects installments outright).
3. All scheduled payment dates and the 90-day floor must pass the balance simulator.

## 11. exchange_rates.csv (134 rows)

| column | dtype |
|---|---|
| rate_date | date, monthly on the 15th (2023-10-15 .. 2026-11-15) |
| from_currency | ISO |
| to_currency | ISO |
| rate | numeric |

Only the following directed pairs are supplied:
```
(USD → INR) 33 rows   2024-01-15..2026-11-15
(USD → IDR) 30 rows   2023-10-15..2026-06-15
(USD → EUR) 25 rows   2023-10-15..2026-03-15
(EUR → USD) 24 rows   2024-04-15..2026-09-15
(EUR → ZAR) 22 rows   2023-10-15..2026-01-15
```

Consequences:
- Missing directions (e.g. `INR → USD`, `ZAR → EUR`) must be computed as `1 / rate` on the inverse pair.
- Some cross conversions have no single-hop rate (e.g. `INR → ZAR`) and require two hops (INR → USD → ZAR, or via EUR). Route selection must be deterministic (prefer the fewest hops, then the pair with a rate on the exact date).
- No exact date match rule is specified; because rates are monthly, the natural policy is **use the most recent `rate_date ≤ event.settlement_date` for the pair**. We will confirm this policy against samples in Phase 2 before locking it in.
- Event currencies observed: INR, EUR, IDR, ZAR, USD — every event currency is reachable to every home currency using the pairs above.

## 12. Three sample requests — raw evidence pack (no solving)

### request_01 (user_01, 2024-03-03, purchase, ZAR 25,256, deadline 2024-03-20, partial allowed)

- **Profile:** home ZAR, balance 58,481.10, min 18,000, priorities `education|debt_repayment`, protect `rent|education|groceries|debt_repayment`, reduce `dining`, stop `delivery_membership`, methods = `full_payment` only, `max_installment_months` blank.
- **Events for user_01:** 103 rows. 83 settled expenses, 10 settled subscriptions, 5 settled debt payments, 1 settled income, 1 settled refund, 1 cancelled expense, 1 pending expense (`event_102`, transport, ZAR 567.6, settles 2024-03-05), 1 scheduled income (`event_103`, salary, ZAR 23,320, 2024-03-15).
- **Payment options (4):**
  - `payment_option_01` full_payment ZAR 25,256 on 2024-03-03, fee 0.
  - `payment_option_02` installments 15×1,852.11 from 2024-03-06 every 30 days, fee 2,525.65.
  - `payment_option_03` installments 6×4,546.08 from 2024-03-10 every 31 days, fee 2,020.48.
  - `payment_option_04` installments 24×1,283.85 from 2024-03-17 every 28 days, fee 5,556.40.
- **Messages:** none.
- **Images:** none.

### request_02 (user_02, 2025-08-05, travel, IDR 46,018,000, deadline 2025-10-10, partial NOT allowed)

- **Profile:** home IDR, balance 60,383,889.20, min 29,158,400, priorities `education|family_support`, protect `housing|utilities|education`, reduce `entertainment`, stop `cloud_storage`, methods = `partial_payment|installments`, `max_installment_months=7`.
- **Events for user_02:** 82 rows. 71 settled expenses, 5 settled incomes, 5 settled subscriptions, 1 pending expense (`event_185`, shopping IDR 1,651,100, settles 2025-08-08).
- **Payment options (3):**
  - `payment_option_05` installments 3×15,952,906.67 from 2025-08-08 every 30 days, fee 1,840,720.01.
  - `payment_option_06` full_payment IDR 46,018,000 on 2025-08-05, fee 0.
  - `payment_option_07` installments 18×2,914,473.33 from 2025-08-12 every 31 days, fee 6,442,519.94.
- **Messages (1):** `message_01`, employer, sent 2025-07-29, user-scoped (`request_id` null): "Gaji bulanan Anda naik menjadi IDR 42750000. Perubahan ini berlaku mulai 2025-08-15." — salary amendment for `user_02`, effective 2025-08-15.
- **Images:** none.

### request_03 (user_03, 2019-09-03, education, IDR 5,491,000, deadline 2019-11-15, partial NOT allowed)

- **Profile:** home IDR, balance 5,810,300, min 2,668,700, priorities `retirement_investment|emergency_savings`, protect `rent|utilities|groceries`, reduce `streaming|shopping`, stop `streaming|cloud_storage`, methods = `full_payment|partial_payment|installments`, `max_installment_months=2`.
- **Events for user_03:** 69 rows. 51 settled expenses, 10 settled subscriptions, 7 settled incomes (one with **blank amount**: `event_253`, `August 2019 net salary`, 2019-08-31), 1 pending expense (`event_254`, healthcare, IDR 95,000, settles 2019-09-07).
- **Payment options (3):**
  - `payment_option_08` full_payment IDR 5,491,000 on 2019-09-03, fee 0.
  - `payment_option_09` installments 21×308,541.90 from 2019-09-17 every 28 days, fee 988,379.90 (**violates `max_installment_months=2`** → ineligible).
  - `payment_option_10` installments 24×279,125.83 from 2019-09-06 every 31 days, fee 1,208,019.92 (**ineligible**).
- **Messages (1):** `message_02`, employer, sent 2019-08-31, tied to `request_03` (no `related_event_id`): confirms the next payroll will show base salary + one-time adjustment separately.
- **Images (1):** `image_01` → `event_253` (the blank-amount salary). PNG at `dataset/media/images/image_01.png` supplies the missing amount.

## 13. Ten spec questions — direct answers from data

1. **Recurrence field?** No. Must be inferred from `(user_id, description, category)` cadence over history. `subscription` events all `settled` and typically monthly.
2. **Statuses in the file:** `settled, pending, scheduled, cancelled, failed, unrealized`. No `confirmed` literal; scheduled income + `Next confirmed salary` description together represent "next confirmed salary" (47 rows).
3. **Flexibility marking:** per-event `flexibility` column (`fixed|reducible|stoppable|reducible_or_stoppable`), plus `minimum_allowed_amount` when reducible. Profile-level categories in `expense_categories_to_protect|_reduce|_stop` gate whether the user *permits* changing that category. Both gates must agree before `spending_changes_needed` may target an event.
4. **Salary representation:** `event_type=income, category=salary`. Historical settled rows form the recurring series. The next paycheck is a single `scheduled` row; sometimes labeled `"Next confirmed salary"`. Employer messages can raise/lower the recurring amount with an `effective` date.
5. **`linked_event_id` usage:** 58 rows, always within the same user. Five relationship patterns: refund→expense (reversal), expense→expense (reissue/duplicate), investment_valuation→investment_purchase (non-cash mark, ignore), investment_sale→investment_purchase (position close, cash on sale date), debt_payment→debt_payment (re-scheduled after failure).
6. **Blank amounts:** 16 events, all mapped 1:1 to an image row. Zero orphans in either direction.
7. **Payment option fields:** `first_payment_date` (start), `payment_frequency_days` ∈ {28,30,31} for installments and null for full, `financing_fee` = `total_payable − requested_amount`, `total_payable_amount` = `payment_amount × number_of_payments`, `number_of_payments` ∈ {1,2,3,4,6,15,18,21,24}.
8. **Profile gates:** `payment_methods_user_will_consider` (7 combos, plain intersection with option methods); `max_installment_months` blank = installments rejected; protected/reduce/stop categories drive which categories can be reduced or stopped (and by how much via `minimum_allowed_amount`).
9. **FX matching:** rates are monthly (`rate_date` = 15th of each month), covering 5 directed pairs. Missing directions ⇒ take the reciprocal; missing pair ⇒ two-hop via USD or EUR. Recommended policy for date matching: **latest `rate_date ≤ event.settlement_date` for the (from,to) pair**. To be locked in Phase 2 after checking sample outputs.
10. **Three sample evidence packs** listed above — profile + all events for the user (with pending/scheduled highlighted) + all options for the request + all user-scoped and request-scoped messages + all images. No interpretation yet.

## 14. Loose ends to resolve in Phase 2

- Confirm the FX date-matching rule ("last rate on or before settlement date") against a sample request whose events cross currencies.
- Confirm `scheduled` credits count as future income and `pending` credits are excluded (per spec) — verify no ambiguity in ledger.
- Decide the recurrence-inference threshold (min occurrences + cadence rounding) that yields the tightest 90-day forecast without over-fitting.
- Decide how to handle events with `settlement_date` blank (10 non-cash rows) — likely ignore for cash flow entirely.
- Confirm behavior when a `scheduled→failed` link chain leaves only the *scheduled* future row valid (7 debt_payment cases).
