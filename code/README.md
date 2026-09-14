# Buy or Wait? — solution

## Run

```bash
python3 code/main.py
```

Writes `output.csv` at the repo root: one row per request in
`dataset/requests.csv`, columns per DESIGN.md §3.

## Validate

```bash
python3 code/validate_output.py
```

Structural check: columns, row count, enums, bounds, and DESIGN.md §3
state-machine invariants (`affordable_now`, `wait`, `not_recommended`,
`partial_payment` shapes, ≤3 spending changes with unique targets).

## Score against solved samples

```bash
python3 code/evaluation/score_samples.py
```

Recomputes decisions for the 25 rows in `dataset/sample_requests.csv` using
the same engine and prints per-row diffs against the published outputs.
Samples are never used as a lookup table.

## Phase status

Phase 3 baseline: pre-change 90-day cash engine, emits `affordable_now /
full_payment`, `affordable_later / wait`, `not_affordable / not_recommended`.
Ranking, installments, partial-payment, spending-change search, VLM/LLM
evidence patching come in later phases.

Design docs: [DATA_NOTES.md](DATA_NOTES.md), [DESIGN.md](DESIGN.md).
Runtime log per AGENTS.md: `log.txt` at the repo root.
