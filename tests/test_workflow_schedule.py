import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowScheduleTest(unittest.TestCase):
    WORKFLOWS = ROOT / ".github" / "workflows"
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
        start = lines.index(marker)
        for end in range(start + 1, len(lines)):
            if lines[end].startswith("      - name: "):
                return "\n".join(lines[start:end])
        return "\n".join(lines[start:])

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

    def test_base_forecast_is_required_and_uses_beijing_target_date(self):
        text = self.read_workflow("daily-forecast.yml")
        self.assertIn("TZ: Asia/Shanghai", self.job_block(text, "forecast"))
        step = self.step_block(
            text, "forecast", "Generate required base forecast and plan"
        )
        expected = [
            'TARGET_DATE="$(date +%F)"',
            'python import_sporttery.py --date "$TARGET_DATE"',
            "python build_historical_features.py",
            'python predict_today.py --date "$TARGET_DATE"',
            'python generate_betting_plan.py --date "$TARGET_DATE"',
        ]
        positions = [step.index(command) for command in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("id: base_forecast", step)
        self.assertNotIn("continue-on-error: true", step)
        self.assertNotIn("collect_market_heat.py", step)
        self.assertNotIn("generate_draw_alert.py", step)
        self.assertNotIn("draw_alert_ledger.py", step)
        self.assertNotIn("build_site.py", step)

    def test_base_forecast_optional_steps_fail_independently_and_report_still_builds(self):
        text = self.read_workflow("daily-forecast.yml")
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
                self.assertIn('TARGET_DATE="$(date +%F)"', step)
            positions.append(text.index(f"      - name: {step_name}"))

        build = self.step_block(text, "forecast", "Build final website and image")
        self.assertIn(
            "if: always() && steps.base_forecast.outcome == 'success'", build
        )
        self.assertIn("python build_site.py", build)
        self.assertIn("python build_daily_image.py", build)
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

    def test_refresh_failure_isolation_binds_each_command_to_its_own_step(self):
        text = self.read_workflow("draw-alert-refresh.yml")
        refresh_steps = {
            "Refresh Sporttery import": 'python import_sporttery.py --date "$TARGET_DATE"',
            "Refresh predictions": 'python predict_today.py --date "$TARGET_DATE"',
            "Refresh optional market evidence": 'python collect_market_heat.py --date "$TARGET_DATE"',
            "Refresh draw alerts": 'python generate_draw_alert.py --date "$TARGET_DATE"',
        }
        step_positions = []
        for step_name, command in refresh_steps.items():
            step = self.step_block(text, "refresh", step_name)
            self.assertIn("continue-on-error: true", step)
            self.assertIn('TARGET_DATE="$(date +%F)"', step)
            self.assertIn(command, step)
            step_positions.append(text.index(f"      - name: {step_name}"))
        self.assertEqual(step_positions, sorted(step_positions))

        ledger_step = self.step_block(text, "refresh", "Refresh draw alert ledger")
        rebuild_step = self.step_block(text, "refresh", "Rebuild report from the latest committed data")
        self.assertIn("python draw_alert_ledger.py --settle", ledger_step)
        self.assertIn("python build_site.py", rebuild_step)
        self.assertIn("python build_daily_image.py", rebuild_step)
        self.assertLess(
            text.index("      - name: Refresh draw alert ledger"),
            text.index("      - name: Rebuild report from the latest committed data"),
        )

    def test_settlement_uses_yesterday_for_results_and_today_for_training(self):
        text = self.read_workflow("noon-settlement.yml")
        step = self.step_block(text, "settlement", "Update results, settle ledgers, and train the draw model")
        expected = [
            'TODAY="$(date +%F)"',
            'SETTLEMENT_DATE="$(date -d \'yesterday\' +%F)"',
            'python update_sporttery_results.py --date "$SETTLEMENT_DATE"',
            "python build_historical_features.py",
            "python generate_betting_plan.py --settle-only",
            "python draw_alert_ledger.py --settle",
            'python draw_model_learning.py --train --date "$TODAY"',
            "python build_site.py",
            "python build_daily_image.py",
        ]
        positions = [step.index(command) for command in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('python update_sporttery_results.py --date "$TODAY"', step)

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

    def test_commits_include_immutable_learning_and_report_outputs(self):
        required = (
            "data/market_heat_*.json",
            "data/draw_feature_snapshots/*.json",
            "data/models/*.joblib",
            "output/draw_alert*.csv",
            "output/draw_alert*.json",
            "output/draw_model_registry.json",
            "web/index.html",
            "web/daily-report.png",
        )
        commit_steps = {
            "daily-forecast.yml": ("forecast", "Commit generated files"),
            "draw-alert-refresh.yml": ("refresh", "Commit refreshed files"),
            "noon-settlement.yml": ("settlement", "Commit settlement files"),
        }
        for name, (job_name, step_name) in commit_steps.items():
            step = self.step_block(self.read_workflow(name), job_name, step_name)
            for pattern in required:
                self.assertIn(pattern, step)

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


if __name__ == "__main__":
    unittest.main()
