#!/usr/bin/env python3
"""Measure project progress across Hermes Kanban boards.

Reads the Kanban SQLite databases directly and reports, per board:

  * card counts by status
  * cycle time for completed work (started -> completed)
  * acceptance-criteria verdicts harvested from run metadata
  * per-assignee throughput and failure counts
  * a simple throughput trend over time

Why read the DB directly instead of shelling out to ``hermes kanban``:
the CLI gives per-command snapshots, not cross-card aggregates. This tool
needs every card, every run and every event in one pass, and it must work
for boards other than the active one.

Read-only. It opens the database with ``file:...?mode=ro`` so a concurrent
dispatcher can never be blocked by this report, and it can never write to
a board by accident.

Usage:
    python3 tools/measure/projects_report.py
    python3 tools/measure/projects_report.py --board deploy-pipeline
    python3 tools/measure/projects_report.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
BOARD_ROOT = HERMES_HOME / "kanban" / "boards"

STATUS_ORDER = ["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done"]

# Outcomes that mean "the worker ran and finished", versus "the worker
# tried and did not". The distinction matters for the failure-rate number:
# a rate-limited attempt is not the same as a card that was completed and
# then had its work rejected.
SUCCESS_OUTCOMES = {"completed", "done", "success"}

# A rate-limited attempt is a retry, not a failure. The dispatcher requeues
# these without counting them against the card's failure budget, so counting
# them as failures would report a broken pipeline when the only real story
# is "the provider quota ran out for a while".
RETRY_OUTCOMES = {"rate_limited", "scheduled", "reclaimed"}

FAILURE_OUTCOMES = {
    "spawn_failed",
    "timed_out",
    "crashed",
    "error",
    "failed",
    "max_retries",
    "blocked",
}

# Verdicts that count as "this criterion is actually satisfied".
SATISFIED_VERDICTS = {"VERIFIED", "DONE", "PASS", "PASSED", "OK", "TRUE", "COMPLETE"}

# Every verdict keyword a worker may write, satisfied or not.
VERDICT_KEYWORDS = (
    "VERIFIED", "PASSED", "COMPLETE", "DONE", "PASS", "OK", "TRUE",
    "FAIL", "FAILED", "FAILURE", "PARTIAL", "UNVERIFIED", "BLOCKED",
    "SKIPPED", "OPEN", "MISSING",
)

# A verdict keyword standing alone, e.g. "PASS", "FAIL", "PARTIAL".
_VERDICT_WORD_RE = re.compile(
    r"\b(" + "|".join(VERDICT_KEYWORDS) + r")\b", re.IGNORECASE)

# A verdict keyword immediately after a label and a separator, e.g.
# "CronJob exists with schedule, concurrencyPolicy Forbid: PASS (live: ...)".
# This form is preferred when present because the label itself can contain
# words like "complete" that would otherwise be misread as the verdict.
_VERDICT_AFTER_LABEL_RE = re.compile(
    r"^[^:]{1,120}:\s*(" + "|".join(VERDICT_KEYWORDS) + r")\b(.*)$",
    re.IGNORECASE | re.DOTALL)

# A verdict keyword at the very start, e.g. "PASS - kubectl kustomize exits 0".
_VERDICT_LEADING_RE = re.compile(
    r"^\s*(" + "|".join(VERDICT_KEYWORDS) + r")\b\s*[-—:]*\s*(.*)$",
    re.IGNORECASE | re.DOTALL)


def _normalize_verdict(raw):
    """Return (status, evidence) for one acceptance-criterion value.

    Three shapes exist in the wild:
      * ``{"status": "VERIFIED", "evidence": "..."}``  -- structured
      * ``"PASS - kubectl kustomize exits 0"``          -- verdict first
      * ``"CronJob exists: PASS (live: 23 4 * * *)"``   -- label, then verdict

    All three must count, otherwise a board where workers wrote prose
    verdicts looks entirely unsatisfied. Text with no recognizable verdict
    is reported as UNKNOWN rather than being silently counted as satisfied.
    """
    if isinstance(raw, dict):
        status = str(raw.get("status", "UNKNOWN")).strip().upper()
        evidence = str(raw.get("evidence", "") or "")
        # A structured status can still be prose, e.g. "PASS - it worked".
        if status not in VERDICT_KEYWORDS:
            return status, evidence
        m = _VERDICT_LEADING_RE.match(status + " " + evidence)
        if m and m.group(1).upper() != status:
            return m.group(1).upper(), m.group(2).strip()
        return status, evidence
    if isinstance(raw, bool):
        return ("PASS" if raw else "FAIL"), ""
    if isinstance(raw, (int, float)):
        return "PASS" if raw else "FAIL", ""
    text = str(raw or "").strip()
    if not text:
        return "UNKNOWN", ""

    m = _VERDICT_AFTER_LABEL_RE.match(text)
    if m:
        return m.group(1).upper(), m.group(2).strip()

    m = _VERDICT_LEADING_RE.match(text)
    if m:
        return m.group(1).upper(), m.group(2).strip()

    # Last resort: the first verdict word anywhere. Reported as UNKNOWN
    # unless it is a real verdict keyword, so prose that merely mentions
    # "failure" in passing is not scored.
    m = _VERDICT_WORD_RE.search(text)
    if m:
        return m.group(1).upper(), text
    return "UNKNOWN", text


def _epoch(value):
    """Coerce a stored timestamp to an int epoch, or None.

    Timestamps in this schema are unix epoch seconds, but older rows and
    hand-edited rows can hold a string or NULL. Every caller treats None as
    "unknown" and excludes the row from averages rather than treating it
    as zero.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fmt_duration(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def _fmt_when(epoch):
    if epoch is None:
        return "-"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def connect_ro(board_dir: Path) -> sqlite3.Connection:
    """Open a board database read-only.

    The ``mode=ro`` URI flag is the important part: WAL mode means a normal
    connection would want to write the -shm/-wal files, which can block a
    live dispatcher. Read-only mode sidesteps that entirely.
    """
    db = board_dir / "kanban.db"
    if not db.exists():
        raise FileNotFoundError(db)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def board_dirs():
    """Every board directory that has a database, plus the legacy root board."""
    if not BOARD_ROOT.exists():
        return []
    found = []
    for child in sorted(BOARD_ROOT.iterdir()):
        if child.is_dir() and (child / "kanban.db").exists():
            found.append(child)
    legacy = HERMES_HOME / "kanban.db"
    if legacy.exists():
        found.append(HERMES_HOME)
    return found


def board_meta(board_dir: Path) -> dict:
    meta_file = board_dir / "board.json"
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return {"slug": board_dir.name, "name": board_dir.name}


def collect(conn: sqlite3.Connection) -> dict:
    """Pull cards, runs and events for one board."""
    tasks = [dict(r) for r in conn.execute("SELECT * FROM tasks")]

    runs = []
    for r in conn.execute("SELECT * FROM task_runs"):
        d = dict(r)
        meta = {}
        if d.get("metadata"):
            try:
                meta = json.loads(d["metadata"])
            except (TypeError, json.JSONDecodeError):
                meta = {}
        d["meta"] = meta
        runs.append(d)

    events = [dict(r) for r in conn.execute("SELECT * FROM task_events")]

    return {"tasks": tasks, "runs": runs, "events": events}


def acceptance_criteria(runs):
    """Harvest acceptance-criteria verdicts from run metadata.

    Workers write a ``acceptance_criteria`` dict keyed by criterion id. The
    value is either a structured ``{"status": ..., "evidence": ...}`` or a
    prose string starting with the verdict. Both are normalized to a
    (status, evidence) pair so a board written in either style measures the
    same way.
    """
    verdicts = []
    for run in runs:
        ac = run["meta"].get("acceptance_criteria")
        if not isinstance(ac, dict):
            continue
        for cid, detail in ac.items():
            status, evidence = _normalize_verdict(detail)
            verdicts.append({
                "criterion": cid,
                "status": status,
                "evidence": evidence[:300],
                "task_id": run.get("task_id"),
            })
    return verdicts


def measure(board_dir: Path) -> dict:
    conn = connect_ro(board_dir)
    try:
        data = collect(conn)
    finally:
        conn.close()

    tasks = data["tasks"]
    runs = data["runs"]
    meta = board_meta(board_dir)

    by_status = Counter(t["status"] for t in tasks)
    by_assignee = defaultdict(Counter)
    for t in tasks:
        by_assignee[t.get("assignee") or "(unassigned)"][t["status"]] += 1

    # Cycle time for cards that actually finished.
    cycle = []
    for t in tasks:
        if t["status"] != "done":
            continue
        start, end = _epoch(t.get("started_at")), _epoch(t.get("completed_at"))
        if start and end and end >= start:
            cycle.append(end - start)

    # Run outcomes, deduplicated: a card can have several attempts and only
    # the last one counts as its outcome.
    final_outcome = {}
    for run in sorted(runs, key=lambda r: (_epoch(r.get("started_at")) or 0)):
        if run.get("outcome"):
            final_outcome[run.get("task_id")] = run["outcome"]

    outcomes = Counter(final_outcome.values())
    successes = sum(n for o, n in outcomes.items() if o in SUCCESS_OUTCOMES)
    failures = sum(n for o, n in outcomes.items() if o in FAILURE_OUTCOMES)
    retries = sum(n for o, n in outcomes.items() if o in RETRY_OUTCOMES)

    # Token estimates recorded by workers, where present.
    est_tokens = []
    for run in runs:
        m = run["meta"]
        for key in ("est_tokens", "estimated_tokens", "tokens"):
            val = m.get(key)
            if isinstance(val, (int, float)):
                est_tokens.append(int(val))
                break

    verdicts = acceptance_criteria(runs)
    verdict_counts = Counter(v["status"] for v in verdicts)
    satisfied = sum(n for s, n in verdict_counts.items() if s in SATISFIED_VERDICTS)
    unknown = verdict_counts.get("UNKNOWN", 0)

    # Throughput: cards completed per calendar week.
    weekly = Counter()
    for t in tasks:
        end = _epoch(t.get("completed_at"))
        if t["status"] == "done" and end:
            dt = datetime.fromtimestamp(end, tz=timezone.utc)
            iso = dt.isocalendar()
            weekly[f"{iso[0]}-W{iso[1]:02d}"] += 1

    # Age of anything still open, oldest first.
    now = int(datetime.now(tz=timezone.utc).timestamp())
    open_cards = []
    for t in tasks:
        if t["status"] in ("done", "archived"):
            continue
        created = _epoch(t.get("created_at"))
        open_cards.append({
            "id": t["id"],
            "title": t["title"],
            "status": t["status"],
            "assignee": t.get("assignee") or "(unassigned)",
            "priority": t.get("priority"),
            "age_s": (now - created) if created else None,
            "created_at": _fmt_when(created),
        })
    open_cards.sort(key=lambda c: -(c["age_s"] or 0))

    # Failure detail, so a failure rate is actionable rather than a number.
    # Only real failures are listed here; retries live in their own bucket.
    failures_detail = []
    for run in runs:
        if run.get("outcome") in FAILURE_OUTCOMES:
            failures_detail.append({
                "task_id": run.get("task_id"),
                "profile": run.get("profile"),
                "outcome": run.get("outcome"),
                "error": str(run.get("error") or "")[:200],
                "when": _fmt_when(_epoch(run.get("started_at"))),
            })

    return {
        "slug": meta.get("slug", board_dir.name),
        "name": meta.get("name", board_dir.name),
        "description": meta.get("description", ""),
        "total": len(tasks),
        "by_status": {s: by_status.get(s, 0) for s in STATUS_ORDER},
        "by_assignee": {a: dict(c) for a, c in sorted(by_assignee.items())},
        "cycle_time": {
            "count": len(cycle),
            "median_s": int(statistics.median(cycle)) if cycle else None,
            "mean_s": int(statistics.mean(cycle)) if cycle else None,
            "min_s": min(cycle) if cycle else None,
            "max_s": max(cycle) if cycle else None,
            "median_h": _fmt_duration(statistics.median(cycle)) if cycle else None,
        },
        "outcomes": dict(outcomes),
        "runs_total": len(runs),
        "successes": successes,
        "failures": failures,
        "retries": retries,
        "failure_rate": round(failures / (successes + failures), 3) if (successes + failures) else None,
        "failure_detail": failures_detail[-10:],
        "acceptance": {
            "total": len(verdicts),
            "satisfied": satisfied,
            "unknown": unknown,
            "counts": dict(verdict_counts),
            "verdicts": verdicts,
        },
        "est_tokens": {
            "count": len(est_tokens),
            "total": sum(est_tokens),
            "median": int(statistics.median(est_tokens)) if est_tokens else None,
        },
        "weekly_completed": dict(sorted(weekly.items())),
        "open_cards": open_cards,
    }


def print_report(report: dict) -> None:
    name = report["name"]
    slug = report["slug"]
    print("=" * 72)
    print(f"{name}  ({slug})")
    if report["description"]:
        print(f"  {report['description']}")
    print("=" * 72)

    total = report["total"]
    print(f"\nCards: {total}")
    for status, n in report["by_status"].items():
        if n:
            bar = "#" * min(n, 40)
            print(f"  {status:<10} {n:>3}  {bar}")

    if report["by_assignee"]:
        print("\nBy assignee:")
        for assignee, counts in report["by_assignee"].items():
            summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
            print(f"  {assignee:<14} {summary}")

    ct = report["cycle_time"]
    print(f"\nCycle time (done cards): {ct['count']} measured")
    if ct["count"]:
        print(f"  median {ct['median_h']}   mean {_fmt_duration(ct['mean_s'])}   "
              f"range {_fmt_duration(ct['min_s'])}..{_fmt_duration(ct['max_s'])}")

    print(f"\nRuns: {report['runs_total']}   success {report['successes']}   "
          f"fail {report['failures']}   retry {report['retries']}")
    if report["failure_rate"] is not None:
        print(f"  failure rate: {report['failure_rate'] * 100:.1f}% "
              f"(retries excluded — a rate-limited attempt is requeued, not failed)")
    if report["outcomes"]:
        for outcome, n in sorted(report["outcomes"].items(), key=lambda kv: -kv[1]):
            print(f"    {outcome:<16} {n}")

    ac = report["acceptance"]
    print(f"\nAcceptance criteria: {ac['satisfied']}/{ac['total']} satisfied"
          + (f"   ({ac['unknown']} unparseable)" if ac["unknown"] else ""))
    if ac["counts"]:
        for status, n in sorted(ac["counts"].items(), key=lambda kv: -kv[1]):
            print(f"    {status:<12} {n}")
    unmet = [v for v in ac["verdicts"] if v["status"] not in SATISFIED_VERDICTS]
    if unmet:
        print("  Not satisfied:")
        for v in unmet[:10]:
            print(f"    [{v['status']}] {v['task_id']} {v['criterion']}")
            if v["evidence"]:
                print(f"        {v['evidence'][:150]}")

    if report["failure_detail"]:
        print("\n  Failures:")
        for f in report["failure_detail"]:
            print(f"    {f['when']}  {f['task_id']}  {f['profile']:<10} {f['outcome']:<14} {f['error'][:90]}")

    et = report["est_tokens"]
    if et["count"]:
        print(f"\nToken estimates: {et['count']} recorded, total {et['total']:,}, median {et['median']:,}")

    if report["weekly_completed"]:
        print("\nCompleted per week:")
        for week, n in sorted(report["weekly_completed"].items())[-8:]:
            print(f"    {week}  {n}")

    if report["open_cards"]:
        print("\nOpen cards (oldest first):")
        for c in report["open_cards"][:12]:
            print(f"    {c['id']}  {c['status']:<10} {c['assignee']:<12} "
                  f"age {_fmt_duration(c['age_s']):>8}  {c['title'][:60]}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", help="measure only this board slug")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a text report")
    ap.add_argument("--include-closed", action="store_true",
                    help="include archived boards")
    args = ap.parse_args()

    dirs = board_dirs()
    if args.board:
        dirs = [d for d in dirs if board_meta(d).get("slug") == args.board or d.name == args.board]
        if not dirs:
            print(f"no such board: {args.board}", file=sys.stderr)
            return 1

    reports = []
    for d in dirs:
        try:
            reports.append(measure(d))
        except (sqlite3.Error, FileNotFoundError) as exc:
            print(f"skip {d}: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps(reports, indent=2, default=str))
        return 0

    for report in reports:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
