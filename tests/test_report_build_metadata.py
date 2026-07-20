import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import build_daily_image
import build_site


ROOT = Path(__file__).resolve().parents[1]
REPORT_DATE = date(2026, 7, 19)


class ReportBuildMetadataTest(unittest.TestCase):
    def imported_build_ids(self, report_build_id):
        environment = os.environ.copy()
        if report_build_id is None:
            environment.pop("REPORT_BUILD_ID", None)
        else:
            environment["REPORT_BUILD_ID"] = report_build_id

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json, build_daily_image, build_site; "
                    "print(json.dumps([build_site.BUILD_ID, "
                    "build_daily_image.BUILD_ID]))"
                ),
            ],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    def test_build_ids_bind_to_environment_and_default_to_local(self):
        self.assertEqual(
            ["run-42-decision", "run-42-decision"],
            self.imported_build_ids("run-42-decision"),
        )
        self.assertEqual(["local", "local"], self.imported_build_ids(None))

    def test_site_contains_machine_readable_build_id(self):
        with patch.object(build_site, "BUILD_ID", "run-42-decision"):
            html = build_site.render_site([])

        self.assertIn(
            '<meta name="report-build-id" content="run-42-decision">', html
        )
        self.assertNotIn("run-42-decision", html.split("</head>", 1)[1])

    def test_site_escapes_build_id_in_meta_content(self):
        with patch.object(build_site, "BUILD_ID", 'run-<&"decision'):
            html = build_site.render_site([])

        self.assertIn(
            '<meta name="report-build-id" '
            'content="run-&lt;&amp;&quot;decision">',
            html,
        )
        self.assertNotIn('run-<&"decision', html)

    def test_site_uses_game_prediction_dashboard_title(self):
        html = build_site.render_site([])

        self.assertIn("<title>博弈预测看板</title>", html)
        self.assertIn("<h1>博弈预测看板</h1>", html)
        self.assertNotIn("世界杯每日预测看板", html)

    def test_png_contains_the_same_build_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            web = root / "web"
            output.mkdir()
            with patch.object(build_daily_image, "OUTPUT_DIR", output), patch.object(
                build_daily_image, "WEB_DIR", web
            ), patch.object(build_daily_image, "BUILD_ID", "run-42-decision"):
                path = build_daily_image.draw_report()

            with Image.open(path) as image:
                self.assertEqual("run-42-decision", image.info["build_id"])

    def test_provisional_png_uses_only_pointer_selected_generation_and_binds_metadata(self):
        selected = {
            "generation_id": "a" * 64,
            "candidates": [
                {
                    "candidate_id": "selected-candidate",
                    "route": "active",
                    "provisional_stake": 20,
                    "source_plan_row": {
                        "match": "Pointer Selected Match",
                        "play": "HAD",
                        "selection": "Home",
                        "probability": "0.55",
                        "odds": "2.10",
                        "stake": "20",
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            web = root / "web"
            output.mkdir()
            (output / "betting_plan_2099-12-31.csv").write_text(
                "match,stake\nLegacy Latest Match,999\n", encoding="utf-8"
            )
            with patch.object(build_daily_image, "ROOT", root), patch.object(
                build_daily_image, "OUTPUT_DIR", output
            ), patch.object(build_daily_image, "WEB_DIR", web), patch.object(
                build_daily_image, "BUILD_ID", "run-77-provisional"
            ), patch.object(
                build_daily_image,
                "read_valid_provisional_state",
                return_value=selected,
                create=True,
            ) as validated, patch.object(
                build_daily_image,
                "latest_plan",
                side_effect=AssertionError("legacy latest-plan fallback used"),
            ):
                path = build_daily_image.draw_report(
                    report_date=REPORT_DATE,
                    report_stage="provisional",
                )

            validated.assert_called_once_with(root, REPORT_DATE)
            with Image.open(path) as image:
                self.assertEqual(REPORT_DATE.isoformat(), image.info["report_date"])
                self.assertEqual("provisional", image.info["report_stage"])
                self.assertEqual("run-77-provisional", image.info["build_id"])

    def test_provisional_site_uses_only_pointer_selected_generation(self):
        selected = {
            "generation_id": "b" * 64,
            "candidates": [
                {
                    "candidate_id": "selected-candidate",
                    "route": "active",
                    "provisional_stake": 20,
                    "source_plan_row": {
                        "match": "Pointer Selected Match",
                        "play": "HAD",
                        "selection": "Home",
                        "probability": "0.55",
                        "odds": "2.10",
                        "stake": "20",
                    },
                }
            ],
        }
        with patch.object(
            build_site,
            "read_valid_provisional_state",
            return_value=selected,
            create=True,
        ) as validated, patch.object(
            build_site,
            "read_provisional_candidates",
            side_effect=AssertionError("legacy provisional glob used"),
        ):
            html = build_site.render_site(
                [], report_date=REPORT_DATE, report_stage="provisional"
            )

        validated.assert_called_once_with(build_site.ROOT, REPORT_DATE)
        self.assertIn("Pointer Selected Match", html)
        self.assertIn(
            '<meta name="report-date" content="2026-07-19">', html
        )
        self.assertIn(
            '<meta name="report-stage" content="provisional">', html
        )

    def test_new_builder_clis_reject_noncanonical_dates_with_argparse_exit_two(self):
        for script in ("build_site.py", "build_daily_image.py"):
            for invalid in ("20260719", "2026-7-19", "not-a-date"):
                with self.subTest(script=script, invalid=invalid):
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(ROOT / script),
                            "--date",
                            invalid,
                            "--stage",
                            "provisional",
                        ],
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(2, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
