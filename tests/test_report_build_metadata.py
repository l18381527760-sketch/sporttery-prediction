import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import build_daily_image
import build_site


ROOT = Path(__file__).resolve().parents[1]


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


if __name__ == "__main__":
    unittest.main()
