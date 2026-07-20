import hashlib
import json
import multiprocessing
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from PIL.PngImagePlugin import PngInfo

import build_daily_image
import build_site
import revalidation_reporting
from revalidation_reporting import build_revalidation_index, publish_revalidation_report


BJT = timezone(timedelta(hours=8))
DAY = date(2026, 7, 18)
AT_2355 = datetime(2026, 7, 18, 23, 55, tzinfo=BJT)
AT_0010 = datetime(2026, 7, 19, 0, 10, tzinfo=BJT)


def confirmed(candidate_id: str, *, ledger_status: str = "ingested") -> dict:
    return {
        "candidate_id": candidate_id,
        "state": "confirmed",
        "ledger_status": ledger_status,
        "match": "A vs B",
        "market": "主胜",
        "provisional_odds": "2.10",
        "current_odds": "2.05",
        "provisional_stake": 20,
        "final_stake": 16,
        "current_ev": "0.087",
        "reason": "赔率仍满足门槛",
        "next_revalidation_at_bjt": "",
    }


def cancelled(candidate_id: str) -> dict:
    return {
        **confirmed(candidate_id, ledger_status="not_applicable"),
        "state": "cancelled",
        "final_stake": 0,
        "reason": "赔率低于最低门槛",
    }


def runtime_entry(candidate: dict) -> dict:
    payload = {
        key: value
        for key, value in candidate.items()
        if key not in {"state", "ledger_status", "final_stake"}
    }
    return {
        "candidate": payload,
        "state": candidate["state"],
        "ledger_status": candidate["ledger_status"],
        "confirmed_stake": candidate.get("final_stake", 0),
    }


def write_runtime_state(root: Path, candidates: list[dict]) -> Path:
    path = root / f"output/revalidation_state_{DAY.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"candidates": [runtime_entry(candidate) for candidate in candidates]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def concurrent_publish_worker(
    root_text: str,
    change: dict,
    start,
    results,
) -> None:
    start.wait()
    try:
        report = publish_revalidation_report(
            Path(root_text), DAY, [change], AT_2355, "a" * 40
        )
        results.put(("ok", report))
    except Exception as exc:  # pragma: no cover - asserted in the parent process
        results.put(("error", f"{type(exc).__name__}: {exc}"))


class RevalidationReportingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_cross_midnight_publication_keeps_prior_date_image_immutable(self):
        first = publish_revalidation_report(
            self.root, DAY, [confirmed("c1")], AT_2355, "abc"
        )
        old_path = self.root / first["report_image_url"]
        old_bytes = old_path.read_bytes()

        publish_revalidation_report(
            self.root, DAY + timedelta(days=1), [cancelled("c2")], AT_0010, "def"
        )

        self.assertEqual(old_bytes, old_path.read_bytes())

    def test_status_and_png_hashes_are_verified_from_disk_with_immutable_revision_names(self):
        first = publish_revalidation_report(
            self.root, DAY, [cancelled("c1")], AT_2355, "abc"
        )
        second = publish_revalidation_report(
            self.root, DAY, [cancelled("c2")], AT_2355, "abc"
        )
        first_image = self.root / first["report_image_url"]
        second_image = self.root / second["report_image_url"]
        status_path = self.root / second["status_url"]
        status = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertEqual(1, first["revision"])
        self.assertEqual(2, second["revision"])
        self.assertNotEqual(first_image, second_image)
        self.assertTrue(first_image.name.startswith("revision-1-"))
        self.assertTrue(second_image.name.startswith("revision-2-"))
        self.assertEqual(
            hashlib.sha256(second_image.read_bytes()).hexdigest(),
            status["report_image_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(status_path.read_bytes()).hexdigest(),
            second["status_sha256"],
        )
        with Image.open(second_image) as image:
            self.assertEqual("2026-07-18", image.info["report_date"])
            self.assertEqual(status["change_digest"], image.info["change_digest"])
            self.assertEqual("revalidation", image.info["report_stage"])

    def test_digest_groups_every_newly_reportable_terminal_candidate_canonically(self):
        report = publish_revalidation_report(
            self.root,
            DAY,
            [cancelled("c2"), confirmed("c1")],
            AT_2355,
            "abc",
        )
        status = json.loads((self.root / report["status_url"]).read_text(encoding="utf-8"))

        self.assertEqual(["c1", "c2"], [row["candidate_id"] for row in status["changed_candidates"]])
        self.assertEqual(report["change_digest"], status["change_digest"])

    def test_confirmed_candidate_stays_hidden_until_canonical_ledger_ingestion_succeeds(self):
        write_runtime_state(
            self.root, [confirmed("c1", ledger_status="pending")]
        )
        hidden = publish_revalidation_report(
            self.root, DAY, [confirmed("c1", ledger_status="pending")], AT_2355, "abc"
        )
        write_runtime_state(self.root, [confirmed("c1")])
        visible = publish_revalidation_report(
            self.root, DAY, [confirmed("c1")], AT_2355, "abc"
        )

        self.assertEqual(0, hidden["revision"])
        self.assertEqual("", hidden["change_digest"])
        self.assertEqual("", hidden["report_image_url"])
        self.assertEqual(1, visible["revision"])

    def test_revision_zero_status_and_index_publish_the_first_durable_due_time(self):
        provisional = {
            **confirmed("p1", ledger_status="not_applicable"),
            "state": "provisional",
            "kickoff_at": "2026-07-19T02:00:00+08:00",
        }
        write_runtime_state(self.root, [provisional])

        status = publish_revalidation_report(
            self.root, DAY, [], AT_2355, "a" * 40
        )
        index = json.loads(
            (self.root / "web/revalidation-index.json").read_text(encoding="utf-8")
        )

        self.assertEqual(0, status["revision"])
        self.assertEqual("2026-07-19T00:15:00+08:00", status["next_revalidation_at_bjt"])
        self.assertEqual([DAY.isoformat()], [item["report_date"] for item in index["dates"]])

    def test_retry_reuses_matching_image_after_status_write_crash(self):
        write_runtime_state(self.root, [cancelled("c1")])
        real_write = revalidation_reporting._write_json_atomic

        def fail_status(path, value):
            if path.name == "status.json":
                raise OSError("status replace interrupted")
            return real_write(path, value)

        with patch.object(
            revalidation_reporting, "_write_json_atomic", side_effect=fail_status
        ), patch.object(
            build_daily_image, "BUILD_ID", "first-run"
        ), self.assertRaisesRegex(OSError, "status replace interrupted"):
            publish_revalidation_report(
                self.root, DAY, [cancelled("c1")], AT_2355, "a" * 40
            )

        images_after_crash = list(
            (self.root / f"web/revalidation/{DAY.isoformat()}").glob("revision-*.png")
        )
        self.assertEqual(1, len(images_after_crash))
        crashed_bytes = images_after_crash[0].read_bytes()

        with patch.object(build_daily_image, "BUILD_ID", "retry-run"):
            recovered = publish_revalidation_report(
                self.root, DAY, [cancelled("c1")], AT_2355, "a" * 40
            )

        self.assertEqual(1, recovered["revision"])
        self.assertEqual(crashed_bytes, (self.root / recovered["report_image_url"]).read_bytes())
        self.assertEqual(1, len(list(images_after_crash[0].parent.glob("revision-*.png"))))

    def test_concurrent_processes_group_all_durable_candidates_once(self):
        changes = [cancelled("c1"), cancelled("c2")]
        write_runtime_state(self.root, changes)
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=concurrent_publish_worker,
                args=(str(self.root), change, start, results),
            )
            for change in changes
        ]
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)

        self.assertFalse(any(process.is_alive() for process in processes))
        self.assertEqual(["ok", "ok"], sorted(outcome[0] for outcome in outcomes))
        reports = [outcome[1] for outcome in outcomes]
        self.assertEqual({1}, {report["revision"] for report in reports})
        self.assertEqual(1, len({report["change_digest"] for report in reports}))
        status = json.loads(
            (self.root / f"web/revalidation/{DAY.isoformat()}/status.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(["c1", "c2"], status["published_candidate_ids"])
        self.assertEqual(
            ["c1", "c2"],
            [candidate["candidate_id"] for candidate in status["changed_candidates"]],
        )
        self.assertEqual(1, len(list((self.root / f"web/revalidation/{DAY}").glob("revision-*.png"))))

    def test_identical_duplicates_collapse_and_conflicting_duplicates_fail_before_write(self):
        duplicate = cancelled("c1")
        write_runtime_state(self.root, [duplicate])
        report = publish_revalidation_report(
            self.root, DAY, [duplicate, dict(duplicate)], AT_2355, "a" * 40
        )
        self.assertEqual(
            ["c1"],
            [candidate["candidate_id"] for candidate in report["changed_candidates"]],
        )

        other_root = self.root / "conflict"
        conflict = {**duplicate, "reason": "different terminal event"}
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            publish_revalidation_report(
                other_root,
                DAY,
                [duplicate, conflict],
                AT_2355,
                "a" * 40,
            )
        self.assertFalse((other_root / "web").exists())

    def test_compact_runtime_change_uses_persisted_ingested_status_and_next_due_time(self):
        state_path = self.root / "output/revalidation_state_2026-07-18.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({
            "candidates": [
                {
                    "candidate": {"candidate_id": "c1", "match": "A vs B", "market": "主胜"},
                    "state": "confirmed",
                    "ledger_status": "ingested",
                    "confirmed_stake": 16,
                },
                {
                    "candidate": {"candidate_id": "p2", "kickoff_at": "2026-07-19T02:00:00+08:00"},
                    "state": "provisional",
                    "ledger_status": "not_applicable",
                },
            ],
        }, ensure_ascii=False), encoding="utf-8")

        report = publish_revalidation_report(
            self.root,
            DAY,
            [{"candidate_id": "c1", "state": "confirmed", "stake": 16, "receipt": {"current_odds": "2.05", "current_ev": "0.087"}}],
            AT_2355,
            "abc",
        )
        status = json.loads((self.root / report["status_url"]).read_text(encoding="utf-8"))

        self.assertEqual("ingested", status["changed_candidates"][0]["ledger_status"])
        self.assertEqual("2026-07-19T00:15:00+08:00", status["next_revalidation_at_bjt"])

    def test_noop_does_not_create_a_new_revision_or_change_digest(self):
        first = publish_revalidation_report(
            self.root, DAY, [confirmed("c1")], AT_2355, "abc"
        )
        repeated = publish_revalidation_report(self.root, DAY, [], AT_2355, "abc")

        self.assertEqual(first, repeated)
        self.assertEqual(
            [first["report_image_url"]],
            [path.relative_to(self.root).as_posix() for path in (self.root / "web/revalidation/2026-07-18").glob("revision-*.png")],
        )

    def test_noop_refreshes_scheduler_fields_from_current_durable_state(self):
        provisional = {
            **confirmed("p2", ledger_status="not_applicable"),
            "state": "provisional",
            "kickoff_at": "2026-07-19T02:00:00+08:00",
        }
        write_runtime_state(self.root, [cancelled("c1"), provisional])
        first = publish_revalidation_report(
            self.root, DAY, [cancelled("c1")], AT_2355, "a" * 40
        )
        images = list((self.root / f"web/revalidation/{DAY}").glob("revision-*.png"))

        screened = {**provisional, "state": "screened"}
        write_runtime_state(self.root, [cancelled("c1"), screened])
        refreshed = publish_revalidation_report(
            self.root, DAY, [], AT_0010, "a" * 40
        )

        self.assertEqual(first["revision"], refreshed["revision"])
        self.assertEqual(first["change_digest"], refreshed["change_digest"])
        self.assertEqual(first["report_image_url"], refreshed["report_image_url"])
        self.assertEqual("2026-07-19T01:20:00+08:00", refreshed["next_revalidation_at_bjt"])
        self.assertEqual(images, list((self.root / f"web/revalidation/{DAY}").glob("revision-*.png")))

    def test_index_keeps_two_dates_and_omits_completed_notified_dates(self):
        for offset in range(3):
            publish_revalidation_report(
                self.root,
                DAY + timedelta(days=offset),
                [cancelled(f"c{offset}")],
                AT_2355 + timedelta(days=offset),
                "abc",
            )
        status_path = self.root / "web/revalidation/2026-07-18/status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["all_candidates_terminal"] = True
        status["notification_sent"] = True
        status_path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")

        index = build_revalidation_index(self.root, AT_0010 + timedelta(days=2))

        self.assertEqual(["2026-07-19", "2026-07-20"], [item["report_date"] for item in index["dates"]])
        for item in index["dates"]:
            status_file = self.root / item["status_url"]
            self.assertEqual(hashlib.sha256(status_file.read_bytes()).hexdigest(), item["status_sha256"])

    def test_report_builders_label_provisional_amount_outside_profit_and_keep_confirmed_paid_only(self):
        provisional = [
            {"candidate_id": "p1", "match": "A vs B", "market": "主胜", "provisional_stake": "20", "state": "provisional"},
        ]
        states = {"p1": {"state": "screened", "ledger_status": "pending", "confirmed_stake": 0}}
        paid_ledger = [{"date": DAY.isoformat(), "stake": "16", "status": "未结算", "profit": "0"}]
        with patch.object(build_site, "read_provisional_candidates", return_value=(provisional, states)), patch.object(
            build_site, "read_betting_ledger", return_value=paid_ledger
        ):
            html = build_site.render_site([])

        self.assertIn("暂定金额（未计入盈亏）", html)
        self.assertIn("90分钟筛查通过", html)
        self.assertIn("累计模拟投入", html)
        self.assertNotIn("20.00", html.split("累计模拟投入", 1)[1].split("</section>", 1)[0])

    def test_dashboard_reads_immutable_provisional_csv_before_overlaying_runtime_state(self):
        output = self.root / "output"
        output.mkdir()
        (output / "provisional_betting_plan_2026-07-18.csv").write_text(
            "candidate_id,match,market,provisional_stake\n"
            "p1,A vs B,主胜,20\n",
            encoding="utf-8",
        )
        (output / "revalidation_state_2026-07-18.json").write_text(json.dumps({
            "candidates": [{
                "candidate": {"candidate_id": "p1"},
                "state": "screened",
                "ledger_status": "not_applicable",
            }],
        }, ensure_ascii=False), encoding="utf-8")
        with patch.object(build_site, "OUTPUT_DIR", output):
            candidates, states = build_site.read_provisional_candidates()

        self.assertEqual("A vs B", candidates[0]["match"])
        self.assertEqual("screened", states["p1"]["state"])

    def test_production_candidate_odds_field_wins_in_site_and_png_rendering(self):
        production_candidate = {
            **cancelled("p1"),
            "odds": "2.10",
            "initial_odds": "9.90",
            "provisional_odds": "8.80",
        }
        with patch.object(
            build_site,
            "read_provisional_candidates",
            return_value=([production_candidate], {"p1": production_candidate}),
        ):
            html = build_site.render_site([])
        self.assertIn(">2.10</td>", html)
        self.assertNotIn(">9.90</td>", html)
        self.assertNotIn(">8.80</td>", html)

        rendered_text = []
        real_draw_fitted_text = build_daily_image.draw_fitted_text

        def capture_text(draw, position, text, *args, **kwargs):
            rendered_text.append(str(text))
            return real_draw_fitted_text(draw, position, text, *args, **kwargs)

        image_path = self.root / "canonical-odds.png"
        with patch.object(
            build_daily_image, "draw_fitted_text", side_effect=capture_text
        ):
            build_daily_image.draw_report(
                output_path=image_path,
                report_date=DAY,
                revalidation_changes=[production_candidate],
                change_digest="f" * 64,
            )
        odds_lines = [line for line in rendered_text if "初选赔率" in line]
        self.assertEqual(1, len(odds_lines))
        self.assertIn("2.10", odds_lines[0])
        self.assertNotIn("9.90", odds_lines[0])
        self.assertNotIn("8.80", odds_lines[0])

    def test_index_drops_noncanonical_or_tampered_status_image_bindings(self):
        write_runtime_state(self.root, [cancelled("c1")])
        report = publish_revalidation_report(
            self.root, DAY, [cancelled("c1")], AT_2355, "a" * 40
        )
        status_path = self.root / report["status_url"]
        original_status = json.loads(status_path.read_text(encoding="utf-8"))
        index_path = self.root / "web/revalidation-index.json"

        invalid_statuses = [
            {**original_status, "report_image_url": "web/revalidation/missing.png"},
            {**original_status, "report_image_url": "web/revalidation/2026-07-18/../status.json"},
            {**original_status, "report_image_sha256": "0" * 64},
            {**original_status, "report_image_sha256": "not-a-digest"},
        ]
        for invalid in invalid_statuses:
            with self.subTest(binding=invalid["report_image_url"], digest=invalid["report_image_sha256"]):
                revalidation_reporting._write_json_atomic(status_path, invalid)
                rebuilt = build_revalidation_index(self.root, AT_0010)
                self.assertEqual([], rebuilt["dates"])
                self.assertEqual([], json.loads(index_path.read_text(encoding="utf-8"))["dates"])

        image_path = self.root / original_status["report_image_url"]
        expected_metadata = {
            "report_date": DAY.isoformat(),
            "change_digest": original_status["change_digest"],
            "report_stage": "revalidation",
        }
        metadata_variants = [
            ("metadata-free", {}),
            (
                "report-date",
                {**expected_metadata, "report_date": (DAY + timedelta(days=1)).isoformat()},
            ),
            ("change-digest", {**expected_metadata, "change_digest": "0" * 64}),
            ("report-stage", {**expected_metadata, "report_stage": "daily"}),
        ]
        for name, metadata in metadata_variants:
            with self.subTest(metadata=name):
                pnginfo = PngInfo()
                for key, value in metadata.items():
                    pnginfo.add_text(key, value)
                Image.new("RGB", (1, 1)).save(image_path, pnginfo=pnginfo)
                invalid = {
                    **original_status,
                    "report_image_sha256": hashlib.sha256(
                        image_path.read_bytes()
                    ).hexdigest(),
                }
                revalidation_reporting._write_json_atomic(status_path, invalid)

                rebuilt = build_revalidation_index(self.root, AT_0010)

                self.assertEqual([], rebuilt["dates"])
                self.assertEqual(
                    [],
                    json.loads(index_path.read_text(encoding="utf-8"))["dates"],
                )
                with self.assertRaisesRegex(
                    ValueError, "existing revalidation status is invalid"
                ):
                    publish_revalidation_report(self.root, DAY, [], AT_0010, "a" * 40)

        revalidation_reporting._write_json_atomic(status_path, original_status)
        image_path.write_bytes(image_path.read_bytes() + b"tampered")
        rebuilt = build_revalidation_index(self.root, AT_0010)
        self.assertEqual([], rebuilt["dates"])

        image_path.write_bytes(image_path.read_bytes()[:-8])
        status_path.write_text(
            json.dumps(original_status, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        rebuilt = build_revalidation_index(self.root, AT_0010)
        self.assertEqual([], rebuilt["dates"])


if __name__ == "__main__":
    unittest.main()
