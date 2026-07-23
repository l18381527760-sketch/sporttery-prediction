# Data Evidence Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make historical fixture identity, verified 90-minute results, live odds phases, and evidence health complete enough to support settlement and future model learning without changing the current betting model.

**Architecture:** Add small read-only contract modules around the existing immutable import manifests and result rows, then route current consumers through them. Extend the existing live-odds snapshots with explicit capture phases and aggregate both legacy and live directories into a single evidence-health report. Keep all current strategy, ledger, revalidation, site, and Apps Script behavior fail-closed.

**Tech Stack:** Python 3.12+, standard library dataclasses/CSV/JSON/hashlib, existing unittest suite, GitHub Actions YAML, existing Apps Script unchanged.

## Global Constraints

- Business timezone is exactly `Asia/Shanghai` / UTC+08:00.
- Simulation only; `real_money_automation` remains `false`.
- Existing `value-v4` remains in `shadow` activation mode.
- Historical ledgers, locked plans, settlements, receipts, and import extracts are immutable.
- Only a uniquely mapped, source-identified, regular-time 90-minute result is trainable or settleable.
- Identity or provenance ambiguity must return an explicit ineligible state; it must never guess.
- Existing Apps Script remains the only email sender.
- Normal daily simulated stake remains 0-200 yuan; this project must not activate or resize bets.
- Use UTF-8 for new text files and preserve existing public schemas unless a schema version is incremented.

---

## File Map

**New modules**

- `fixture_identity.py`: reads historical fixture identities from a verified immutable import manifest, with a compatibility fallback limited to a matching-date `fixtures.csv`.
- `result_evidence.py`: provides the single canonical predicate and normalized read model for a proven regular-time result.
- `evidence_health.py`: computes identity, result, and live-snapshot coverage plus blocking violations.

**Modified modules**

- `update_sporttery_results.py`: resolves fallback results against the target date's immutable fixture identity and supports bounded historical reconciliation.
- `build_historical_features.py`: consumes only canonical proven results through `result_evidence.py`.
- `draw_model_learning.py`: consumes the same result predicate when constructing training samples.
- `betting_ledger.py`: delegates result provenance eligibility to the shared predicate without changing settlement economics.
- `live_odds.py`: records explicit capture phase and per-match minutes-to-kickoff.
- `capture_odds_snapshot.py`: forwards `--phase` to live capture.
- `model_metrics.py`: reports phase coverage across immutable live and legacy snapshots.
- `report_status.py`: embeds an evidence-health summary and blocks readiness on hard evidence violations.
- `.github/workflows/noon-settlement.yml`: runs bounded reconciliation and evidence health before settlement/learning.
- `.github/workflows/odds-snapshot.yml`: labels live decision/monitoring captures explicitly.

**New tests**

- `tests/test_fixture_identity.py`
- `tests/test_result_evidence.py`
- `tests/test_evidence_health.py`
- `tests/test_build_historical_features.py`
- `tests/test_evidence_pipeline_replay.py`

**Extended tests**

- `tests/test_update_sporttery_results.py`
- `tests/test_build_historical_features.py`
- `tests/test_draw_model_learning.py`
- `tests/test_betting_ledger.py`
- `tests/test_live_odds.py`
- `tests/test_capture_odds_snapshot.py`
- `tests/test_model_metrics.py`
- `tests/test_report_status.py`
- `tests/test_workflow_schedule.py`

---

### Task 1: Historical Fixture Identity Reader

**Files:**
- Create: `fixture_identity.py`
- Create: `tests/test_fixture_identity.py`

**Interfaces:**
- Consumes: `import_sporttery.read_valid_import_manifest(root: Path, target_date: date) -> dict`
- Produces: `fixture_match_ids(root: Path, target_date: date) -> dict[tuple[str, str, str], frozenset[str]]`
- Produces: `fixture_identity_rate(root: Path, target_date: date) -> tuple[int, int]`

- [ ] **Step 1: Write failing tests for historical and compatibility identity reads**

