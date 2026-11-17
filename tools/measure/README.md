# Project measurement

Turns the Hermes Kanban boards into numbers you can actually measure
projects with: throughput, cycle time, acceptance-criteria coverage and
failure rates.

```bash
python3 tools/measure/projects_report.py                  # every board
python3 tools/measure/projects_report.py --board projects # one board
python3 tools/measure/projects_report.py --json           # machine-readable
```

Read-only. Opens each board's SQLite file with `file:...?mode=ro`, so it
never writes to a board and never blocks the live dispatcher.

## What it measures

| Metric | Source | Why it matters |
|---|---|---|
| Cards by status | `tasks.status` | Where the work actually sits |
| Cycle time | `started_at` -> `completed_at` on done cards | Median is the honest number; mean gets dragged by outliers |
| Acceptance criteria | `task_runs.metadata.acceptance_criteria` | The quality signal — how many criteria are actually satisfied |
| Run outcomes | `task_runs.outcome` | Success / failure / retry split |
| Throughput | `completed_at` bucketed by ISO week | Trend over time |
| Open card age | `tasks.created_at` | Stale work nobody picked up |

## Acceptance criteria are the real signal

Card counts tell you how much work moved. They do not tell you whether the
work was any good. Workers write structured evidence into the run metadata:

```json
{
  "acceptance_criteria": {
    "ac1_artifact_clean": {
      "status": "VERIFIED",
      "evidence": "CronJob manual-sec-3 logged: 'redacted url-watches.json keys=[api_access_token] removed=2'"
    },
    "ac2_restored_app_works": "Scratch namespace changedetection-sec-restore: app came up and logged WATCHING"
  }
}
```

Three value shapes are accepted, because workers use all three:

- structured — `{"status": "VERIFIED", "evidence": "..."}`
- verdict first — `"PASS - kubectl kustomize apps/staging exits 0"`
- label then verdict — `"CronJob exists with concurrencyPolicy Forbid: PASS (live: 23 4 * * *)"`

`PASS`, `VERIFIED`, `DONE`, `OK`, `COMPLETE`, `TRUE` and `PASSED` count as
satisfied. Anything else — `FAIL`, `PARTIAL`, `UNVERIFIED`, `OPEN`, or text
with no recognizable verdict — does not. Text with no verdict is reported as
`UNKNOWN` and counted separately, so a board where workers wrote prose never
silently looks green.

## Retries are not failures

`rate_limited` is a retry. The dispatcher requeues those without charging
the card's failure budget, so counting them as failures would report a
broken pipeline when the real story is "the provider quota ran out for a
while". They are reported separately as `retry`.

## Caveats

- **Historical failures do not age out.** The `systems` board still shows an
  88.9% failure rate from eight crashes on 2026-11-15, all from the same
  cause: the `default` profile pointed at a model the provider did not
  offer. That config was fixed afterwards and the board has had no failures
  since. The report shows when each failure happened — read the timestamps
  before concluding a board is currently broken.
- **Cycle time excludes cards that never started.** A card that was created
  and never claimed has no `started_at`, so it contributes to neither the
  median nor the mean. Those cards show up under open-card age instead.
- **Acceptance criteria only appear if a worker wrote them.** A board with
  zero criteria is not a board with zero problems; it is a board where
  nobody recorded evidence.
