import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowScheduleTest(unittest.TestCase):
    WORKFLOWS = ROOT / ".github" / "workflows"
    CONCURRENCY = (
        "concurrency:\n  group: sporttery-repository\n"
        "  cancel-in-progress: false\n  queue: max"
    )
    WORKFLOW_JOBS = {
        "daily-forecast.yml": ("forecast", "Checkout"),
        "draw-alert-refresh.yml": ("refresh", "Checkout latest main report"),
        "pre-kickoff-revalidation.yml": ("revalidate", "Checkout latest main report"),
        "odds-snapshot.yml": ("snapshot", "Checkout"),
        "noon-settlement.yml": ("settlement", "Checkout"),
        "email-report.yml": ("email", "Checkout latest report"),
    }

    def read_workflow(self, name):
        return (self.WORKFLOWS / name).read_text(encoding="utf-8")

    def job_block(self, text, job_name):
        lines = text.splitlines()
        start = lines.index(f"  {job_name}:")
        for end in range(start + 1, len(lines)):
            if lines[end].startswith("  ") and not lines[end].startswith("    "):
                return "\n".join(lines[start:end])
        return "\n".join(lines[start:])

    def step_block(self, text, job_name, step_name):
        lines = self.job_block(text, job_name).splitlines()
        start = lines.index(f"      - name: {step_name}")
        for end in range(start + 1, len(lines)):
            if lines[end].startswith("      - name: "):
                return "\n".join(lines[start:end])
        return "\n".join(lines[start:])

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
        return "\n".join(
            line[10:] if line.startswith("          ") else line
            for line in step.split(marker, 1)[1].splitlines()
        )

    def run_bash_body(self, body, root, **environment):
        env = os.environ.copy()
        env.update(environment)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get(
            "PATH", ""
        )
        return subprocess.run(
            [self.bash_executable(), "-c", "set -eo pipefail\n" + body],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def bash_executable(self):
        for candidate in (
            shutil.which("bash"),
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        ):
            if candidate and Path(candidate).is_file():
                return candidate
        self.fail("bash is required to validate workflow shell bodies")

    def test_phase_schedules_use_beijing_mapped_crons(self):
        expected = {
            "daily-forecast.yml": 'cron: "15 4 * * *"',
            "draw-alert-refresh.yml": 'cron: "30 5 * * *"',
            "pre-kickoff-revalidation.yml": 'cron: "*/10 * * * *"',
            "odds-snapshot.yml": 'cron: "*/30 * * * *"',
            "noon-settlement.yml": 'cron: "45 5 * * *"',
        }
        for name, cron in expected.items():
            self.assertIn(cron, self.read_workflow(name))
        self.assertIn('cron: "5 6 * * *"', self.read_workflow("noon-settlement.yml"))

    def test_all_workflows_share_latest_main_and_beijing_queue_contract(self):
        for name, (job_name, checkout_name) in self.WORKFLOW_JOBS.items():
            text = self.read_workflow(name)
            self.assertIn(self.CONCURRENCY, text)
            job = self.job_block(text, job_name)
            self.assertIn("TZ: Asia/Shanghai", job)
            checkout = self.step_block(text, job_name, checkout_name)
            self.assertIn("uses: actions/checkout@v4", checkout)
            self.assertIn("with:\n          ref: main", checkout)
        for name, (job_name, _) in self.WORKFLOW_JOBS.items():
            job = self.job_block(self.read_workflow(name), job_name)
            if name == "email-report.yml":
                self.assertIn("github.event_name == 'workflow_dispatch'", job)
            else:
                self.assertIn("github.event_name == 'schedule' || github.ref == 'refs/heads/main'", job)

    def test_required_steps_validate_exact_target_dates_through_step_environment(self):
        required = (
            ("daily-forecast.yml", "forecast", "Generate required daily forecast"),
            (
                "draw-alert-refresh.yml",
                "refresh",
                "Create provisional decision generation",
            ),
            (
                "noon-settlement.yml",
                "settlement",
                "Update results and settle confirmed canonical rows",
            ),
        )
        expected = (
            'TARGET_DATE="$REQUESTED_TARGET_DATE"',
            'TARGET_DATE="${TARGET_DATE:-$(date +%F)}"',
            'NORMALIZED_TARGET_DATE="$(date -d "$TARGET_DATE" +%F)"',
            'if [ "$NORMALIZED_TARGET_DATE" != "$TARGET_DATE" ]; then',
            "exit 1",
        )
        for workflow, job, step_name in required:
            with self.subTest(workflow=workflow):
                text = self.read_workflow(workflow)
                step = self.step_block(text, job, step_name)
                self.assertIn(
                    "REQUESTED_TARGET_DATE: ${{ inputs.target_date }}", step
                )
                cursor = 0
                for value in expected:
                    cursor = step.find(value, cursor)
                    self.assertNotEqual(-1, cursor, value)
                    cursor += len(value)
                run_body = self.multiline_step_body(text, job, step_name)
                self.assertNotIn("${{ inputs.target_date }}", run_body)

        revalidation = self.step_block(
            self.read_workflow("pre-kickoff-revalidation.yml"),
            "revalidate",
            "Run due revalidations",
        )
        self.assertIn('NORMALIZED_TARGET_DATE="$(date -d "$TARGET_DATE" +%F)"', revalidation)
        self.assertIn('if [ "$NORMALIZED_TARGET_DATE" != "$TARGET_DATE" ]; then', revalidation)

    def test_daily_optional_failures_are_isolated_and_required_publication_stays_gated(self):
        text = self.read_workflow("daily-forecast.yml")
        optional = (
            ("Collect optional market evidence", "collect_market_heat.py"),
            ("Generate optional draw alerts", "generate_draw_alert.py"),
        )
        for step_name, command in optional:
            step = self.step_block(text, "forecast", step_name)
            self.assertIn("continue-on-error: true", step)
            self.assertIn(command, step)
        build = self.step_block(text, "forecast", "Build base website and image")
        self.assertIn("if: always() && steps.base_forecast.outcome == 'success'", build)
        self.assertNotIn("continue-on-error: true", build)
        for step_name in (
            "Commit generated files",
            "Configure Pages",
            "Upload Pages artifact",
            "Deploy to GitHub Pages",
        ):
            step = self.step_block(text, "forecast", step_name)
            self.assertNotIn("if: always()", step)
            self.assertNotIn("continue-on-error: true", step)

    def test_status_publication_precedes_changed_only_commit_and_pages_deploy(self):
        workflows = (
            (
                "daily-forecast.yml",
                "report_status.py --date \"$TARGET_DATE\" --phase forecast",
                "Commit generated files",
            ),
            (
                "draw-alert-refresh.yml",
                "report_status.py --date \"$TARGET_DATE\" --phase provisional",
                "Commit provisional files",
            ),
            (
                "pre-kickoff-revalidation.yml",
                "report_status.py --date \"$DISPLAY_DATE\" --phase provisional",
                "Commit changed revalidation files",
            ),
            (
                "noon-settlement.yml",
                "report_status.py --date \"$TODAY\" --phase settlement",
                "Commit settlement files",
            ),
        )
        for workflow, status_command, commit_name in workflows:
            with self.subTest(workflow=workflow):
                text = self.read_workflow(workflow)
                status = text.index(status_command)
                commit = text.index(f"      - name: {commit_name}")
                upload = text.index("      - name: Upload Pages artifact")
                deploy = text.index("      - name: Deploy to GitHub Pages")
                self.assertLess(status, commit)
                self.assertLess(commit, upload)
                self.assertLess(upload, deploy)
                commit_step = text[commit:upload]
                self.assertIn('file_pattern: "data output web"', commit_step)
                self.assertNotIn("*", commit_step)

    def test_settlement_uses_prior_date_for_results_and_target_date_for_training(self):
        step = self.step_block(
            self.read_workflow("noon-settlement.yml"),
            "settlement",
            "Update results and settle confirmed canonical rows",
        )
        expected = (
            'TODAY="$TARGET_DATE"',
            'SETTLEMENT_DATE="$(date -d "$TODAY - 1 day" +%F)"',
            'python update_sporttery_results.py --date "$SETTLEMENT_DATE"',
            "python generate_betting_plan.py --settle-only",
            'python draw_model_learning.py --train --date "$TODAY"',
            'python build_site.py --date "$TODAY" --stage settlement',
            'python build_daily_image.py --date "$TODAY" --stage settlement',
        )
        cursor = 0
        for command in expected:
            cursor = step.find(command, cursor)
            self.assertNotEqual(-1, cursor, command)
            cursor += len(command)
        self.assertNotIn('update_sporttery_results.py --date "$TODAY"', step)

    def test_report_workflows_install_required_dependencies_and_do_not_dispatch_email(self):
        for workflow, job in (
            ("daily-forecast.yml", "forecast"),
            ("draw-alert-refresh.yml", "refresh"),
            ("pre-kickoff-revalidation.yml", "revalidate"),
            ("noon-settlement.yml", "settlement"),
        ):
            with self.subTest(workflow=workflow):
                text = self.read_workflow(workflow)
                install_name = (
                    "Install reporting dependencies and image fonts"
                    if workflow == "pre-kickoff-revalidation.yml"
                    else "Install learning dependencies and image fonts"
                )
                install = self.step_block(text, job, install_name)
                self.assertIn("python -m pip install --quiet -r requirements.txt", install)
                self.assertIn("python -m pip install --quiet pillow", install)
                self.assertIn("fonts-noto-cjk", install)
                self.assertNotIn("email-report.yml", text)

    def test_daily_forecast_is_prediction_only(self):
        text = self.read_workflow("daily-forecast.yml")
        generation = self.step_block(text, "forecast", "Generate required daily forecast")
        self.assertIn('python import_sporttery.py --date "$TARGET_DATE"', generation)
        self.assertIn("python build_historical_features.py", generation)
        self.assertIn('python predict_today.py --date "$TARGET_DATE"', generation)
        self.assertNotIn("generate_betting_plan.py", generation)
        self.assertNotIn("provisional_plan.py", generation)
        self.assertNotIn("decision_bundle.py", generation)
        self.assertNotIn("plan_lock.py", text)
        self.assertNotIn("betting_ledger.py ingest", text)

    def test_refresh_captures_live_decision_snapshot_and_publishes_provisional_generation(self):
        text = self.read_workflow("draw-alert-refresh.yml")
        refresh = self.step_block(text, "refresh", "Create provisional decision generation")
        expected = [
            'python import_sporttery.py --date "$TARGET_DATE"',
            'LIVE_PATH="$(python capture_odds_snapshot.py --date "$TARGET_DATE" --phase decision --live --print-path)"',
            'python predict_today.py --date "$TARGET_DATE"',
            'PROVISIONAL_AT_BJT="$(date --iso-8601=ns)"',
            'python decision_bundle.py --date "$TARGET_DATE" --locked-at "$PROVISIONAL_AT_BJT" --decision-snapshot "$LIVE_PATH"',
            'python provisional_plan.py --date "$TARGET_DATE" --generated-at "$PROVISIONAL_AT_BJT"',
        ]
        cursor = 0
        for command in expected:
            cursor = refresh.find(command, cursor)
            self.assertNotEqual(-1, cursor, command)
            cursor += len(command)
        self.assertNotIn("plan_lock", refresh)
        self.assertNotIn("betting_ledger.py ingest", refresh)
        self.assertNotIn("generate_betting_plan.py", refresh)
        self.assertIn("report_status.py --date \"$TARGET_DATE\" --phase provisional", text)

    def test_report_builders_receive_one_explicit_date_and_stage(self):
        expected = (
            (
                "daily-forecast.yml",
                "forecast",
                "Build base website and image",
                "$TARGET_DATE",
                "forecast",
            ),
            (
                "draw-alert-refresh.yml",
                "refresh",
                "Publish provisional website and status",
                "$TARGET_DATE",
                "provisional",
            ),
            (
                "noon-settlement.yml",
                "settlement",
                "Update results and settle confirmed canonical rows",
                "$TODAY",
                "settlement",
            ),
        )
        for workflow, job, step_name, report_date, stage in expected:
            with self.subTest(workflow=workflow):
                step = self.step_block(self.read_workflow(workflow), job, step_name)
                self.assertEqual(
                    1,
                    step.count(
                        f'python build_site.py --date "{report_date}" --stage {stage}'
                    ),
                )
                self.assertEqual(
                    1,
                    step.count(
                        f'python build_daily_image.py --date "{report_date}" --stage {stage}'
                    ),
                )

    def test_revalidation_is_manual_rehearsable_and_publishes_only_changes(self):
        text = self.read_workflow("pre-kickoff-revalidation.yml")
        self.assertIn('cron: "*/10 * * * *"', text)
        self.assertIn("target_date:", text)
        self.assertIn("now_bjt:", text)
        step = self.step_block(text, "revalidate", "Run due revalidations")
        self.assertIn("python revalidation.py run-due", step)
        self.assertIn("--target-date \"$TARGET_DATE\"", step)
        self.assertIn("--now-bjt \"$NOW_BJT\"", step)
        self.assertIn("changed_dates", step)
        report = self.step_block(text, "revalidate", "Rebuild changed reports")
        self.assertIn("python revalidation_reporting.py", report)
        self.assertIn("python build_site.py", report)
        self.assertIn("python build_daily_image.py", report)
        self.assertIn("python report_status.py", report)
        self.assertIn("if: steps.due.outputs.changed == 'true'", report)
        commit = self.step_block(text, "revalidate", "Commit changed revalidation files")
        self.assertIn("if: steps.due.outputs.changed == 'true'", commit)
        self.assertIn('file_pattern: "data output web"', commit)
        deploy = self.step_block(text, "revalidate", "Deploy to GitHub Pages")
        self.assertIn("if: steps.due.outputs.changed == 'true'", deploy)
        self.assertLess(text.index("Rebuild changed reports"), text.index("Deploy to GitHub Pages"))

    def test_revalidation_rebuilds_global_assets_once_for_the_selected_display_date(self):
        text = self.read_workflow("pre-kickoff-revalidation.yml")
        due = self.step_block(text, "revalidate", "Run due revalidations")
        self.assertIn('echo "display_date=$DISPLAY_DATE" >> "$GITHUB_OUTPUT"', due)
        rebuild = self.step_block(text, "revalidate", "Rebuild changed reports")
        self.assertIn("DISPLAY_DATE: ${{ steps.due.outputs.display_date }}", rebuild)
        self.assertEqual(
            1,
            rebuild.count(
                'python build_site.py --date "$DISPLAY_DATE" --stage provisional'
            ),
        )
        self.assertEqual(
            1,
            rebuild.count(
                'python build_daily_image.py --date "$DISPLAY_DATE" --stage provisional'
            ),
        )
        self.assertEqual(
            1,
            rebuild.count(
                'python report_status.py --date "$DISPLAY_DATE" --phase provisional'
            ),
        )
        loop = re.search(r"for TARGET_DATE.*?done", rebuild, re.DOTALL)
        self.assertIsNotNone(loop)
        self.assertNotIn("build_site.py", loop.group(0))
        self.assertNotIn("build_daily_image.py", loop.group(0))
        self.assertNotIn("report_status.py", loop.group(0))

    def test_monitoring_is_live_observation_only(self):
        text = self.read_workflow("odds-snapshot.yml")
        capture = self.step_block(text, "snapshot", "Capture live monitoring snapshot")
        self.assertIn('python capture_odds_snapshot.py --date "$TARGET_DATE" --phase monitoring --live', capture)
        self.assertNotIn("decision_bundle.py", text)
        self.assertNotIn("provisional_plan.py", text)
        self.assertNotIn("plan_lock.py", text)
        self.assertNotIn("betting_ledger.py", text)

    def test_settlement_processes_only_confirmed_canonical_rows(self):
        text = self.read_workflow("noon-settlement.yml")
        settlement = self.step_block(text, "settlement", "Update results and settle confirmed canonical rows")
        self.assertIn("python generate_betting_plan.py --settle-only", settlement)
        self.assertIn("only path that ingests confirmed rows", settlement)
        self.assertNotIn("draw_alert_ledger.py", settlement)

    def test_email_is_manual_diagnostic_only(self):
        text = self.read_workflow("email-report.yml")
        trigger = text.split("permissions:", 1)[0]
        self.assertNotIn("schedule:", trigger)
        job = self.job_block(text, "email")
        self.assertIn('ALLOW_MANUAL_EMAIL_DIAGNOSTIC == "true"', job)
        self.assertIn("python send_daily_email.py", text)

    def test_all_multiline_workflow_shell_bodies_pass_bash_n(self):
        for workflow_path in sorted(self.WORKFLOWS.glob("*.yml")):
            for index, body in enumerate(self.multiline_run_bodies(workflow_path.read_text(encoding="utf-8")), start=1):
                result = subprocess.run(
                    [self.bash_executable(), "-n"], input=body, text=True,
                    capture_output=True, check=False,
                )
                self.assertEqual(0, result.returncode, f"{workflow_path.name} body {index}: {result.stderr}")

    def test_run_due_cli_emits_machine_readable_changed_dates(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "revalidation.py"),
                "run-due",
                "--target-date", "2026-07-16",
                "--now-bjt", "2026-07-16T12:00:00+08:00",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"changed_dates": []}, json.loads(result.stdout))

    def test_exact_date_clis_reject_compact_and_malformed_dates_with_exit_two(self):
        commands = (
            (
                "provisional_plan.py",
                lambda value: [
                    "--date",
                    value,
                    "--generated-at",
                    "2026-07-16T13:30:00+08:00",
                ],
            ),
            (
                "revalidation.py",
                lambda value: [
                    "run-due",
                    "--target-date",
                    value,
                    "--now-bjt",
                    "2026-07-16T13:30:00+08:00",
                ],
            ),
        )
        for script, arguments in commands:
            for invalid in ("20260716", "2026-7-16", "not-a-date"):
                with self.subTest(script=script, invalid=invalid):
                    result = subprocess.run(
                        [sys.executable, str(ROOT / script), *arguments(invalid)],
                        cwd=ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(2, result.returncode, result.stderr)

    def test_live_refresh_executes_with_the_exact_printed_snapshot_path(self):
        body = self.multiline_step_body(
            self.read_workflow("draw-alert-refresh.yml"),
            "refresh",
            "Create provisional decision generation",
        )
        stub = '''import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with Path("calls.log").open("a", encoding="utf-8") as handle:
    handle.write(name + " " + " ".join(args) + "\\n")
if name == "capture_odds_snapshot.py":
    print("data/odds_snapshots/exact-live-path.json")
if name == "decision_bundle.py":
    value = args[args.index("--decision-snapshot") + 1]
    if value != "data/odds_snapshots/exact-live-path.json":
        raise SystemExit(23)
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for script in (
                "import_sporttery.py",
                "capture_odds_snapshot.py",
                "predict_today.py",
                "decision_bundle.py",
                "provisional_plan.py",
            ):
                (root / script).write_text(stub, encoding="utf-8")

            result = self.run_bash_body(
                body,
                root,
                REQUESTED_TARGET_DATE="2026-07-16",
            )

            self.assertEqual(0, result.returncode, result.stderr)
            calls = (root / "calls.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                [
                    "import_sporttery.py",
                    "capture_odds_snapshot.py",
                    "predict_today.py",
                    "decision_bundle.py",
                    "provisional_plan.py",
                ],
                [line.split(" ", 1)[0] for line in calls],
            )
            self.assertIn(
                "--decision-snapshot data/odds_snapshots/exact-live-path.json",
                calls[3],
            )

    def test_due_step_handles_empty_json_and_rejects_malformed_json(self):
        body = self.multiline_step_body(
            self.read_workflow("pre-kickoff-revalidation.yml"),
            "revalidate",
            "Run due revalidations",
        )
        stub = '''import os
print(os.environ["DUE_JSON"])
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "revalidation.py").write_text(stub, encoding="utf-8")
            output = root / "github-output.txt"
            empty = self.run_bash_body(
                body,
                root,
                REQUESTED_TARGET_DATE="",
                REQUESTED_NOW_BJT="2026-07-19T00:05:00+08:00",
                DUE_JSON='{"changed_dates": []}',
                GITHUB_OUTPUT=str(output),
            )
            self.assertEqual(0, empty.returncode, empty.stderr)
            emitted = output.read_text(encoding="utf-8")
            self.assertIn("changed_dates=\n", emitted)
            self.assertIn("display_date=\n", emitted)
            self.assertIn("changed=false\n", emitted)

            output.write_text("", encoding="utf-8")
            cross_midnight = self.run_bash_body(
                body,
                root,
                REQUESTED_TARGET_DATE="",
                REQUESTED_NOW_BJT="2026-07-19T00:05:00+08:00",
                DUE_JSON=(
                    '{"changed_dates": ["2026-07-18", "2026-07-19"]}'
                ),
                GITHUB_OUTPUT=str(output),
            )
            self.assertEqual(0, cross_midnight.returncode, cross_midnight.stderr)
            emitted = output.read_text(encoding="utf-8")
            self.assertIn("changed_dates=2026-07-18 2026-07-19\n", emitted)
            self.assertIn("display_date=2026-07-19\n", emitted)
            self.assertIn("changed=true\n", emitted)

            for malformed in ("{", '{"changed_dates": "2026-07-19"}'):
                with self.subTest(malformed=malformed):
                    output.write_text("", encoding="utf-8")
                    failed = self.run_bash_body(
                        body,
                        root,
                        REQUESTED_TARGET_DATE="",
                        REQUESTED_NOW_BJT="2026-07-19T00:05:00+08:00",
                        DUE_JSON=malformed,
                        GITHUB_OUTPUT=str(output),
                    )
                    self.assertNotEqual(0, failed.returncode)
                    self.assertEqual("", output.read_text(encoding="utf-8"))

    def test_cross_midnight_rebuild_preserves_both_date_reports_and_one_global_target(self):
        body = self.multiline_step_body(
            self.read_workflow("pre-kickoff-revalidation.yml"),
            "revalidate",
            "Rebuild changed reports",
        )
        stub = '''import sys
from pathlib import Path

with Path("global-calls.log").open("a", encoding="utf-8") as handle:
    handle.write(Path(sys.argv[0]).name + " " + " ".join(sys.argv[1:]) + "\\n")
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for script in (
                "build_site.py",
                "build_daily_image.py",
                "report_status.py",
                "revalidation_reporting.py",
            ):
                (root / script).write_text(stub, encoding="utf-8")
            expected = {}
            for report_date in ("2026-07-18", "2026-07-19"):
                directory = root / "web" / "revalidation" / report_date
                directory.mkdir(parents=True)
                image = directory / "revision-1.png"
                image.write_bytes(("image-" + report_date).encode("ascii"))
                status = directory / "status.json"
                status.write_text(
                    json.dumps(
                        {
                            "report_date": report_date,
                            "report_image_url": image.relative_to(root).as_posix(),
                        }
                    ),
                    encoding="utf-8",
                )
                expected[report_date] = (status.read_bytes(), image.read_bytes())
            subprocess.run(
                ["git", "init", "-q"], cwd=root, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.email", "workflow@example.test"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Workflow Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "fixture"], cwd=root, check=True
            )

            result = self.run_bash_body(
                body,
                root,
                CHANGED_DATES="2026-07-18 2026-07-19",
                DISPLAY_DATE="2026-07-19",
                GITHUB_RUN_ID="77",
                GITHUB_RUN_ATTEMPT="2",
            )

            self.assertEqual(0, result.returncode, result.stderr)
            for report_date, (status_bytes, image_bytes) in expected.items():
                directory = root / "web" / "revalidation" / report_date
                self.assertEqual(status_bytes, (directory / "status.json").read_bytes())
                self.assertEqual(image_bytes, (directory / "revision-1.png").read_bytes())
            calls = (root / "global-calls.log").read_text(encoding="utf-8").splitlines()
            for script in ("build_site.py", "build_daily_image.py", "report_status.py"):
                matching = [line for line in calls if line.startswith(script + " ")]
                self.assertEqual(1, len(matching), matching)
                self.assertIn("--date 2026-07-19", matching[0])
            self.assertEqual(
                1,
                sum(line.startswith("revalidation_reporting.py ") for line in calls),
            )

    def test_provisional_and_reporting_clis_expose_workflow_arguments(self):
        commands = {
            "provisional_plan.py": ([], ("--date", "--generated-at")),
            "revalidation_reporting.py": (["rebuild-index"], ("--now-bjt",)),
        }
        for script, (arguments, expected) in commands.items():
            result = subprocess.run(
                [sys.executable, str(ROOT / script), *arguments, "--help"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            for argument in expected:
                self.assertIn(argument, result.stdout)


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
        "REVALIDATION_INDEX_URL",
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

    def test_apps_script_readme_lists_exactly_eight_manual_config_properties(self):
        text = self.read_doc(self.APPS_SCRIPT_README)
        code = self.read_doc(self.CODE_PATH)
        section = re.search(
            r"^### 必须手工配置的 8 项 Script Properties\n(?P<body>.*?)(?=^### )",
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
        self.assertIn("`TEST_MODE` 是临时部署开关，不计入上述 8 项必填配置", text)

    def test_operator_docs_define_pre_kickoff_cutover_and_rollback_contract(self):
        combined = "\n".join(self.read_doc(path) for path in self.OPERATOR_DOCS)
        for literal in (
            "14:00 初选是 provisional",
            "provisional 金额不计入盈亏",
            "T-90",
            "T-30",
            "最终金额只能保持或降低",
            "错过窗口必须取消",
            "允许跨北京时间午夜",
            "Apps Script 是唯一的邮件发送方",
            "`.github/workflows/email-report.yml` 在 GitHub Actions 中保持 disabled",
            "回滚后的正确行为是零新增模拟投注",
        ):
            self.assertIn(literal, combined)

        for artifact in (
            "`data/live_odds_snapshots/YYYY-MM-DD/`",
            "`output/provisional_generation_YYYY-MM-DD.json`",
            "`output/revalidation_state_YYYY-MM-DD.json`",
            "`output/revalidation_receipts/YYYY-MM-DD/<candidate_id>-t90.json`",
            "`output/revalidation_receipts/YYYY-MM-DD/<candidate_id>-t30.json`",
            "`web/revalidation-index.json`",
            "`web/revalidation/YYYY-MM-DD/status.json`",
            "`web/revalidation/YYYY-MM-DD/revision-<revision>-<change-digest-prefix>.png`",
            "`REVALIDATION_INDEX_URL`",
            "`target_date`",
            "`now_bjt`",
            "`SENT_REVALIDATION_DIGESTS`",
            "最近 30 个业务日",
        ):
            self.assertIn(artifact, combined)

        cloud_setup = self.read_doc(ROOT / "CLOUD_SETUP.md")
        for reason_code in (
            "passed",
            "confirmed",
            "candidate_invalid",
            "snapshot_invalid",
            "fixture_mismatch",
            "market_mismatch",
            "market_not_selling",
            "single_ineligible",
            "kickoff_invalid",
            "snapshot_after_kickoff",
            "odds_invalid",
            "odds_below_minimum",
            "ev_below_minimum",
            "stake_below_minimum",
            "t90_window_missed",
            "t30_window_missed",
        ):
            self.assertIn(f"`{reason_code}`", cloud_setup)

    def test_repository_cutover_remains_simulation_only_and_shadow(self):
        config = json.loads(self.read_doc(ROOT / "betting_config.json"))
        self.assertEqual("shadow", config["pre_kickoff_revalidation"]["mode"])
        self.assertEqual("shadow", config["value_strategy"]["activation_mode"])
        self.assertEqual("simulation", config["simulation_account"]["mode"])
        self.assertFalse(config["simulation_account"]["real_money_automation"])

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
            "不可变导入清单",
            "初选 generation 指针",
            "单调候选状态",
            "幂等账本写入",
        ):
            self.assertIn(literal, combined)
        for misleading in ("cron 后备", "后备触发", "cron fallback", "补调度"):
            self.assertNotIn(misleading, combined)

    def test_docs_define_the_schema_two_stage_contract(self):
        apps_readme = self.read_doc(self.APPS_SCRIPT_README)
        cloud_setup = self.read_doc(ROOT / "CLOUD_SETUP.md")
        self.assertIn(
            "5000 monthly stake cap and 5000 realized-loss stop are separate controls",
            apps_readme,
        )
        for text in (apps_readme, cloud_setup):
            for literal in (
                "schema 2",
                "`forecast_ready`",
                "`initial_report_ready`",
                "`settlement_ready`",
                "`revalidation_ready`",
                "provisional 金额不进入盈亏",
                "不得把中午导入赔率",
            ):
                self.assertIn(literal, text)
            self.assertNotIn("`output/plan_lock_${TARGET_DATE}.json`", text)

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
            normal.index('setProperty("LAST_INITIAL_SENT_DATE"'),
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
                "配置上述 8 项 Script Properties",
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
                "回滚邮件触发器与赛前系统",
                "先禁用并删除 `runAutomation` 触发器",
                "回滚后的正确行为是零新增模拟投注",
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