```python
import csv
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fixture_identity import fixture_identity_rate, fixture_match_ids


DAY = date(2026, 7, 21)


class FixtureIdentityTest(unittest.TestCase):
    def write_manifest(self, root: Path, fixture_rows: str) -> None:
        data = root / "data"
        extracts = data / "import_extracts" / DAY.isoformat()
        manifests = data / "import_manifests"
        extracts.mkdir(parents=True)
        manifests.mkdir(parents=True)
        fixture = extracts / "fixtures.csv"
        fixture.write_text(
            "date,team_a,team_b,match_id,kickoff_at\n" + fixture_rows,
            encoding="utf-8",
        )
        odds = extracts / "odds.json"
        odds.write_text("{}", encoding="utf-8")
        ratings = extracts / "ratings.csv"
        ratings.write_text("team,elo\n甲队,1500\n乙队,1500\n", encoding="utf-8")
        records = {}
        for name, path in (("fixtures", fixture), ("odds", odds), ("ratings", ratings)):
            payload = path.read_bytes()
            records[name] = {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
        manifest = {
            "schema_version": 2,
            "target_date": DAY.isoformat(),
            "source": "sporttery",
            "imported_at_bjt": "2026-07-21T12:05:00+08:00",
            **records,
        }
        (manifests / "2026-07-21.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )

    def test_reads_target_day_from_immutable_manifest_when_current_csv_is_newer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            self.write_manifest(
                root,
                "2026-07-21,甲队,乙队,2040580,2026-07-21T18:00:00+08:00\n",
            )
            (data / "fixtures.csv").write_text(
                "date,team_a,team_b,match_id\n2026-07-22,丙队,丁队,999\n",
                encoding="utf-8",
            )

            identities = fixture_match_ids(root, DAY)
            self.assertEqual(
                frozenset({"2040580"}),
                identities[("2026-07-21", "甲队", "乙队")],
            )
            self.assertEqual((1, 1), fixture_identity_rate(root, DAY))

    def test_rejects_duplicate_provider_ids_for_different_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_manifest(
                root,
                "2026-07-21,甲队,乙队,2040580,2026-07-21T18:00:00+08:00\n"
                "2026-07-21,丙队,丁队,2040580,2026-07-21T20:00:00+08:00\n",
            )
            with self.assertRaisesRegex(ValueError, "fixture match_id is duplicated"):
                fixture_match_ids(root, DAY)
```

- [ ] **Step 2: Run the focused tests and verify the missing module failure**

Run: `python -m unittest tests.test_fixture_identity -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'fixture_identity'`.

- [ ] **Step 3: Implement the manifest-backed reader**

```python
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from import_sporttery import import_manifest_path, read_valid_import_manifest


FixtureKey = tuple[str, str, str]


def fixture_match_ids(
    root: Path, target_date: date
) -> dict[FixtureKey, frozenset[str]]:
    root = root.resolve()
    manifest_path = import_manifest_path(root, target_date)
    if manifest_path.exists():
        manifest = read_valid_import_manifest(root, target_date)
        path = root / manifest["fixtures"]["path"]
    else:
        path = root / "data" / "fixtures.csv"
    rows = _rows(path)
    target = target_date.isoformat()
    identities: dict[FixtureKey, set[str]] = {}
    owner: dict[str, FixtureKey] = {}
    for row in rows:
        if row.get("date") != target:
            continue
        home = str(row.get("team_a") or "").strip()
        away = str(row.get("team_b") or "").strip()
        match_id = str(row.get("match_id") or "").strip()
        if not home or not away or not match_id:
            raise ValueError("fixture identity is incomplete")
        key = (target, home, away)
        if match_id in owner and owner[match_id] != key:
            raise ValueError("fixture match_id is duplicated")
        owner[match_id] = key
        identities.setdefault(key, set()).add(match_id)
    return {key: frozenset(values) for key, values in identities.items()}


def fixture_identity_rate(root: Path, target_date: date) -> tuple[int, int]:
    identities = fixture_match_ids(root, target_date)
    return sum(len(ids) == 1 for ids in identities.values()), len(identities)


def _rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError("fixture identity source is invalid") from exc
```

- [ ] **Step 4: Run tests and repository import-manifest regressions**

Run: `python -m unittest tests.test_fixture_identity tests.test_import_sporttery -v`

Expected: PASS.

- [ ] **Step 5: Commit the identity contract**

```bash
git add fixture_identity.py tests/test_fixture_identity.py
git commit -m "feat: read historical fixture identities"
```

---

### Task 2: Resolve Results Against Historical Identity

**Files:**
- Modify: `update_sporttery_results.py:143-284`
- Modify: `update_sporttery_results.py:351-366`
- Modify: `tests/test_update_sporttery_results.py`

**Interfaces:**
- Consumes: `fixture_identity.fixture_match_ids(root, target_date)`
- Preserves: `update_results(target_date: date) -> Path`
- Produces: fallback rows with `match_id`, `result_status=finished`, provider provenance, and 90-minute scope only when one canonical ID exists

- [ ] **Step 1: Add a failing regression for yesterday's manifest after today's fixture overwrite**

```python
def test_fallback_uses_historical_manifest_after_current_fixture_overwrite(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        data = root / "data"
        data.mkdir()
        (data / "fixtures.csv").write_text(
            "date,team_a,team_b,match_id\n2026-07-22,New A,New B,9999\n",
            encoding="utf-8",
        )
        historical = {
            ("2026-07-21", "Team A", "Team B"): frozenset({"2040580"})
        }
        fallback = [{
            "homeTeam": "Team A", "awayTeam": "Team B",
            "score": "1:1", "source_record_id": "tr-88",
        }]
        with (
            patch.object(results, "ROOT", root),
            patch.object(results, "DATA_DIR", data),
            patch.object(results, "official_result_rows", side_effect=RuntimeError("offline")),
            patch.object(results, "fetch_zgzcw_results", return_value=fallback),
            patch.object(results, "fixture_match_ids", return_value=historical),
        ):
            path = results.update_results(date(2026, 7, 21))

        row = self.read_rows(path)[0]
        self.assertEqual("2040580", row["match_id"])
        self.assertEqual("finished", row["result_status"])
        self.assertEqual("regular_time_90", row["score_scope"])
        self.assertEqual("tr-88", row["source_record_id"])
```

