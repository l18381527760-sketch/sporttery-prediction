# Reliable Daily Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Apps Script the timezone-safe orchestrator and sole email sender, while GitHub Actions publishes a cryptographically verifiable same-day report that can be retried until 18:00 Beijing time without sending stale content or duplicate mail.

**Architecture:** Keep prediction and settlement computation in GitHub Actions. Add an immutable daily plan-lock manifest and an atomic `web/report-status.json` contract. A versioned Apps Script state machine reads that contract, dispatches the next missing workflow, verifies the PNG hash, and sends either one normal report or one failure notice per Beijing business date.

**Tech Stack:** Python 3.12, standard-library JSON/CSV/hashlib, Pillow PNG metadata, unittest, Node.js built-in test runner, Google Apps Script, GitHub Actions, GitHub Pages.

## Global Constraints

- Phase 1 must not change probability formulas, value thresholds, staking formulas, or settlement rules.
- Use `Asia/Shanghai` for every business-date decision; never derive the report date from the Apps Script account timezone or a US timezone.
- Keep GitHub cron schedules as fallback triggers and keep `.github/workflows/email-report.yml` disabled in GitHub.
- Apps Script is the only email sender after deployment.
- A normal email requires a supported status schema, today's Beijing date, all four readiness flags, settlement through yesterday, a locked plan, and an image whose computed SHA-256 matches the status file.
- Zero fixtures and zero bets are valid only when the workflow explicitly completed and wrote zero counts; missing artifacts are never interpreted as zero.
- Retry incomplete work through 18:00 Beijing time. At or after 18:00, send at most one failure notice and never attach an old report image.
- Never place the GitHub token or recipient address in committed source. Store both in Apps Script Script Properties.
- Preserve the current public site and generated CSV/JSON history.
- Every workflow rerun for the same date must be idempotent.

## File Structure

- Create `plan_lock.py`: atomically lock the decision-time plan and odds file by SHA-256.
- Create `report_status.py`: merge workflow phase completion into `web/report-status.json` and hash the final PNG.
- Create `apps-script/Code.gs`: production orchestrator, report validator, dispatch client, deduplication, and email sender.
- Create `apps-script/appsscript.json`: Shanghai timezone and required Apps Script scopes.
- Create `apps-script/README.md`: exact deployment and recovery procedure.
- Create `tests/test_plan_lock.py`, `tests/test_report_status.py`, `tests/test_report_build_metadata.py`, and `tests/apps_script_orchestrator.test.mjs`.
- Modify `build_site.py` and `build_daily_image.py`: embed the workflow `build_id` in HTML/PNG metadata.
- Modify the three report-writing workflows and `tests/test_workflow_schedule.py`.
- Modify `README.md` and `CLOUD_SETUP.md` to document Apps Script as the primary path.

---

### Task 1: Decision-Time Plan Lock

**Files:**
- Create: `plan_lock.py`
- Create: `tests/test_plan_lock.py`

**Interfaces:**
- `sha256_file(path: Path) -> str`
- `read_valid_lock(root: Path, target_date: date) -> dict | None`
- `lock_plan(root: Path, target_date: date, locked_at: datetime, source: str) -> dict`
- CLI: `python plan_lock.py is-locked --date YYYY-MM-DD`
- CLI: `python plan_lock.py lock --date YYYY-MM-DD --locked-at ISO-8601 --source sporttery`

- [ ] **Step 1: Write failing lock and idempotency tests**

