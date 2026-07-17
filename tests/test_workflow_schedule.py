import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class WorkflowScheduleTest(unittest.TestCase):
    WORKFLOWS = ROOT / ".github" / "workflows"
    TARGET_DATE_STEPS = {
        "daily-forecast.yml": (
            "forecast",
            (
                "Generate required base forecast and plan",
                "Capture opening odds",
                "Collect optional market evidence",
                "Generate optional draw alerts",
                "Build final website and image",
            ),
        ),
        "draw-alert-refresh.yml": (
            "refresh",
            (
                "Refresh required decision plan",
                "Refresh optional market evidence",
                "Refresh draw alerts",
                "Rebuild report from the latest committed data",
            ),
        ),
        "noon-settlement.yml": (
            "settlement",
            ("Update results, settle ledgers, and train the draw model",),
        ),
    }
    REPORT_WORKFLOWS = {
        "daily-forecast.yml": (
            "forecast",
            "Build final website and image",
            "Commit generated files",
            "forecast",
            "$TARGET_DATE",
        ),
        "draw-alert-refresh.yml": (
            "refresh",
            "Rebuild report from the latest committed data",
            "Commit refreshed files",
            "decision",
            "$TARGET_DATE",
        ),
        "noon-settlement.yml": (
            "settlement",
            "Update results, settle ledgers, and train the draw model",
            "Commit settlement files",
            "settlement",
            "$TODAY",
        ),
    }
    WORKFLOW_JOBS = {
        "daily-forecast.yml": "forecast",
        "draw-alert-refresh.yml": "refresh",
        "noon-settlement.yml": "settlement",
        "email-report.yml": "email",
        "odds-snapshot.yml": "snapshot",
    }
    MAIN_OR_SCHEDULE_GUARD = (
        "if: github.event_name == 'schedule' || github.ref == 'refs/heads/main'"
    )
    LOCK_PATH_COMMAND = 'LOCK_PATH="output/plan_lock_${TARGET_DATE}.json"'
    VALID_LOCK_MESSAGE = (
        'echo "Valid plan lock exists for $TARGET_DATE; preserving locked plan and odds"'
    )
    INVALID_LOCK_MESSAGE = (
        'echo "Existing plan lock is invalid for $TARGET_DATE; refusing to rewrite locked artifacts" >&2'
    )

    def read_workflow(self, name):
        return (self.WORKFLOWS / name).read_text(encoding="utf-8")

    def job_block(self, text, job_name):
        lines = text.splitlines()
        marker = f"  {job_name}:"
        start = lines.index(marker)
        for end in range(start + 1, len(lines)):
            if lines[end].startswith("  ") and not lines[end].startswith("    "):
                return "\n".join(lines[start:end])
        return "\n".join(lines[start:])

    def step_block(self, text, job_name, step_name):
        lines = self.job_block(text, job_name).splitlines()
        marker = f"      - name: {step_name}"
        self.assertIn(marker, lines)
        start = lines.index(marker)
        for end in range(start + 1, len(lines)):
            if lines[end].startswith("      - name: "):
                return "\n".join(lines[start:end])
        return "\n".join(lines[start:])

    def assert_commands_in_order(self, text, commands):
        cursor = 0
        for command in commands:
            position = text.find(command, cursor)
            self.assertNotEqual(-1, position, f"missing ordered command: {command}")
            cursor = position + len(command)

    def multiline_run_bodies(self, text):
        return re.findall(
            r"^        run: \|\n(.*?)(?=^      - name: |\Z)",
            text,
            re.MULTILINE | re.DOTALL,
        )

    def multiline_step_body(self, text, job_name, step_name):
        step = self.step_block(text, job_name, step_name)
        marker = "        run: |\n"
        self.assertIn(marker, step)
        indented = step.split(marker, 1)[1]
        return "\n".join(
            line[10:] if line.startswith("          ") else line
            for line in indented.splitlines()
        )

    def bash_executable(self):
        candidates = (
            shutil.which("bash"),
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        )
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return candidate
        self.fail("bash is required to validate and execute workflow shell bodies")

    def run_workflow_body(self, body, root):
        env = os.environ.copy()
        env["REQUESTED_TARGET_DATE"] = "2026-07-16"
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        return subprocess.run(
            [self.bash_executable(), "-c", body],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def write_workflow_writer_stubs(self, root):
        shutil.copy2(ROOT / "plan_lock.py", root / "plan_lock.py")
        stub = '''import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
target_date = args[args.index("--date") + 1]
phase = args[args.index("--phase") + 1] if "--phase" in args else ""
with Path("writer-calls.log").open("a", encoding="utf-8") as handle:
    handle.write(name + (":" + phase if phase else "") + "\\n")

if name == "capture_odds_snapshot.py" and os.environ.get("FAIL_CAPTURE") == "true":
    raise SystemExit(7)

Path("data").mkdir(exist_ok=True)
Path("output").mkdir(exist_ok=True)
if name == "import_sporttery.py":
    Path(f"data/sporttery_odds_{target_date}.json").write_text(
        json.dumps({"writer": name}), encoding="utf-8"
    )
elif name == "capture_odds_snapshot.py":
    Path(f"data/sporttery_odds_{target_date}.json").write_text(
        json.dumps({"writer": name, "phase": phase}), encoding="utf-8"
    )
elif name == "generate_betting_plan.py":
    Path(f"output/betting_plan_{target_date}.csv").write_text(
        "date,plan\\n" + target_date + ",locked-candidate\\n", encoding="utf-8"
    )
'''
        for name in (
            "import_sporttery.py",
            "build_historical_features.py",
            "predict_today.py",
            "generate_betting_plan.py",
            "capture_odds_snapshot.py",
        ):
            (root / name).write_text(stub, encoding="utf-8")

    def writer_calls(self, root):
        path = root / "writer-calls.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def sha256_bytes(self, value):
        return hashlib.sha256(value).hexdigest()

    def test_step_block_scopes_duplicate_step_names_to_the_requested_job(self):
        workflow = """jobs:
  other:
    steps:
      - name: Refresh input
        continue-on-error: true
        run: python correct.py
  target:
    steps:
      - name: Refresh input
        run: python missing-isolation.py
"""

        target_step = self.step_block(workflow, "target", "Refresh input")

        self.assertIn("python missing-isolation.py", target_step)
        self.assertNotIn("continue-on-error: true", target_step)

    def test_beijing_schedule_crons_include_settlement_retry_and_snapshots(self):
        schedules = {
            "daily-forecast.yml": 'cron: "15 4 * * *"',
            "draw-alert-refresh.yml": 'cron: "30 5 * * *"',
            "noon-settlement.yml": 'cron: "45 5 * * *"',
            "email-report.yml": 'cron: "0 6 * * *"',
            "odds-snapshot.yml": 'cron: "*/30 * * * *"',
        }
        for name, cron in schedules.items():
            self.assertIn(cron, self.read_workflow(name))
        self.assertIn('cron: "5 6 * * *"', self.read_workflow("noon-settlement.yml"))

    def test_all_related_workflows_share_the_repository_queue(self):
        contract = "concurrency:\n  group: sporttery-repository\n  cancel-in-progress: false\n  queue: max"
        for name in self.WORKFLOW_JOBS:
            self.assertIn(contract, self.read_workflow(name))

    def test_jobs_only_run_for_schedules_or_main_and_checkout_latest_main(self):
        checkout_steps = {
            "daily-forecast.yml": "Checkout",
            "draw-alert-refresh.yml": "Checkout latest main report",
            "noon-settlement.yml": "Checkout",
            "email-report.yml": "Checkout latest report",
            "odds-snapshot.yml": "Checkout",
        }
        for workflow_name, job_name in self.WORKFLOW_JOBS.items():
            text = self.read_workflow(workflow_name)
            self.assertIn(self.MAIN_OR_SCHEDULE_GUARD, self.job_block(text, job_name))
            checkout = self.step_block(text, job_name, checkout_steps[workflow_name])
            self.assertIn("uses: actions/checkout@v4", checkout)
            self.assertIn("with:\n          ref: main", checkout)

    def test_report_workflows_define_optional_target_date_input(self):
        input_contract = """workflow_dispatch:
    inputs:
      target_date:
        description: Beijing business date (YYYY-MM-DD)
        required: false
        type: string"""
        for name in self.REPORT_WORKFLOWS:
            self.assertIn(input_contract, self.read_workflow(name))

    def test_target_date_inputs_use_step_env_bridge_not_bash_interpolation(self):
        env_bridge = (
            "        env:\n"
            "          REQUESTED_TARGET_DATE: ${{ inputs.target_date }}"
        )
        for name, (job_name, step_names) in self.TARGET_DATE_STEPS.items():
            text = self.read_workflow(name)
            for run_body in self.multiline_run_bodies(text):
                self.assertNotIn("inputs.target_date", run_body)
            for step_name in step_names:
                step = self.step_block(text, job_name, step_name)
                self.assertIn(env_bridge, step)
                self.assertIn('TARGET_DATE="$REQUESTED_TARGET_DATE"', step)
                self.assertIn('TARGET_DATE="${TARGET_DATE:-$(date +%F)}"', step)

    def test_all_multiline_workflow_shell_bodies_pass_bash_n(self):
        for workflow_path in sorted(self.WORKFLOWS.glob("*.yml")):
            text = workflow_path.read_text(encoding="utf-8")
            for index, body in enumerate(self.multiline_run_bodies(text), start=1):
                result = subprocess.run(
                    [self.bash_executable(), "-n"],
                    input=body,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    0,
                    result.returncode,
                    f"invalid shell in {workflow_path.name} body {index}: {result.stderr}",
                )

    def test_required_report_steps_validate_the_target_date_exactly(self):
        required_steps = {
            "daily-forecast.yml": (
                "forecast",
                "Generate required base forecast and plan",
            ),
            "draw-alert-refresh.yml": (
                "refresh",
                "Refresh required decision plan",
            ),
            "noon-settlement.yml": (
                "settlement",
                "Update results, settle ledgers, and train the draw model",
            ),
        }
        expected = [
            'TARGET_DATE="$REQUESTED_TARGET_DATE"',
            'TARGET_DATE="${TARGET_DATE:-$(date +%F)}"',
            'NORMALIZED_TARGET_DATE="$(date -d "$TARGET_DATE" +%F)"',
            'if [ "$NORMALIZED_TARGET_DATE" != "$TARGET_DATE" ]; then',
            "exit 1",
        ]
        for name, (job_name, step_name) in required_steps.items():
            text = self.read_workflow(name)
            self.assertIn("TZ: Asia/Shanghai", self.job_block(text, job_name))
            step = self.step_block(text, job_name, step_name)
            self.assert_commands_in_order(step, expected)

    def test_base_forecast_is_required_and_uses_beijing_target_date(self):
        text = self.read_workflow("daily-forecast.yml")
        self.assertIn("TZ: Asia/Shanghai", self.job_block(text, "forecast"))
        step = self.step_block(
            text, "forecast", "Generate required base forecast and plan"
        )
        expected = [
            'TARGET_DATE="$REQUESTED_TARGET_DATE"',
            'TARGET_DATE="${TARGET_DATE:-$(date +%F)}"',
            self.LOCK_PATH_COMMAND,
            'if [ -e "$LOCK_PATH" ]; then',
            'if python plan_lock.py is-locked --date "$TARGET_DATE"; then',
            self.VALID_LOCK_MESSAGE,
            "else",
            self.INVALID_LOCK_MESSAGE,
            "exit 1",
            "else",
            'python import_sporttery.py --date "$TARGET_DATE"',
            "python build_historical_features.py",
            'python predict_today.py --date "$TARGET_DATE"',
            'python generate_betting_plan.py --date "$TARGET_DATE"',
        ]
        self.assert_commands_in_order(step, expected)
        self.assertIn("id: base_forecast", step)
        self.assertNotIn("continue-on-error: true", step)
        self.assertNotIn("collect_market_heat.py", step)
        self.assertNotIn("generate_draw_alert.py", step)
        self.assertNotIn("draw_alert_ledger.py", step)
        self.assertNotIn("build_site.py", step)

    def test_base_forecast_optional_steps_fail_independently_and_report_still_builds(self):
        text = self.read_workflow("daily-forecast.yml")
        opening = self.step_block(text, "forecast", "Capture opening odds")
        self.assertNotIn("continue-on-error: true", opening)
        self.assert_commands_in_order(
            opening,
            [
                'TARGET_DATE="$REQUESTED_TARGET_DATE"',
                'TARGET_DATE="${TARGET_DATE:-$(date +%F)}"',
                self.LOCK_PATH_COMMAND,
                'if [ -e "$LOCK_PATH" ]; then',
                'if python plan_lock.py is-locked --date "$TARGET_DATE"; then',
                self.VALID_LOCK_MESSAGE,
                "else",
                self.INVALID_LOCK_MESSAGE,
                "exit 1",
                "else",
                'python capture_odds_snapshot.py --date "$TARGET_DATE" --phase opening',
            ],
        )
        self.assertIn(
            'if ! python capture_odds_snapshot.py --date "$TARGET_DATE" --phase opening; then',
            opening,
        )
        optional_steps = {
            "Collect optional market evidence": (
                'python collect_market_heat.py --date "$TARGET_DATE"',
                True,
            ),
            "Generate optional draw alerts": (
                'python generate_draw_alert.py --date "$TARGET_DATE"',
                True,
            ),
            "Update optional draw alert ledger": (
                "python draw_alert_ledger.py --settle",
                False,
            ),
        }
        positions = []
        for step_name, (command, uses_date) in optional_steps.items():
            step = self.step_block(text, "forecast", step_name)
            self.assertIn("continue-on-error: true", step)
            self.assertIn(command, step)
            if uses_date:
                self.assertIn('TARGET_DATE="$REQUESTED_TARGET_DATE"', step)
                self.assertIn('TARGET_DATE="${TARGET_DATE:-$(date +%F)}"', step)
            positions.append(text.index(f"      - name: {step_name}"))

        build = self.step_block(text, "forecast", "Build final website and image")
        self.assertIn(
            "if: always() && steps.base_forecast.outcome == 'success'", build
        )
        self.assertIn("python build_site.py", build)
        self.assertIn("python build_daily_image.py", build)
        positions.insert(0, text.index("      - name: Capture opening odds"))
        positions.append(text.index("      - name: Build final website and image"))
        self.assertEqual(positions, sorted(positions))

    def test_base_failure_cannot_reach_commit_or_pages_publication(self):
        text = self.read_workflow("daily-forecast.yml")
        publication_steps = (
            "Commit generated files",
            "Configure Pages",
            "Upload Pages artifact",
            "Deploy to GitHub Pages",
        )

        for step_name in publication_steps:
            step = self.step_block(text, "forecast", step_name)
            self.assertNotIn("if: always()", step)
            self.assertNotIn("continue-on-error: true", step)

        build_position = text.index("      - name: Build final website and image")
        publication_positions = [
            text.index(f"      - name: {step_name}") for step_name in publication_steps
        ]
        self.assertLess(build_position, publication_positions[0])
        self.assertEqual(publication_positions, sorted(publication_positions))

    def test_decision_refresh_requires_capture_generation_and_plan_locking(self):
        text = self.read_workflow("draw-alert-refresh.yml")
        step = self.step_block(text, "refresh", "Refresh required decision plan")
        expected = [
            self.LOCK_PATH_COMMAND,
            'if [ -e "$LOCK_PATH" ]; then',
            'if python plan_lock.py is-locked --date "$TARGET_DATE"; then',
            self.VALID_LOCK_MESSAGE,
            "else",
            self.INVALID_LOCK_MESSAGE,
            "exit 1",
            "else",
            'python import_sporttery.py --date "$TARGET_DATE"',
            'python capture_odds_snapshot.py --date "$TARGET_DATE" --phase decision',
            'python predict_today.py --date "$TARGET_DATE"',
            'python generate_betting_plan.py --date "$TARGET_DATE"',
            "python plan_lock.py lock \\",
            "--date \"$TARGET_DATE\" \\",
            "--locked-at \"$(date --iso-8601=seconds)\" \\",
            "--source sporttery",
            "fi",
        ]
        self.assert_commands_in_order(step, expected)
        self.assertNotIn("continue-on-error: true", step)

    def test_valid_decision_lock_survives_delayed_forecast_rerun_without_writers(self):
        refresh_body = self.multiline_step_body(
            self.read_workflow("draw-alert-refresh.yml"),
            "refresh",
            "Refresh required decision plan",
        )
        forecast_body = self.multiline_step_body(
            self.read_workflow("daily-forecast.yml"),
            "forecast",
            "Generate required base forecast and plan",
        )
        opening_body = self.multiline_step_body(
            self.read_workflow("daily-forecast.yml"),
            "forecast",
            "Capture opening odds",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_workflow_writer_stubs(root)

            refresh = self.run_workflow_body(refresh_body, root)
            self.assertEqual(0, refresh.returncode, refresh.stderr)
            expected_initial_calls = [
                "import_sporttery.py",
                "capture_odds_snapshot.py:decision",
                "predict_today.py",
                "generate_betting_plan.py",
            ]
            self.assertEqual(expected_initial_calls, self.writer_calls(root))

            plan_path = root / "output" / "betting_plan_2026-07-16.csv"
            odds_path = root / "data" / "sporttery_odds_2026-07-16.json"
            lock_path = root / "output" / "plan_lock_2026-07-16.json"
            plan_before = plan_path.read_bytes()
            odds_before = odds_path.read_bytes()
            lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(self.sha256_bytes(plan_before), lock_payload["plan_sha256"])
            self.assertEqual(self.sha256_bytes(odds_before), lock_payload["odds_sha256"])

            forecast = self.run_workflow_body(forecast_body, root)
            opening = self.run_workflow_body(opening_body, root)
            self.assertEqual(0, forecast.returncode, forecast.stderr)
            self.assertEqual(0, opening.returncode, opening.stderr)
            self.assertEqual(expected_initial_calls, self.writer_calls(root))
            self.assertEqual(plan_before, plan_path.read_bytes())
            self.assertEqual(odds_before, odds_path.read_bytes())

    def test_decision_capture_failure_stops_before_prediction_plan_and_lock(self):
        body = self.multiline_step_body(
            self.read_workflow("draw-alert-refresh.yml"),
            "refresh",
            "Refresh required decision plan",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_workflow_writer_stubs(root)
            with patch.dict(os.environ, {"FAIL_CAPTURE": "true"}):
                result = self.run_workflow_body(body, root)

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(
                ["import_sporttery.py", "capture_odds_snapshot.py:decision"],
                self.writer_calls(root),
            )
            self.assertFalse((root / "output" / "plan_lock_2026-07-16.json").exists())

    def test_decision_zero_fixture_capture_success_continues_through_plan_lock(self):
        body = self.multiline_step_body(
            self.read_workflow("draw-alert-refresh.yml"),
            "refresh",
            "Refresh required decision plan",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_workflow_writer_stubs(root)

            result = self.run_workflow_body(body, root)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                [
                    "import_sporttery.py",
                    "capture_odds_snapshot.py:decision",
                    "predict_today.py",
                    "generate_betting_plan.py",
                ],
                self.writer_calls(root),
            )
            self.assertTrue((root / "output" / "plan_lock_2026-07-16.json").is_file())

    def test_invalid_existing_lock_fails_both_plan_workflows_before_writers(self):
        guarded_steps = (
            (
                "daily-forecast.yml",
                "forecast",
                "Generate required base forecast and plan",
            ),
            (
                "draw-alert-refresh.yml",
                "refresh",
                "Refresh required decision plan",
            ),
        )
        for workflow_name, job_name, step_name in guarded_steps:
            with self.subTest(workflow=workflow_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.write_workflow_writer_stubs(root)
                (root / "output").mkdir(exist_ok=True)
                (root / "data").mkdir(exist_ok=True)
                plan_path = root / "output" / "betting_plan_2026-07-16.csv"
                odds_path = root / "data" / "sporttery_odds_2026-07-16.json"
                lock_path = root / "output" / "plan_lock_2026-07-16.json"
                plan_path.write_bytes(b"locked plan")
                odds_path.write_bytes(b"locked odds")
                lock_path.write_text("{}", encoding="utf-8")

                body = self.multiline_step_body(
                    self.read_workflow(workflow_name), job_name, step_name
                )
                result = self.run_workflow_body(body, root)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("Existing plan lock is invalid", result.stderr)
                self.assertEqual([], self.writer_calls(root))
                self.assertEqual(b"locked plan", plan_path.read_bytes())
                self.assertEqual(b"locked odds", odds_path.read_bytes())

    def test_decision_optional_refreshes_remain_isolated(self):
        text = self.read_workflow("draw-alert-refresh.yml")
        refresh_steps = {
            "Refresh optional market evidence": 'python collect_market_heat.py --date "$TARGET_DATE"',
            "Refresh draw alerts": 'python generate_draw_alert.py --date "$TARGET_DATE"',
        }
        step_positions = []
        for step_name, command in refresh_steps.items():
            step = self.step_block(text, "refresh", step_name)
            self.assertIn("continue-on-error: true", step)
            self.assertIn('TARGET_DATE="$REQUESTED_TARGET_DATE"', step)
            self.assertIn('TARGET_DATE="${TARGET_DATE:-$(date +%F)}"', step)
            self.assertIn(command, step)
            step_positions.append(text.index(f"      - name: {step_name}"))
        self.assertEqual(step_positions, sorted(step_positions))

        ledger_step = self.step_block(text, "refresh", "Refresh draw alert ledger")
        rebuild_step = self.step_block(text, "refresh", "Rebuild report from the latest committed data")
        self.assertIn("python draw_alert_ledger.py --settle", ledger_step)
        self.assertNotIn("continue-on-error: true", ledger_step)
        self.assertIn("python build_site.py", rebuild_step)
        self.assertIn("python build_daily_image.py", rebuild_step)
        self.assertLess(
            text.index("      - name: Refresh draw alert ledger"),
            text.index("      - name: Rebuild report from the latest committed data"),
        )

    def test_recurring_snapshot_marks_monitoring_phase(self):
        text = self.read_workflow("odds-snapshot.yml")
        step = self.step_block(text, "snapshot", "Capture official odds snapshot")
        self.assertIn('TARGET_DATE="$(date +%F)"', step)
        self.assertIn(
            'python capture_odds_snapshot.py --date "$TARGET_DATE" --phase monitoring',
            step,
        )
        commit = self.step_block(text, "snapshot", "Commit snapshot")
        self.assertIn('file_pattern: "data/odds_snapshots"', commit)
        self.assertNotIn("*.json", commit)

    def test_settlement_uses_yesterday_for_results_and_today_for_training(self):
        text = self.read_workflow("noon-settlement.yml")
        step = self.step_block(text, "settlement", "Update results, settle ledgers, and train the draw model")
        expected = [
            'TARGET_DATE="$REQUESTED_TARGET_DATE"',
            'TARGET_DATE="${TARGET_DATE:-$(date +%F)}"',
            'TODAY="$TARGET_DATE"',
            'SETTLEMENT_DATE="$(date -d "$TODAY - 1 day" +%F)"',
            'python update_sporttery_results.py --date "$SETTLEMENT_DATE"',
            "python build_historical_features.py",
            "python generate_betting_plan.py --settle-only",
            "python draw_alert_ledger.py --settle",
            'python draw_model_learning.py --train --date "$TODAY"',
            "python build_site.py",
            "python build_daily_image.py",
        ]
        self.assert_commands_in_order(step, expected)
        self.assertNotIn('python update_sporttery_results.py --date "$TODAY"', step)

    def test_phased_status_is_published_after_both_builders_and_before_publication(self):
        for name, (
            job_name,
            build_step_name,
            commit_step_name,
            phase,
            report_date,
        ) in self.REPORT_WORKFLOWS.items():
            text = self.read_workflow(name)
            build = self.step_block(text, job_name, build_step_name)
            expected_status = (
                f'python report_status.py --date "{report_date}" --phase {phase}'
            )
            if phase == "settlement":
                expected_status += ' --settled-through "$SETTLEMENT_DATE"'

            build_id = (
                f'REPORT_BUILD_ID="${{GITHUB_RUN_ID}}-'
                f'${{GITHUB_RUN_ATTEMPT}}-{phase}"'
            )
            expected = [
                build_id,
                'export REPORT_BUILD_ID',
                "python build_site.py",
                "python build_daily_image.py",
                'SOURCE_COMMIT_SHA="$(git rev-parse HEAD)"',
                'GENERATED_AT_SHANGHAI="$(date --iso-8601=seconds)"',
                expected_status,
                '--build-id "$REPORT_BUILD_ID"',
                '--source-commit "$SOURCE_COMMIT_SHA"',
                '--generated-at "$GENERATED_AT_SHANGHAI"',
            ]
            self.assert_commands_in_order(build, expected)
            self.assertNotIn("continue-on-error: true", build)
            self.assertLess(
                text.index(expected_status),
                text.index(f"      - name: {commit_step_name}"),
            )

    def test_report_workflows_do_not_dispatch_email_report(self):
        for name in self.REPORT_WORKFLOWS:
            self.assertNotIn("email-report.yml", self.read_workflow(name))

    def test_base_refresh_and_settlement_install_learning_and_image_dependencies(self):
        jobs = {
            "daily-forecast.yml": "forecast",
            "draw-alert-refresh.yml": "refresh",
            "noon-settlement.yml": "settlement",
        }
        for name, job_name in jobs.items():
            step = self.step_block(
                self.read_workflow(name), job_name, "Install learning dependencies and image fonts"
            )
            self.assertIn("python -m pip install --quiet -r requirements.txt", step)
            self.assertIn("python -m pip install --quiet pillow", step)
            self.assertIn("fonts-noto-cjk", step)

    def test_generated_file_commits_do_not_require_optional_outputs(self):
        commit_steps = {
            "daily-forecast.yml": ("forecast", "Commit generated files"),
            "draw-alert-refresh.yml": ("refresh", "Commit refreshed files"),
            "noon-settlement.yml": ("settlement", "Commit settlement files"),
        }
        for name, (job_name, step_name) in commit_steps.items():
            step = self.step_block(self.read_workflow(name), job_name, step_name)
            self.assertIn('file_pattern: "data output web"', step)
            self.assertNotIn("*", step)

    def test_email_uses_the_queue_latest_main_checkout_and_secret_only_credentials(self):
        text = self.read_workflow("email-report.yml")
        job = self.job_block(text, "email")
        self.assertIn("TZ: Asia/Shanghai", job)
        self.assertIn("python send_daily_email.py", self.step_block(text, "email", "Send report image"))
        password = re.search(r"^      GMAIL_APP_PASSWORD: (.+)$", job, re.MULTILINE)
        self.assertIsNotNone(password)
        self.assertEqual("${{ secrets.GMAIL_APP_PASSWORD }}", password.group(1))
        self.assertNotIn("requirements.txt", job)
        self.assertNotIn("pillow", job.lower())


class DeploymentDocumentationTest(unittest.TestCase):
    APPS_SCRIPT_README = ROOT / "apps-script" / "README.md"
    CODE_PATH = ROOT / "apps-script" / "Code.gs"
    MANIFEST_PATH = ROOT / "apps-script" / "appsscript.json"
    OPERATOR_DOCS = (
        ROOT / "README.md",
        ROOT / "CLOUD_SETUP.md",
        APPS_SCRIPT_README,
    )
    REQUIRED_CONFIG_PROPERTIES = (
        "GITHUB_OWNER",
        "GITHUB_REPO",
        "GITHUB_TOKEN",
        "REPORT_STATUS_URL",
        "REPORT_IMAGE_URL",
        "REPORT_SITE_URL",
        "RECIPIENT_EMAIL",
    )

    def read_doc(self, path):
        self.assertTrue(path.exists(), f"missing deployment document: {path}")
        return path.read_text(encoding="utf-8")

    def assert_text_in_order(self, text, required_text):
        cursor = 0
        for item in required_text:
            position = text.find(item, cursor)
            self.assertNotEqual(-1, position, f"missing ordered documentation text: {item}")
            cursor = position + len(item)

    def function_body(self, text, name):
        match = re.search(
            rf"^function {re.escape(name)}\([^)]*\) \{{(?P<body>.*?)(?=^function |\Z)",
            text,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match, f"missing Apps Script function: {name}")
        return match.group("body")

    def test_apps_script_readme_defines_the_sender_schedule_and_safety_contract(self):
        text = self.read_doc(self.APPS_SCRIPT_README)
        for literal in (
            "Apps Script 是唯一的邮件发送方",
            "`runAutomation` 每 10 分钟运行一次",
            "`Asia/Shanghai`",
            "14:00-18:00",
            "18:00",
            "不附带附件",
            "电脑可以关机",
            "仅用于概率分析和模拟记账",
            "不保证盈利",
        ):
            self.assertIn(literal, text)

    def test_apps_script_readme_lists_exactly_seven_manual_config_properties(self):
        text = self.read_doc(self.APPS_SCRIPT_README)
        code = self.read_doc(self.CODE_PATH)
        section = re.search(
            r"^### 必须手工配置的 7 项 Script Properties\n(?P<body>.*?)(?=^### )",
            text,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(section)
        property_names = re.findall(
            r"^- `([A-Z][A-Z0-9_]+)`：", section.group("body"), re.MULTILINE
        )
        self.assertEqual(list(self.REQUIRED_CONFIG_PROPERTIES), property_names)
        code_properties = set(re.findall(r'requiredProperty_\(properties, "([A-Z0-9_]+)"\)', code))
        self.assertEqual(set(self.REQUIRED_CONFIG_PROPERTIES), code_properties)
        self.assertIn("运行状态属性由 `Code.gs` 自动写入", text)
        self.assertIn("`TEST_MODE` 是临时部署开关，不计入上述 7 项必填配置", text)

    def test_docs_match_manifest_timezone_workflow_crons_and_pages_artifact_root(self):
        apps_readme = self.read_doc(self.APPS_SCRIPT_README)
        cloud_setup = self.read_doc(ROOT / "CLOUD_SETUP.md")
        combined = "\n".join(self.read_doc(path) for path in self.OPERATOR_DOCS)
        manifest = json.loads(self.read_doc(self.MANIFEST_PATH))
        self.assertIn(f"`{manifest['timeZone']}`", combined)

        scheduled_workflows = (
            "daily-forecast.yml",
            "draw-alert-refresh.yml",
            "noon-settlement.yml",
            "odds-snapshot.yml",
            "email-report.yml",
        )
        for workflow_name in scheduled_workflows:
            workflow = self.read_doc(ROOT / ".github" / "workflows" / workflow_name)
            for cron in re.findall(r'cron: "([^"]+)"', workflow):
                self.assertIn(f"`{cron}`", cloud_setup)

        for workflow_name in (
            "daily-forecast.yml",
            "draw-alert-refresh.yml",
            "noon-settlement.yml",
        ):
            workflow = self.read_doc(ROOT / ".github" / "workflows" / workflow_name)
            self.assertRegex(
                workflow,
                r"uses: actions/upload-pages-artifact@v3\s+with:\s+path: web",
            )

        for text in (apps_readme, cloud_setup):
            self.assertIn("仓库路径 `web/report-status.json`", text)
            self.assertIn(
                "`https://l18381527760-sketch.github.io/sporttery-prediction/report-status.json`",
                text,
            )
            self.assertIn(
                "`https://l18381527760-sketch.github.io/sporttery-prediction/daily-report.png`",
                text,
            )
            self.assertIn(
                "`https://l18381527760-sketch.github.io/sporttery-prediction/`",
                text,
            )
        self.assertNotRegex(combined, r"https?://[^\s`)]+/web/report-status\.json")
        self.assertNotRegex(combined, r"https?://[^\s`)]+/web/daily-report\.png")

    def test_docs_describe_crons_and_apps_script_dispatch_as_independent(self):
        combined = "\n".join(self.read_doc(path) for path in self.OPERATOR_DOCS)
        for literal in (
            "cron 定时运行与 Apps Script dispatch 彼此独立",
            "Pages 更新前",
            "同一阶段",
            "额外的排队运行",
            "共享并发队列",
            "有效的方案锁",
            "同日状态更新具备幂等性",
        ):
            self.assertIn(literal, combined)
        for misleading in ("cron 后备", "后备触发", "cron fallback", "补调度"):
            self.assertNotIn(misleading, combined)

    def test_docs_define_the_fail_closed_prewrite_plan_lock_guard(self):
        apps_readme = self.read_doc(self.APPS_SCRIPT_README)
        cloud_setup = self.read_doc(ROOT / "CLOUD_SETUP.md")
        for text in (apps_readme, cloud_setup):
            for literal in (
                "`output/plan_lock_${TARGET_DATE}.json`",
                "在任何 plan/odds writer 之前",
                "`import_sporttery.py`",
                "`predict_today.py`",
                "`generate_betting_plan.py`",
                "有效锁",
                "跳过全部 plan/odds writer",
                "原有方案与赔率字节保持不变",
                "锁文件存在但 `plan_lock.py is-locked` 校验失败",
                "立即失败",
                "不能把无效锁当成没有锁",
            ):
                self.assertIn(literal, text)

    def test_test_mode_and_gmail_recovery_docs_match_mail_state_code(self):
        text = self.read_doc(self.APPS_SCRIPT_README)
        code = self.read_doc(self.CODE_PATH)
        for literal in (
            "同一天安全试运行",
            "不会写入 `LAST_SENT_DATE`",
            "不会写入 `LAST_SENT_IMAGE_SHA256`",
            "不会写入 `LAST_FAILURE_NOTICE_DATE`",
            "不测试 Gmail 实际投递",
            "生产发送失败不会写入发送状态",
            "修复原因后",
            "`TEST_MODE=false`",
            "受控重试",
        ):
            self.assertIn(literal, text)

        normal = self.function_body(code, "sendNormalReport_")
        failure = self.function_body(code, "sendFailureNotice_")
        self.assertLess(
            normal.index("GmailApp.sendEmail"),
            normal.index('setProperty("LAST_SENT_DATE"'),
        )
        self.assertLess(
            failure.index("GmailApp.sendEmail"),
            failure.index('setProperty("LAST_FAILURE_NOTICE_DATE"'),
        )
        normal_test_mode = re.search(
            r'if \(properties\.getProperty\("TEST_MODE"\) === "true"\) \{(?P<body>.*?)\} else',
            normal,
            re.DOTALL,
        )
        failure_test_mode = re.search(
            r'if \(properties\.getProperty\("TEST_MODE"\) === "true"\) \{(?P<body>.*?)\} else',
            failure,
            re.DOTALL,
        )
        self.assertIsNotNone(normal_test_mode)
        self.assertIsNotNone(failure_test_mode)
        self.assertNotIn("setProperty", normal_test_mode.group("body"))
        self.assertNotIn("setProperty", failure_test_mode.group("body"))

    def test_edited_docs_and_apps_script_contain_no_token_shaped_secrets(self):
        sensitive_files = self.OPERATOR_DOCS + (self.CODE_PATH,)
        secret_patterns = (
            r"\bghp_[A-Za-z0-9]{20,}\b",
            r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
            r"\bgho_[A-Za-z0-9]{20,}\b",
            r"\bghu_[A-Za-z0-9]{20,}\b",
            r"\bghs_[A-Za-z0-9]{20,}\b",
            r"\bghr_[A-Za-z0-9]{20,}\b",
        )
        for path in sensitive_files:
            text = self.read_doc(path)
            for pattern in secret_patterns:
                self.assertNotRegex(text, pattern, f"token-shaped secret in {path}")

    def test_apps_script_readme_documents_least_privilege_token_and_deployment_order(self):
        text = self.read_doc(self.APPS_SCRIPT_README)
        for literal in (
            "`l18381527760-sketch/sporttery-prediction`",
            "Metadata: Read-only",
            "Actions: Read and write",
            "不得填写真实令牌或密钥",
        ):
            self.assertIn(literal, text)
        self.assert_text_in_order(
            text,
            (
                "打开现有 Apps Script 项目",
                "`apps-script/Code.gs`",
                "`apps-script/appsscript.json`",
                "配置上述 7 项 Script Properties",
                "`TEST_MODE=true`",
                "手动运行 `runAutomation`",
                "批准权限",
                "运行 `installAutomationTrigger`",
                "恰好一个每 10 分钟运行的 `runAutomation` 触发器",
                "`workflow_dispatch`",
                "当天北京时间日期",
                "`web/report-status.json`",
                "PNG 的 SHA-256",
                "`TEST_MODE=false`",
                "`.github/workflows/email-report.yml`",
                "GitHub Actions",
                "disabled",
            ),
        )

    def test_apps_script_readme_covers_recovery_and_safe_rollback(self):
        text = self.read_doc(self.APPS_SCRIPT_README)
        for literal in (
            "令牌被撤销",
            "重复触发器",
            "状态或哈希不匹配",
            "Gmail 发送失败",
        ):
            self.assertIn(literal, text)
        self.assert_text_in_order(
            text,
            (
                "回滚到旧的每日触发器",
                "先禁用并删除 `runAutomation` 触发器",
                "再恢复之前唯一的每日 `sendDailyReport` 触发器",
            ),
        )

    def test_top_level_docs_replace_the_old_github_email_sender_description(self):
        readme = self.read_doc(ROOT / "README.md")
        cloud_setup = self.read_doc(ROOT / "CLOUD_SETUP.md")
        combined = readme + "\n" + cloud_setup
        for literal in (
            "Apps Script 是唯一的邮件发送方",
            "`runAutomation`",
            "每 10 分钟",
            "14:00-18:00",
            "电脑可以关机",
            "`.github/workflows/email-report.yml`",
            "保持 disabled",
        ):
            self.assertIn(literal, combined)
        self.assertNotIn("| 14:00 | Gmail 发送", combined)
        self.assertNotIn('`0 6 * * *`：14:00 邮件日报', combined)

    def test_operator_docs_include_the_required_verification_commands(self):
        text = self.read_doc(self.APPS_SCRIPT_README)
        for command in (
            "python -m unittest tests.test_workflow_schedule -v",
            "python -m unittest discover -s tests -v",
            "node --test tests/apps_script_orchestrator.test.mjs",
        ):
            self.assertIn(command, text)


if __name__ == "__main__":
    unittest.main()