- [ ] **Step 2: Run the regression and verify it fails before the import is routed**

Run: `python -m unittest tests.test_update_sporttery_results.ResultProvenanceTest.test_fallback_uses_historical_manifest_after_current_fixture_overwrite -v`

Expected: FAIL because `update_sporttery_results` does not expose or call `fixture_match_ids`.

- [ ] **Step 3: Route target-date identity through the new contract**

```python
from fixture_identity import fixture_match_ids


def _fixture_match_ids(target_date: date) -> dict[tuple[str, str, str], set[str]]:
    return {
        key: set(values)
        for key, values in fixture_match_ids(ROOT, target_date).items()
    }
```

Keep `_resolve_fallback_target` fail-closed: source record ID remains mandatory, more than one canonical ID remains `unavailable`, and conflicting scores remain `conflict`.

- [ ] **Step 4: Run all result provenance tests**

Run: `python -m unittest tests.test_update_sporttery_results -v`

Expected: PASS, including byte-idempotency and ambiguity tests.

- [ ] **Step 5: Commit the resolver change**

```bash
git add update_sporttery_results.py tests/test_update_sporttery_results.py
git commit -m "fix: resolve results from historical manifests"
```

---

### Task 3: Shared Proven 90-Minute Result Contract

**Files:**
- Create: `result_evidence.py`
- Create: `tests/test_result_evidence.py`
- Modify: `build_historical_features.py`
- Create: `tests/test_build_historical_features.py`
- Modify: `draw_model_learning.py:206-269`
- Modify: `betting_ledger.py:2541-2559`
- Modify: corresponding existing tests

**Interfaces:**
- Produces: `proven_90_minute_result(row: dict) -> bool`
- Produces: `proven_result_provenance(row: dict) -> bool`
- Produces: `normalized_result(row: dict) -> dict | None`
- Consumers receive `{match_id, home_goals, away_goals, result_source, source_record_id, captured_at_bjt}` or `None`

- [ ] **Step 1: Write the result contract matrix**

```python
import unittest

from result_evidence import (
    normalized_result,
    proven_90_minute_result,
    proven_result_provenance,
)


class ResultEvidenceTest(unittest.TestCase):
    def base(self):
        return {
            "match_id": "2040580",
            "home_goals": "1",
            "away_goals": "1",
            "result_status": "finished",
            "result_source": "zgzcw",
            "source_record_id": "88",
            "captured_at_bjt": "2026-07-22T12:30:00+08:00",
            "score_scope": "regular_time_90",
            "settlement_minutes": "90",
        }

    def test_accepts_complete_regular_time_result(self):
        row = self.base()
        self.assertTrue(proven_result_provenance(row))
        self.assertTrue(proven_90_minute_result(row))
        self.assertEqual(1, normalized_result(row)["home_goals"])

    def test_rejects_every_missing_or_ambiguous_proof(self):
        for field, value in (
            ("match_id", ""), ("result_status", "unavailable"),
            ("result_source", ""), ("source_record_id", ""),
            ("captured_at_bjt", ""), ("score_scope", "including_extra_time"),
            ("settlement_minutes", "120"), ("home_goals", "x"),
        ):
            with self.subTest(field=field):
                row = self.base()
                row[field] = value
                self.assertFalse(proven_90_minute_result(row))
                self.assertIsNone(normalized_result(row))

    def test_rejects_unapproved_result_source(self):
        row = self.base()
        row["result_source"] = "unknown"
        self.assertFalse(proven_result_provenance(row))
        self.assertFalse(proven_90_minute_result(row))
```

- [ ] **Step 2: Run the contract tests and verify the missing module failure**

Run: `python -m unittest tests.test_result_evidence -v`

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement one canonical predicate**

```python
from __future__ import annotations

from datetime import datetime


ALLOWED_RESULT_SOURCES = frozenset({"sporttery", "zgzcw"})


def proven_result_provenance(row: dict) -> bool:
    try:
        source = _text(row.get("result_source")).lower()
        _text(row.get("source_record_id"))
        captured = datetime.fromisoformat(_text(row.get("captured_at_bjt")))
    except (TypeError, ValueError):
        return False
    return (
        source in ALLOWED_RESULT_SOURCES
        and captured.tzinfo is not None
        and captured.utcoffset() is not None
    )


def normalized_result(row: dict) -> dict | None:
    try:
        if not proven_result_provenance(row):
            return None
        if row.get("result_status") != "finished":
            return None
        if row.get("score_scope") != "regular_time_90":
            return None
        if str(row.get("settlement_minutes") or "") != "90":
            return None
        match_id = _text(row.get("match_id"))
        source = _text(row.get("result_source")).lower()
        record_id = _text(row.get("source_record_id"))
        captured = _text(row.get("captured_at_bjt"))
        parsed = datetime.fromisoformat(captured)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        home = int(str(row.get("home_goals")))
        away = int(str(row.get("away_goals")))
        if home < 0 or away < 0:
            return None
    except (TypeError, ValueError):
        return None
    return {
        "match_id": match_id,
        "home_goals": home,
        "away_goals": away,
        "result_source": source,
        "source_record_id": record_id,
        "captured_at_bjt": parsed.isoformat(),
    }


def proven_90_minute_result(row: dict) -> bool:
    return normalized_result(row) is not None


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("required result text is missing")
    return value.strip()
```