```python
import csv
import json
import tempfile
import unittest
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from plan_lock import lock_plan, read_valid_lock


BJT = timezone(timedelta(hours=8))


class PlanLockTest(unittest.TestCase):
    def make_artifacts(self, root: Path) -> None:
        (root / "output").mkdir()
        (root / "data").mkdir()
        with (root / "output" / "betting_plan_2026-07-16.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=["date", "match", "stake"])
            writer.writeheader()
            writer.writerow({"date": "2026-07-16", "match": "A vs B", "stake": 20})
        (root / "data" / "sporttery_odds_2026-07-16.json").write_text(
            json.dumps({"001": {"had": {"h": "2.00"}}}), encoding="utf-8"
        )

    def test_lock_is_valid_only_while_plan_and_odds_hashes_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            lock_plan(
                root,
                date(2026, 7, 16),
                datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
                "sporttery",
            )
            self.assertIsNotNone(read_valid_lock(root, date(2026, 7, 16)))
            (root / "output" / "betting_plan_2026-07-16.csv").write_text(
                "changed", encoding="utf-8"
            )
            self.assertIsNone(read_valid_lock(root, date(2026, 7, 16)))

    def test_relocking_an_unchanged_plan_preserves_the_first_lock_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            first = lock_plan(root, date(2026, 7, 16), datetime(2026, 7, 16, 13, 31, tzinfo=BJT), "sporttery")
            second = lock_plan(root, date(2026, 7, 16), datetime(2026, 7, 16, 14, 5, tzinfo=BJT), "sporttery")
            self.assertEqual(first, second)
            self.assertEqual("2026-07-16T13:31:00+08:00", second["locked_at_bjt"])
```

- [ ] **Step 2: Run the test and verify the module is absent**

Run: `python -m unittest tests.test_plan_lock -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'plan_lock'`.

- [ ] **Step 3: Implement atomic lock creation and hash validation**

Use schema version 1 and this exact payload shape:

```python
{
    "schema_version": 1,
    "report_date": target_date.isoformat(),
    "locked_at_bjt": locked_at.astimezone(BEIJING).isoformat(),
    "plan_path": f"output/betting_plan_{target_date.isoformat()}.csv",
    "plan_sha256": sha256_file(plan_path),
    "odds_path": f"data/sporttery_odds_{target_date.isoformat()}.json",
    "odds_sha256": sha256_file(odds_path),
    "odds_source": source,
}
```

Write to `output/plan_lock_YYYY-MM-DD.json.tmp`, flush and close it, then use `Path.replace()` to publish `output/plan_lock_YYYY-MM-DD.json`. `read_valid_lock()` must reject wrong dates, unsupported schemas, missing files, empty hashes, and either hash mismatch. `lock_plan()` must return the existing payload unchanged when it is already valid.

- [ ] **Step 4: Add CLI behavior tests**

Patch `sys.argv`, call `main()`, and assert:

- `is-locked` exits `0` for a valid lock and `1` for a missing or invalid lock.
- `lock` exits nonzero if the plan or odds artifact is absent.
- a naive `--locked-at` timestamp is rejected instead of silently assuming a timezone.

- [ ] **Step 5: Run the focused tests**

Run: `python -m unittest tests.test_plan_lock -v`

Expected: PASS.

- [ ] **Step 6: Commit the lock primitive**

```bash
git add plan_lock.py tests/test_plan_lock.py
git commit -m "feat: lock decision-time plans by checksum"
```

---

### Task 2: Atomic Report-Status Contract

**Files:**
- Create: `report_status.py`
- Create: `tests/test_report_status.py`

**Interfaces:**
- `base_status(report_date: date) -> dict`
- `artifact_state(root: Path, report_date: date) -> dict`
- `publish_status(root: Path, report_date: date, phase: str, build_id: str, source_commit_sha: str, generated_at: datetime, settled_through: date | None = None) -> dict`
- CLI phases: `forecast`, `decision`, `settlement`

- [ ] **Step 1: Write failing status merge tests**

Cover these exact cases in `ReportStatusTest`: a new business date discards yesterday's flags; three same-day phases merge without losing prior flags; decision requires a valid lock and decision snapshot; a verified zero-fixture day can complete without match rows; nonzero fixtures expose partial odds coverage; the status hash matches exact PNG bytes; and settlement cannot claim a date before yesterday.

The complete-path assertion must include:

