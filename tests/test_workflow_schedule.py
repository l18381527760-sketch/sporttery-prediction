import re
import unittest
from pathlib import Path


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
        optional_steps = {
            "Capture opening odds": (
                'python capture_odds_snapshot.py --date "$TARGET_DATE" --phase opening',
                True,
            ),
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
            'python import_sporttery.py --date "$TARGET_DATE"',
            'python capture_odds_snapshot.py --date "$TARGET_DATE" --phase decision',
            'if python plan_lock.py is-locked --date "$TARGET_DATE"; then',
            'echo "Decision plan is already locked for $TARGET_DATE"',
            "else",
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
        self.assertIn("运行状态属性由 `Code.gs` 自动写入", text)
        self.assertIn("`TEST_MODE` 是临时部署开关，不计入上述 7 项必填配置", text)

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