Change `build_historical_features.load_results()` to filter through the shared predicate:

```python
from result_evidence import proven_90_minute_result


def load_results() -> list[dict]:
    path = DATA_DIR / "bet_results.csv"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if proven_90_minute_result(row)]
```

Change draw training to index only proven results by canonical match ID:

```python
from result_evidence import normalized_result


result_by_match = {}
for source_row in _read_csv(root / "data" / "bet_results.csv"):
    result = normalized_result(source_row)
    if result is not None:
        result_by_match[result["match_id"]] = result

# Inside the snapshot loop:
result = result_by_match.get(key[3])
if result is None:
    continue
home_goals = result["home_goals"]
away_goals = result["away_goals"]
```

Preserve ledger refund handling while sharing provenance and finished-result checks:

```python
from result_evidence import proven_90_minute_result, proven_result_provenance


def _is_proven_result(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("result_status") == "refunded":
        return proven_result_provenance(result)
    return proven_90_minute_result(result)
```

Legacy display rows remain readable but cannot feed history features, draw training, or canonical finished-result settlement.

- [ ] **Step 4: Run focused consumers and ledger settlement tests**

Run: `python -m unittest tests.test_result_evidence tests.test_build_historical_features tests.test_draw_model_learning tests.test_betting_ledger -v`

Expected: PASS. Existing unproven legacy results remain visible but ineligible.

- [ ] **Step 5: Commit the shared result contract**

```bash
git add result_evidence.py tests/test_result_evidence.py build_historical_features.py draw_model_learning.py betting_ledger.py tests/test_build_historical_features.py tests/test_draw_model_learning.py tests/test_betting_ledger.py
git commit -m "refactor: share proven result eligibility"
```

---

### Task 4: Bounded Historical Result Reconciliation

**Files:**
- Modify: `update_sporttery_results.py:403-420`
- Modify: `tests/test_update_sporttery_results.py`
- Modify: `.github/workflows/noon-settlement.yml`
- Modify: `tests/test_workflow_schedule.py`

**Interfaces:**
- Produces CLI: `python update_sporttery_results.py --date YYYY-MM-DD --reconcile-days N`
- `N` is an integer from 1 through 30; dates run oldest-first through `--date`
- A failed date returns exit code 1 and prevents settlement/learning

- [ ] **Step 1: Write failing CLI and workflow contract tests**

```python
def test_reconcile_days_runs_oldest_first_and_is_bounded(self):
    with patch.object(results, "update_results", return_value=Path("results.csv")) as update:
        exit_code = results.main([
            "--date", "2026-07-21", "--reconcile-days", "3"
        ])
    self.assertEqual(0, exit_code)
    self.assertEqual(
        [date(2026, 7, 19), date(2026, 7, 20), date(2026, 7, 21)],
        [call.args[0] for call in update.call_args_list],
    )

def test_reconcile_days_rejects_more_than_thirty(self):
    with self.assertRaises(SystemExit):
        results.main(["--date", "2026-07-21", "--reconcile-days", "31"])
```

Add a workflow assertion that settlement runs:

```text
python update_sporttery_results.py --date "$SETTLEMENT_DATE" --reconcile-days 7
```

before any ledger settlement or model training command.

- [ ] **Step 2: Run tests and verify the unsupported argument failure**

Run: `python -m unittest tests.test_update_sporttery_results tests.test_workflow_schedule -v`

Expected: FAIL because `--reconcile-days` is not accepted.

- [ ] **Step 3: Add bounded oldest-first reconciliation**

```python
def _reconcile_count(value: str) -> int:
    count = int(value)
    if not 1 <= count <= 30:
        raise argparse.ArgumentTypeError("reconcile-days must be between 1 and 30")
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    parser.add_argument("--date", default=yesterday)
    parser.add_argument("--reconcile-days", type=_reconcile_count, default=1)
    args = parser.parse_args(argv)
    end = datetime.strptime(args.date, "%Y-%m-%d").date()
    for offset in reversed(range(args.reconcile_days)):
        update_results(end - timedelta(days=offset))
    return 0
```

Import `argparse` and `timedelta`. Update the workflow to reconcile seven days daily; this is bounded enough for public sources while repairing recent unavailable rows. Do not train if reconciliation exits nonzero.

- [ ] **Step 4: Run result, workflow, and settlement tests**

Run: `python -m unittest tests.test_update_sporttery_results tests.test_workflow_schedule tests.test_betting_ledger -v`

Expected: PASS.

- [ ] **Step 5: Commit reconciliation**

```bash
git add update_sporttery_results.py tests/test_update_sporttery_results.py .github/workflows/noon-settlement.yml tests/test_workflow_schedule.py
git commit -m "feat: reconcile recent canonical results"
```

---

### Task 5: Explicit Live Odds Capture Phases