```python
self.assertEqual(1, status["schema_version"])
self.assertEqual("2026-07-16", status["report_date"])
self.assertTrue(status["forecast_ready"])
self.assertTrue(status["decision_snapshot_ready"])
self.assertTrue(status["settlement_ready"])
self.assertTrue(status["plan_ready"])
self.assertEqual("2026-07-15", status["settled_through"])
self.assertRegex(status["image_sha256"], r"^[0-9a-f]{64}$")
```

- [ ] **Step 2: Run the test and verify the status publisher is absent**

Run: `python -m unittest tests.test_report_status -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'report_status'`.

- [ ] **Step 3: Implement artifact inspection**

`artifact_state()` must inspect, without network access:

- `data/source_status.json` and require its `target_date` to equal the report date.
- `data/fixtures.csv`, counting only rows for the report date.
- `data/sporttery_odds_YYYY-MM-DD.json`, counting a fixture as covered when its match ID has at least one non-empty `had`, `hhad`, or `ttg` market.
- `output/predictions_YYYY-MM-DD.csv`.
- `output/betting_plan_YYYY-MM-DD.csv`; an empty CSV with a valid header is a valid zero-bet plan.
- `output/daily_decision_YYYY-MM-DD.json`.
- `output/plan_lock_YYYY-MM-DD.json` through `read_valid_lock()`.
- the newest `data/odds_snapshots/YYYY-MM-DD-HHMMSS-decision.json` whose payload date and phase match.
- `output/betting_ledger.csv`, `web/index.html`, and a non-empty `web/daily-report.png`.

Set coverage to `1.0` when fixture count is zero. Never treat a missing fixtures file as a zero-fixture day.

- [ ] **Step 4: Implement same-day phase merging and atomic publication**

Start from the existing `web/report-status.json` only when its schema and `report_date` match. Otherwise start from:

```python
{
    "schema_version": 1,
    "report_date": report_date.isoformat(),
    "forecast_ready": False,
    "decision_snapshot_ready": False,
    "settlement_ready": False,
    "plan_ready": False,
    "settled_through": "",
    "decision_odds_at_bjt": "",
    "plan_locked_at_bjt": "",
}
```

Apply only the requested phase transition:

- `forecast`: set `forecast_ready` only when source, fixtures, predictions, plan CSV, decision JSON, site, and image exist.
- `decision`: set `decision_snapshot_ready` when a matching decision snapshot exists, or when a verified zero-fixture day is explicit; set `plan_ready` only when the lock validates.
- `settlement`: set `settlement_ready` only when `settled_through >= report_date - 1 day` and the ledger, site, and image exist.

Every successful publication refreshes `build_id`, `generated_at_bjt`, `image_sha256`, `source_commit_sha`, counts, coverage, `data_quality`, and `source_status`. Write the JSON through a sibling temporary file and `Path.replace()`.

- [ ] **Step 5: Add CLI tests and implement argument parsing**

Required CLI form:

```bash
python report_status.py \
  --date 2026-07-16 \
  --phase decision \
  --build-id 123456-1-decision \
  --source-commit abc123 \
  --generated-at 2026-07-16T13:35:00+08:00
```

`--settled-through` is required only for the `settlement` phase. A naive `--generated-at`, blank build ID, blank source commit, or unsupported phase must fail closed.

- [ ] **Step 6: Run focused tests and inspect the generated schema**

Run: `python -m unittest tests.test_plan_lock tests.test_report_status -v`

Expected: PASS.

- [ ] **Step 7: Commit the status publisher**

```bash
git add report_status.py tests/test_report_status.py
git commit -m "feat: publish atomic report readiness status"
```

---

### Task 3: Bind Site and PNG to the Same Build

**Files:**
- Modify: `build_site.py`
- Modify: `build_daily_image.py`
- Create: `tests/test_report_build_metadata.py`

- [ ] **Step 1: Write failing metadata tests**