**Files:**
- Modify: `live_odds.py:27-78`
- Modify: `live_odds.py:288-366`
- Modify: `capture_odds_snapshot.py:215-235`
- Modify: `tests/test_live_odds.py`
- Modify: `tests/test_capture_odds_snapshot.py`

**Interfaces:**
- Extends: `capture_live_snapshot(root, target_date, captured_at, phase="monitoring") -> Path`
- Live payload adds `capture_phase`
- Each match adds `capture_phase` and `minutes_to_kickoff`
- Allowed phases: `opening`, `decision`, `monitoring`, `pre_kickoff_90`, `pre_kickoff_30`

- [ ] **Step 1: Add failing live phase tests**

```python
def test_live_snapshot_records_requested_and_per_match_phase(self):
    path = live_odds.capture_live_snapshot(
        self.root, DAY, datetime(2026, 7, 21, 16, 45, tzinfo=BJT), phase="decision"
    )
    payload = live_odds.read_valid_live_snapshot(
        self.root, path, DAY, datetime(2026, 7, 21, 16, 46, tzinfo=BJT)
    )
    self.assertEqual("decision", payload["capture_phase"])
    self.assertEqual("pre_kickoff_90", payload["matches"][0]["capture_phase"])
    self.assertEqual(75, payload["matches"][0]["minutes_to_kickoff"])

def test_cli_forwards_phase_to_live_capture(self):
    with patch.object(capture_odds_snapshot, "capture_live_snapshot") as capture:
        capture.return_value = self.snapshot
        with patch.object(sys, "argv", [
            "capture_odds_snapshot.py", "--date", TARGET_DATE,
            "--phase", "decision", "--live",
        ]):
            self.assertEqual(0, capture_odds_snapshot.main())
    self.assertEqual("decision", capture.call_args.kwargs["phase"])
```

- [ ] **Step 2: Run tests and verify missing phase fields/argument**

Run: `python -m unittest tests.test_live_odds tests.test_capture_odds_snapshot -v`

Expected: FAIL because live capture ignores the CLI phase.

- [ ] **Step 3: Add canonical phase classification**

```python
LIVE_PHASES = {
    "opening", "decision", "monitoring", "pre_kickoff_90", "pre_kickoff_30"
}


def _match_phase(requested: str, minutes: int) -> str:
    if minutes <= 45:
        return "pre_kickoff_30"
    if minutes <= 105:
        return "pre_kickoff_90"
    return requested
```

Validate `phase` before fetching, calculate non-negative whole `minutes_to_kickoff`, include both fields in the canonical payload and validator, and call:

```python
capture_live_snapshot(
    ROOT, target_date, datetime.now(BEIJING), phase=args.phase
)
```

Do not change the immutable filename or overwrite semantics.

- [ ] **Step 4: Run all live odds and revalidation tests**

Run: `python -m unittest tests.test_live_odds tests.test_capture_odds_snapshot tests.test_revalidation tests.test_pre_kickoff_rehearsal -v`

Expected: PASS.

- [ ] **Step 5: Commit phase evidence**

```bash
git add live_odds.py capture_odds_snapshot.py tests/test_live_odds.py tests/test_capture_odds_snapshot.py
git commit -m "feat: label live odds capture phases"
```

---

### Task 6: Unified Snapshot Coverage and Evidence Health

**Files:**
- Create: `evidence_health.py`
- Create: `tests/test_evidence_health.py`
- Modify: `model_metrics.py:201-252`
- Modify: `tests/test_model_metrics.py`
- Modify: `report_status.py:506-650`
- Modify: `tests/test_report_status.py`

**Interfaces:**
- Produces: `build_evidence_health(root: Path, target_date: date, now: datetime, *, zero_fixture_verified: bool) -> dict`
- Extends: `snapshot_coverage(snapshot_dir: Path = SNAPSHOT_DIR, live_snapshot_dir: Path = LIVE_SNAPSHOT_DIR, target_date: date | None = None) -> dict`
- Report status adds schema-compatible `evidence_health`; identity blockers gate forecast readiness and decision blockers gate decision/provisional readiness

- [ ] **Step 1: Write failing health and coverage tests**

```python
class EvidenceHealthTest(unittest.TestCase):
    def test_health_blocks_non_unique_identity_and_reports_rates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            (data / "bet_results.csv").write_text(
                "date,match_id,home_goals,away_goals,result_status,result_source,"
                "source_record_id,captured_at_bjt,score_scope,settlement_minutes\n"
                "2026-07-21,1,1,1,finished,sporttery,1,"
                "2026-07-22T12:00:00+08:00,regular_time_90,90\n",
                encoding="utf-8",
            )
            coverage = {
                "files": 1, "matches": 2,
                "phases": {"decision": 2, "pre_kickoff_90": 0, "pre_kickoff_30": 0},
                "requested_phases": {"decision": 2},
                "latest": "2026-07-21T13:45:00+08:00",
                "latest_by_phase": {"decision": "2026-07-21T13:45:00+08:00"},
                "latest_by_requested_phase": {"decision": "2026-07-21T13:45:00+08:00"},
            }
            with (
                patch("evidence_health.fixture_identity_rate", return_value=(1, 2)),
                patch("evidence_health.snapshot_coverage", return_value=coverage),
            ):
                health = build_evidence_health(
                    root, date(2026, 7, 21),
                    datetime(2026, 7, 21, 14, 0, tzinfo=BJT),
                    zero_fixture_verified=False,
                )
        self.assertEqual(0.5, health["identity_confirmation_rate"])
        self.assertIn("identity_not_unique", health["hard_blockers"])
        self.assertEqual(1.0, health["result_provenance_rate"])


class SnapshotCoverageTest(unittest.TestCase):
    def test_counts_nested_live_pre_kickoff_phases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            live = root / "live" / "2026-07-21"
            legacy.mkdir()
            live.mkdir(parents=True)
            for index, phase in enumerate(("pre_kickoff_90", "pre_kickoff_30")):
                (live / f"{index}.json").write_text(json.dumps({
                    "target_date": "2026-07-21",
                    "captured_at": f"2026-07-21T1{index + 6}:00:00+08:00",
                    "capture_phase": phase,
                    "matches": [{"capture_phase": phase}],
                }), encoding="utf-8")
            coverage = snapshot_coverage(legacy, root / "live", date(2026, 7, 21))
        self.assertEqual(1, coverage["phases"]["pre_kickoff_90"])
        self.assertEqual(1, coverage["phases"]["pre_kickoff_30"])
```

- [ ] **Step 2: Run focused tests and verify missing interfaces**

Run: `python -m unittest tests.test_evidence_health tests.test_model_metrics tests.test_report_status -v`

Expected: FAIL because `evidence_health` and the second snapshot directory argument do not exist.

- [ ] **Step 3: Implement the read-only health report**

```python
import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fixture_identity import fixture_identity_rate
from model_metrics import snapshot_coverage
from result_evidence import proven_90_minute_result


BEIJING = timezone(timedelta(hours=8))


def build_evidence_health(
    root: Path,
    target_date: date,
    now: datetime,
    *,
    zero_fixture_verified: bool,
) -> dict:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("evidence health now must include a timezone")
    now_bjt = now.astimezone(BEIJING)
    confirmed, total = fixture_identity_rate(root, target_date)
    results = _result_rows(root / "data" / "bet_results.csv")
    finished = [row for row in results if row.get("date") == target_date.isoformat()]
    proven = sum(proven_90_minute_result(row) for row in finished)
    coverage = snapshot_coverage(
        root / "data" / "odds_snapshots",
        root / "data" / "live_odds_snapshots",
        target_date,
    )
    identity_rate = confirmed / total if total else (1.0 if zero_fixture_verified else 0.0)
    result_rate = proven / len(finished) if finished else None
    forecast_blockers = []
    decision_blockers = []
    if identity_rate < 1.0:
        forecast_blockers.append("identity_not_unique")
    if total and coverage["requested_phases"].get("decision", 0) < total:
        decision_blockers.append("decision_snapshot_incomplete")
    decision_at = _aware(
        coverage.get("latest_by_requested_phase", {}).get("decision")
    )
    if total and decision_at is not None and decision_at > now_bjt:
        decision_blockers.append("decision_odds_from_future")
    if total and (decision_at is None or now_bjt - decision_at > timedelta(minutes=30)):
        decision_blockers.append("decision_odds_stale")
    hard_blockers = list(dict.fromkeys(forecast_blockers + decision_blockers))
    return {
        "schema_version": 1,
        "target_date": target_date.isoformat(),
        "generated_at_bjt": now_bjt.isoformat(),
        "identity_confirmed": confirmed,
        "identity_total": total,
        "identity_confirmation_rate": identity_rate,
        "result_provenance_rate": result_rate,
        "snapshot_coverage": coverage,
        "forecast_blockers": forecast_blockers,
        "decision_blockers": decision_blockers,
        "hard_blockers": hard_blockers,
    }


def _result_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _aware(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(BEIJING)
```

Implement the snapshot path merge without changing existing callers:

```python
LIVE_SNAPSHOT_DIR = ROOT / "data" / "live_odds_snapshots"


def snapshot_coverage(
    snapshot_dir: Path = SNAPSHOT_DIR,
    live_snapshot_dir: Path = LIVE_SNAPSHOT_DIR,
    target_date: date | None = None,
) -> dict:
    phases = {
        "opening": 0, "decision": 0, "monitoring": 0, "pre_kickoff": 0,
        "pre_kickoff_90": 0, "pre_kickoff_30": 0,
    }
    files = matches = 0
    latest = None
    latest_by_phase = {}
    requested_phases = {}
    latest_by_requested_phase = {}
    paths = []
    if snapshot_dir.exists():
        paths.extend(snapshot_dir.glob("*.json"))
    if live_snapshot_dir.exists():
        paths.extend(live_snapshot_dir.rglob("*.json"))
    for path in sorted(set(paths)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        payload_date = str(payload.get("target_date") or "")
        if target_date is not None and payload_date != target_date.isoformat():
            continue
        files += 1
        captured = str(payload.get("captured_at") or "")
        latest = max(latest or captured, captured)
        requested = str(payload.get("capture_phase") or "monitoring")
        for match in payload.get("matches", []):
            if not isinstance(match, dict):
                continue
            matches += 1
            phase = str(match.get("capture_phase") or payload.get("capture_phase") or "monitoring")
            phases[phase] = phases.get(phase, 0) + 1
            latest_by_phase[phase] = max(latest_by_phase.get(phase, ""), captured)
            requested_phases[requested] = requested_phases.get(requested, 0) + 1
            latest_by_requested_phase[requested] = max(
                latest_by_requested_phase.get(requested, ""), captured
            )
    return {
        "files": files, "matches": matches, "phases": phases,
        "requested_phases": requested_phases,
        "latest": latest, "latest_by_phase": latest_by_phase,
        "latest_by_requested_phase": latest_by_requested_phase,
    }
```