```python
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import build_daily_image
import build_site


class ReportBuildMetadataTest(unittest.TestCase):
    def test_site_contains_machine_readable_build_id(self):
        with patch.dict(os.environ, {"REPORT_BUILD_ID": "run-42-decision"}):
            html = build_site.render_site([])
        self.assertIn('<meta name="report-build-id" content="run-42-decision">', html)

    def test_png_contains_the_same_build_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            web = root / "web"
            output.mkdir()
            with patch.object(build_daily_image, "OUTPUT_DIR", output), patch.object(
                build_daily_image, "WEB_DIR", web
            ), patch.dict(os.environ, {"REPORT_BUILD_ID": "run-42-decision"}):
                path = build_daily_image.draw_report()
            with Image.open(path) as image:
                self.assertEqual("run-42-decision", image.info["build_id"])
```

- [ ] **Step 2: Run the tests and confirm the metadata is missing**

Run: `python -m unittest tests.test_report_build_metadata -v`

Expected: FAIL because neither HTML nor PNG contains `run-42-decision`.

- [ ] **Step 3: Add shared build-ID handling**

In both builders use:

```python
BUILD_ID = os.environ.get("REPORT_BUILD_ID", "local")
```

Escape the value before placing it in HTML. In `build_daily_image.py`, use `PngImagePlugin.PngInfo()` and `pnginfo.add_text("build_id", BUILD_ID)`, then pass `pnginfo=pnginfo` to `image.save()`. Do not render the build ID as visible page text.

- [ ] **Step 4: Run metadata and existing reporting tests**

Run: `python -m unittest tests.test_report_build_metadata tests.test_draw_alert_reporting -v`

Expected: PASS.

- [ ] **Step 5: Commit build binding**

```bash
git add build_site.py build_daily_image.py tests/test_report_build_metadata.py
git commit -m "feat: bind report artifacts to build id"
```

---

### Task 4: Make GitHub Workflows Publish Phased Readiness

**Files:**
- Modify: `.github/workflows/daily-forecast.yml`
- Modify: `.github/workflows/draw-alert-refresh.yml`
- Modify: `.github/workflows/noon-settlement.yml`
- Modify: `tests/test_workflow_schedule.py`

- [ ] **Step 1: Add failing workflow-contract tests**

Add tests requiring all three workflows to define an optional `target_date` string input and to derive a fallback Beijing date only when the input is blank. Assert the following exact commands occur after `build_daily_image.py` and before the auto-commit step:

```text
python report_status.py --date "$TARGET_DATE" --phase forecast
python report_status.py --date "$TARGET_DATE" --phase decision
python report_status.py --date "$TODAY" --phase settlement --settled-through "$SETTLEMENT_DATE"
```

Also assert:

- each build step exports `REPORT_BUILD_ID="${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-<phase>"`;
- each status command passes that build ID, `SOURCE_COMMIT_SHA="$(git rev-parse HEAD)"`, and an explicit Shanghai ISO timestamp;
- decision refresh runs `capture_odds_snapshot.py`, `predict_today.py`, `generate_betting_plan.py`, and `plan_lock.py lock` as required steps;
- `plan_lock.py is-locked` prevents a valid locked plan from being regenerated on a rerun;
- all generated commits still include `data output web`;
- `email-report.yml` is not added to any dispatch path.

- [ ] **Step 2: Run the workflow tests and verify failure**

Run: `python -m unittest tests.test_workflow_schedule -v`

Expected: FAIL on missing dispatch inputs, plan locking, build IDs, and status commands.

- [ ] **Step 3: Normalize target-date inputs**

Add this input to each report-writing workflow:

```yaml
workflow_dispatch:
  inputs:
    target_date:
      description: Beijing business date (YYYY-MM-DD)
      required: false
      type: string
```

At the beginning of each required run block use `${{ inputs.target_date }}` and fall back to `date +%F`. Validate with `date -d "$TARGET_DATE" +%F` and reject any normalized value that differs from the input.

- [ ] **Step 4: Make decision capture and plan locking required**

In `draw-alert-refresh.yml`, keep external market heat and draw-alert generation optional, but make this ordered block required:

```bash
TARGET_DATE="${{ inputs.target_date }}"
TARGET_DATE="${TARGET_DATE:-$(date +%F)}"
python import_sporttery.py --date "$TARGET_DATE"
python capture_odds_snapshot.py --date "$TARGET_DATE" --phase decision
if python plan_lock.py is-locked --date "$TARGET_DATE"; then
  echo "Decision plan is already locked for $TARGET_DATE"
else
  python predict_today.py --date "$TARGET_DATE"
  python generate_betting_plan.py --date "$TARGET_DATE"
  python plan_lock.py lock \
    --date "$TARGET_DATE" \
    --locked-at "$(date --iso-8601=seconds)" \
    --source sporttery
fi
```

Do not use `continue-on-error` on this block.

- [ ] **Step 5: Publish status after the final image in every phase**

Set `REPORT_BUILD_ID` before running both builders. Then call `report_status.py` only after `build_site.py` and `build_daily_image.py` succeed. A status publication failure must fail the workflow and prevent Pages publication.

For settlement, use today's Beijing date as `report_date` and yesterday as `settled_through`. Preserve the shared repository concurrency group and latest-main checkout.

- [ ] **Step 6: Run workflow tests and the complete Python suite**

Run: `python -m unittest tests.test_workflow_schedule tests.test_plan_lock tests.test_report_status tests.test_report_build_metadata -v`

Expected: PASS.

Run: `python -m unittest discover -s tests -v`

Expected: all tests PASS.

- [ ] **Step 7: Commit workflow integration**

```bash
git add .github/workflows/daily-forecast.yml .github/workflows/draw-alert-refresh.yml .github/workflows/noon-settlement.yml tests/test_workflow_schedule.py
git commit -m "feat: publish phased report readiness from workflows"
```

---

### Task 5: Apps Script State Machine and Hash-Verified Email

**Files:**
- Create: `apps-script/Code.gs`
- Create: `apps-script/appsscript.json`
- Create: `tests/apps_script_orchestrator.test.mjs`

**Script Properties:**
- Required configuration: `GITHUB_TOKEN`, `GITHUB_OWNER`, `GITHUB_REPO`, `REPORT_STATUS_URL`, `REPORT_IMAGE_URL`, `REPORT_SITE_URL`, `RECIPIENT_EMAIL`.
- Runtime state: `LAST_FORECAST_DISPATCH_DATE`, `LAST_FORECAST_DISPATCH_AT`, `LAST_REFRESH_DISPATCH_DATE`, `LAST_REFRESH_DISPATCH_AT`, `LAST_SETTLEMENT_DISPATCH_DATE`, `LAST_SETTLEMENT_DISPATCH_AT`, `LAST_SENT_DATE`, `LAST_SENT_IMAGE_SHA256`, `LAST_FAILURE_NOTICE_DATE`.
- Optional: `TEST_MODE=true` logs sends without calling Gmail.

- [ ] **Step 1: Write failing pure state-machine tests**

Load `apps-script/Code.gs` with `node:vm` and inject mocks for `Utilities`, `UrlFetchApp`, `GmailApp`, `PropertiesService`, `LockService`, and `ScriptApp`. Add named tests proving: 12:14 does not dispatch; 12:15 dispatches only forecast when missing; 13:30 waits for forecast before refresh; 13:45 waits for decision before settlement; 14:00 rejects yesterday's status/image; ready status plus a matching image hash sends once; a mismatched hash never sends; 18:00 incomplete state sends one attachment-free failure notice; a report becoming ready after that failure notice does not send a late normal email; normal and failure mail deduplicate by Beijing date; and a failed Gmail call does not write `LAST_SENT_DATE`.

- [ ] **Step 2: Run the Node tests and verify the script is absent**

Run: `node --test tests/apps_script_orchestrator.test.mjs`

Expected: FAIL with `ENOENT` for `apps-script/Code.gs`.

- [ ] **Step 3: Implement pure clock, validation, and action selection helpers**

Expose these underscore-suffixed functions for the Node harness:

```javascript
function beijingClock_(now) {}
function reportReadiness_(status, expectedDate, imageSha256) {}
function chooseDispatch_(clock, status, state) {}
function missingReasons_(status, expectedDate) {}
function sha256Hex_(bytes) {}
```

`chooseDispatch_()` must return at most one workflow. It must enforce phase order: forecast before refresh, refresh before settlement. A missing earlier phase blocks dispatch of a later phase. Apply a 30-minute cooldown to repeated dispatch of the same phase.

`reportReadiness_()` must reject unsupported schema versions, wrong dates, false readiness flags, settlement before yesterday, missing/invalid timestamps, `plan_locked_at_bjt > generated_at_bjt`, blank build IDs, blank hashes, empty image bytes, and hash mismatch.

- [ ] **Step 4: Implement GitHub dispatch and report fetching**

Dispatch endpoint:

```text
POST https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches
```

Use `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, and payload:

```json
{"ref":"main","inputs":{"target_date":"2026-07-16"}}
```

Accept only HTTP 204 as success. Fetch status with `?ts=<milliseconds>` and fetch the image with `?build_id=<encoded-build-id>`. Do not log response headers or the token.

- [ ] **Step 5: Implement the locked orchestration entry point**

`runAutomation()` must:

1. acquire `LockService.getScriptLock().tryLock(5000)` and exit if unavailable;
2. calculate the Beijing business date;
3. fetch current status, treating fetch/parse failures as incomplete with a recorded reason;
4. dispatch at most the next missing phase after its time threshold;
5. from 14:00 onward, download and verify the image when status looks ready, unless today's failure notice was already sent;
6. send one normal report and only then write `LAST_SENT_DATE` and `LAST_SENT_IMAGE_SHA256`;
7. at or after 18:00, give a currently ready normal report first priority; otherwise send one text/HTML failure notice with no attachment, write `LAST_FAILURE_NOTICE_DATE`, and stop all later dispatch/normal-mail attempts for that business date;
8. release the lock in `finally`.

Keep a compatibility wrapper:

```javascript
function sendDailyReport() {
  return runAutomation();
}
```

- [ ] **Step 6: Implement trigger installation**

`installAutomationTrigger()` must delete existing triggers for both `runAutomation` and `sendDailyReport`, then create exactly one `runAutomation` trigger with `.timeBased().everyMinutes(10).create()`.

Use this manifest:

```json
{
  "timeZone": "Asia/Shanghai",
  "dependencies": {},
  "exceptionLogging": "STACKDRIVER",
  "runtimeVersion": "V8",
  "oauthScopes": [
    "https://www.googleapis.com/auth/script.external_request",
    "https://www.googleapis.com/auth/script.scriptapp",
    "https://www.googleapis.com/auth/gmail.send"
  ]
}
```

- [ ] **Step 7: Run all Apps Script tests**

Run: `node --test tests/apps_script_orchestrator.test.mjs`

Expected: PASS with zero real network or Gmail calls.

- [ ] **Step 8: Commit the orchestrator**

```bash
git add apps-script/Code.gs apps-script/appsscript.json tests/apps_script_orchestrator.test.mjs
git commit -m "feat: orchestrate verified daily email from apps script"
```

---

### Task 6: Deployment, Recovery, and Operator Documentation

**Files:**
- Create: `apps-script/README.md`
- Modify: `README.md`
- Modify: `CLOUD_SETUP.md`
- Modify: `tests/test_workflow_schedule.py`

- [ ] **Step 1: Add failing documentation assertions**

Require the docs to contain these literal operational facts:

- Apps Script is the sole email sender.
- trigger function is `runAutomation` every 10 minutes.
- timezone is `Asia/Shanghai`.
- normal send window is 14:00 through 18:00 Beijing time.
- the 18:00 failure notice has no stale attachment.
- exact Script Property names and least-privilege GitHub token permissions.
- GitHub email workflow remains disabled.
- rollback restores the old daily Apps Script trigger only after disabling `runAutomation`.

- [ ] **Step 2: Run the documentation test and verify failure**

Run: `python -m unittest tests.test_workflow_schedule -v`

Expected: FAIL because current docs still describe GitHub SMTP as the 14:00 sender.

- [ ] **Step 3: Write exact Apps Script setup instructions**

Document this deployment order in `apps-script/README.md`:

1. open the existing Apps Script project;
2. paste the committed `Code.gs` and update `appsscript.json` in project settings;
3. set the seven required configuration properties without placing values in source;
4. create a fine-grained token scoped only to `l18381527760-sketch/sporttery-prediction`, with Metadata read and Actions read/write;
5. set `TEST_MODE=true`, run `runAutomation` manually, and approve permissions;
6. run `installAutomationTrigger`, then verify exactly one 10-minute trigger;
7. run a manual GitHub `workflow_dispatch` for today's Beijing date and verify `web/report-status.json` plus the PNG hash;
8. set `TEST_MODE=false` only after dry-run logs are correct;
9. confirm `.github/workflows/email-report.yml` remains disabled in the GitHub Actions UI.

Include recovery steps for token revocation, trigger duplication, status mismatch, Gmail failure, and restoring the previous single daily trigger.

- [ ] **Step 4: Update top-level cloud documentation**

Replace the old fixed 14:00 GitHub SMTP description with the Apps Script dispatch/poll/send path. Keep the simulation-only disclaimer and explain that the computer can remain off.

- [ ] **Step 5: Run documentation and full regression tests**

Run: `python -m unittest tests.test_workflow_schedule -v`

Expected: PASS.

Run: `python -m unittest discover -s tests -v`

Expected: all tests PASS.

Run: `node --test tests/apps_script_orchestrator.test.mjs`

Expected: PASS.

- [ ] **Step 6: Commit deployment documentation**

```bash
git add apps-script/README.md README.md CLOUD_SETUP.md tests/test_workflow_schedule.py
git commit -m "docs: explain reliable apps script deployment"
```

---

### Task 7: Phase 1 End-to-End Verification and Rollout Gate

**Files:**
- Verify only; modify a file only to fix a discovered defect and add its regression test.

- [ ] **Step 1: Run static and unit verification**

```bash
python -m py_compile plan_lock.py report_status.py build_site.py build_daily_image.py
python -m unittest discover -s tests -v
node --test tests/apps_script_orchestrator.test.mjs
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 2: Exercise an isolated three-phase report build**