Wire the health result into `report_status.publish_status`:

```python
from evidence_health import build_evidence_health

# Immediately after the existing artifact_state call:
health = build_evidence_health(
    root,
    report_date,
    generated_at,
    zero_fixture_verified=verified_zero_fixture_day(root, report_date),
)

# Add this condition to the existing forecast_ready expression:
forecast_ready = all(
    state[key]
    for key in (
        "source_ready", "fixtures_ready", "import_manifest_ready", "odds_ready",
        "official_odds_complete", "predictions_ready", "site_ready", "image_ready",
    )
) and not health["forecast_blockers"]

# Tighten the existing decision snapshot gate:
snapshot_ready = state["decision_snapshot_ready"] and not health["decision_blockers"]

# Include this entry in the existing status.update mapping:
"evidence_health": health,
```

Add report-status tests for an identity blocker forcing `forecast_ready` false, a decision blocker forcing `decision_snapshot_ready` false without retroactively invalidating a completed forecast, and empty blocker lists preserving existing readiness. Invalid snapshot JSON is skipped and therefore causes incomplete/stale decision evidence rather than a permissive pass. A no-fixture day is healthy only when `verified_zero_fixture_day` proves it.

- [ ] **Step 4: Run metrics, status, site, and image regressions**

Run: `python -m unittest tests.test_evidence_health tests.test_model_metrics tests.test_report_status tests.test_report_build_metadata -v`

Expected: PASS. Existing status consumers continue to parse schema 2 while reading the optional evidence-health field.

- [ ] **Step 5: Commit evidence health**

```bash
git add evidence_health.py tests/test_evidence_health.py model_metrics.py tests/test_model_metrics.py report_status.py tests/test_report_status.py
git commit -m "feat: publish evidence health gates"
```

---

### Task 7: Workflow Integration and End-to-End Evidence Replay

**Files:**
- Modify: `.github/workflows/noon-settlement.yml`
- Modify: `.github/workflows/odds-snapshot.yml`
- Modify: `.github/workflows/daily-forecast.yml`
- Modify: `tests/test_workflow_schedule.py`
- Create: `tests/test_evidence_pipeline_replay.py`
- Modify: `CLOUD_SETUP.md`
- Modify: `README.md`

**Interfaces:**
- Daily forecast must publish identity health before prediction readiness.
- Snapshot workflow must use live capture and explicit phases.
- Settlement must reconcile results before history features, ledger settlement, metrics, or training.
- Replay test uses only temporary immutable artifacts and no network.

Task 5 compatibility clarification: replay fixtures must use the strict live
snapshot v2 contract and its canonical immutable filename. Historical v1
snapshots remain readable but cannot be used to synthesize phase evidence.

- [ ] **Step 1: Add failing ordering and replay tests**