Use a temporary copy of `data`, `output`, and `web`; publish `forecast`, `decision`, and `settlement` phases in order. Assert with a short test helper that:

- yesterday's status is reset;
- each phase preserves earlier flags;
- the final four readiness flags are true;
- the final report date is today in Beijing;
- `settled_through` is yesterday;
- the downloaded PNG bytes hash to `image_sha256`;
- rerunning a phase does not alter the first valid plan lock.

- [ ] **Step 3: Deploy Apps Script in test mode**

Use the signed-in Apps Script project, replace the online code with the committed version, run `installAutomationTrigger`, and verify exactly one 10-minute trigger. Keep `TEST_MODE=true` for the first dispatch and confirm the execution log contains the intended action but no Gmail send.

- [ ] **Step 4: Perform one live no-stale-mail rehearsal**

Temporarily point `REPORT_STATUS_URL` to a known incomplete test status. Run `runAutomation` after the simulated send window and verify no normal email is sent. Restore the production URL, publish a complete status, rerun, and verify exactly one normal email with the matching image.

- [ ] **Step 5: Enable production mode**

Set `TEST_MODE=false`, confirm the GitHub email workflow is disabled, and record the Apps Script source commit in `apps-script/README.md`'s deployment log section using the actual commit hash from `git rev-parse HEAD`.

- [ ] **Step 6: Observe one complete Beijing business day**

Check the Apps Script executions after 12:15, 13:30, 13:45, 14:00, and 18:00. Phase 2 must not begin until either a normal report was sent with a matching hash or the single 18:00 failure path behaved correctly.