```python
import csv
import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import update_sporttery_results as results
from evidence_health import build_evidence_health
from result_evidence import proven_90_minute_result


BJT = timezone(timedelta(hours=8))


def write_import_manifest_fixture(
    root: Path, *, match_id: str, home: str, away: str, kickoff: str
) -> None:
    day = date.fromisoformat(kickoff[:10])
    extracts = root / "data" / "import_extracts" / day.isoformat()
    manifests = root / "data" / "import_manifests"
    extracts.mkdir(parents=True)
    manifests.mkdir(parents=True)
    fixture = extracts / "fixtures.csv"
    fixture.write_text(
        "date,team_a,team_b,match_id,kickoff_at\n"
        f"{day.isoformat()},{home},{away},{match_id},{kickoff}\n",
        encoding="utf-8",
    )
    odds = extracts / "odds.json"
    odds.write_text("{}", encoding="utf-8")
    ratings = extracts / "ratings.csv"
    ratings.write_text(f"team,elo\n{home},1500\n{away},1500\n", encoding="utf-8")
    records = {}
    for name, path in (("fixtures", fixture), ("odds", odds), ("ratings", ratings)):
        payload = path.read_bytes()
        records[name] = {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
    (manifests / f"{day.isoformat()}.json").write_text(json.dumps({
        "schema_version": 2,
        "target_date": day.isoformat(),
        "source": "sporttery",
        "imported_at_bjt": f"{day.isoformat()}T12:05:00+08:00",
        **records,
    }, ensure_ascii=False), encoding="utf-8")


def write_live_snapshot(root: Path, phase: str, captured_at: str) -> None:
    directory = root / "data" / "live_odds_snapshots" / "2026-07-21"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "target_date": "2026-07-21",
        "captured_at": captured_at,
        "capture_phase": phase,
        "fetch_mode": "live",
        "source": "sporttery",
        "response_sha256": "0" * 64,
        "matches": [{
            "match_id": "2040580",
            "source_record_id": "2040580",
            "team_a": "Team A",
            "team_b": "Team B",
            "kickoff_at": "2026-07-21T18:00:00+08:00",
            "capture_phase": phase,
            "minutes_to_kickoff": 255,
        }],
    }
    (directory / f"{phase}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


class EvidencePipelineReplayTest(unittest.TestCase):
    def test_settlement_orders_reconciliation_before_learning(self):
        workflow = Path(".github/workflows/noon-settlement.yml").read_text(
            encoding="utf-8"
        )
        reconcile = workflow.index("--reconcile-days 7")
        history = workflow.index("build_historical_features.py")
        training = workflow.index("draw_model_learning.py --train")
        self.assertLess(reconcile, history)
        self.assertLess(history, training)

    def test_complete_replay_promotes_result_and_reports_healthy_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_import_manifest_fixture(
                root,
                match_id="2040580",
                home="Team A",
                away="Team B",
                kickoff="2026-07-21T18:00:00+08:00",
            )
            data = root / "data"
            (data / "fixtures.csv").write_text(
                "date,team_a,team_b,match_id\n2026-07-22,New A,New B,9999\n",
                encoding="utf-8",
            )
            fallback = [{
                "homeTeam": "Team A", "awayTeam": "Team B",
                "score": "1:1", "source_record_id": "88",
            }]
            with (
                patch.object(results, "ROOT", root),
                patch.object(results, "DATA_DIR", data),
                patch.object(
                    results, "official_result_rows", side_effect=RuntimeError("offline")
                ),
                patch.object(results, "fetch_zgzcw_results", return_value=fallback),
            ):
                result_path = results.update_results(date(2026, 7, 21))
            with result_path.open(encoding="utf-8-sig", newline="") as handle:
                result_row = list(csv.DictReader(handle))[0]
            self.assertEqual("2040580", result_row["match_id"])
            self.assertTrue(proven_90_minute_result(result_row))

            write_live_snapshot(root, "decision", "2026-07-21T13:45:00+08:00")
            health = build_evidence_health(
                root,
                date(2026, 7, 21),
                datetime(2026, 7, 21, 14, 0, tzinfo=BJT),
                zero_fixture_verified=False,
            )
            self.assertEqual(1.0, health["identity_confirmation_rate"])
            self.assertEqual(1.0, health["result_provenance_rate"])
            self.assertEqual([], health["hard_blockers"])
```

- [ ] **Step 2: Run workflow and replay tests and verify ordering/fixture failures**

Run: `python -m unittest tests.test_workflow_schedule tests.test_evidence_pipeline_replay -v`

Expected: FAIL until all commands and replay contracts are integrated.

- [ ] **Step 3: Wire the workflows in fail-closed order**

The relevant command order must be:

```yaml
- name: Capture live evidence
  run: python capture_odds_snapshot.py --date "$TARGET_DATE" --phase decision --live

- name: Reconcile recent results
  run: python update_sporttery_results.py --date "$SETTLEMENT_DATE" --reconcile-days 7

- name: Build proven historical features
  run: python build_historical_features.py

- name: Settle proven results
  run: python generate_betting_plan.py --settle-only

- name: Train shadow draw challenger
  run: python draw_model_learning.py --train --date "$TODAY"
```

Preserve existing concurrency, commit/publish, report-status, and Apps Script contracts. Document that GitHub Actions may retry but cannot duplicate results or email.

- [ ] **Step 4: Run the complete local verification suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`

Expected: PASS with no network access.

Run: `node --test tests/apps_script_orchestrator.test.mjs`

Expected: PASS; Apps Script remains the sole sender and retains Beijing 14:00-18:00 behavior.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 5: Commit the integrated evidence foundation**

```bash
git add .github/workflows/noon-settlement.yml .github/workflows/odds-snapshot.yml .github/workflows/daily-forecast.yml tests/test_workflow_schedule.py tests/test_evidence_pipeline_replay.py CLOUD_SETUP.md README.md
git commit -m "feat: enforce evidence-first workflow order"
```

---

## Project 1 Acceptance Gate

Before planning Project 2, verify all of the following on a production-shaped dry run:

- A previous business day's result can resolve from its immutable manifest after `data/fixtures.csv` contains only the new day.
- A fallback score without source record identity remains unavailable.
- A conflicting or ambiguous score never settles and never becomes a training sample.
- Re-running the same seven-day reconciliation is byte-idempotent.
- Decision, T-90, and T-30 live snapshots are actually fetched and counted separately.
- Evidence health reports 100% unique identity for eligible fixtures.
- Core readiness is false when a hard blocker exists.
- The draw training sample file is non-empty only when at least one fully proven fixture/result pair exists.
- The full Python and Apps Script test suites pass.
- No real-money setting, strategy activation mode, stake, or email sender changes.

After deployment, observe seven daily runs before approving Project 2 planning. The broader 30-day operational acceptance remains required before any model or profitability claim.
