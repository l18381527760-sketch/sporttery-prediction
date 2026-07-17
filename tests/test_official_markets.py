import math
import unittest

from official_markets import (
    OfficialMarket,
    devig,
    normalize_market,
    parse_handicap,
    poisson_handicap_probabilities,
    poisson_total_probabilities,
)


class OfficialMarketMathTest(unittest.TestCase):
    def test_devig_normalizes_every_valid_outcome(self):
        fair = devig({"胜": 1.90, "平": 3.60, "负": 4.20})

        self.assertAlmostEqual(1.0, sum(fair.values()), places=12)
        self.assertEqual({"胜", "平", "负"}, set(fair))

    def test_devig_rejects_invalid_or_partial_prices(self):
        for prices in (
            {},
            {"胜": 1.90, "平": 3.60},
            {"胜": 1.90, "平": 3.60, "负": 1.0},
            {"胜": 1.90, "平": math.inf, "负": 4.20},
            {f"{goals}球": 2.0 + goals for goals in range(7)},
            {"h": 1.90, "d": 3.60, "unknown": 4.20},
        ):
            with self.subTest(prices=prices), self.assertRaises(ValueError):
                devig(prices)

    def test_total_goals_has_zero_through_six_and_seven_plus(self):
        probabilities = poisson_total_probabilities(1.20, 1.05)

        self.assertEqual(
            {"0球", "1球", "2球", "3球", "4球", "5球", "6球", "7+球"},
            set(probabilities),
        )
        self.assertAlmostEqual(1.0, sum(probabilities.values()), places=12)

    def test_poisson_rejects_invalid_expected_goals(self):
        for xg_home, xg_away in ((math.nan, 1.0), (1.0, math.inf), (-0.1, 1.0), (8.1, 1.0)):
            with self.subTest(xg_home=xg_home, xg_away=xg_away), self.assertRaises(ValueError):
                poisson_total_probabilities(xg_home, xg_away)

    def test_plus_one_handicap_changes_the_three_way_result(self):
        probabilities = poisson_handicap_probabilities(1.00, 1.40, +1)

        self.assertAlmostEqual(1.0, sum(probabilities.values()), places=9)
        self.assertGreater(probabilities["胜"], 0)
        self.assertGreater(probabilities["平"], 0)
        self.assertGreater(probabilities["负"], 0)

    def test_only_integer_sporttery_handicaps_are_accepted(self):
        self.assertEqual(1, parse_handicap("+1"))
        self.assertEqual(-1, parse_handicap("-1"))
        with self.assertRaises(ValueError):
            parse_handicap("-0.5")


class OfficialMarketNormalizationTest(unittest.TestCase):
    def test_normalize_market_maps_supported_official_market_selections(self):
        had = normalize_market("1001", "had", _raw({"h": "1.90", "d": "3.60", "a": "4.20"}))
        hhad = normalize_market(
            "1001",
            "hhad",
            _raw({"h": "2.40", "d": "3.20", "a": "2.80", "goalLine": "+1"}),
        )
        ttg = normalize_market(
            "1001",
            "ttg",
            _raw({f"s{index}": str(2.0 + index) for index in range(8)}),
        )

        self.assertIsInstance(had, OfficialMarket)
        self.assertEqual({"胜", "平", "负"}, set(had.prices))
        self.assertIsNone(had.line)
        self.assertEqual(1, hhad.line)
        self.assertEqual({"胜", "平", "负"}, set(hhad.prices))
        self.assertEqual(
            {"0球", "1球", "2球", "3球", "4球", "5球", "6球", "7+球"},
            set(ttg.prices),
        )
        self.assertEqual("sporttery", had.source)
        self.assertEqual("record-1001", had.source_record_id)
        self.assertEqual("2026-07-17T10:00:00+08:00", had.captured_at_bjt)

    def test_normalize_market_rejects_incomplete_or_malformed_markets(self):
        self.assertIsNone(normalize_market("1001", "had", _raw({"h": "1.90", "d": "3.60"})))
        self.assertIsNone(
            normalize_market("1001", "hhad", _raw({"h": "2.40", "d": "3.20", "a": "2.80"}))
        )
        self.assertIsNone(
            normalize_market("1001", "ttg", _raw({"s0": "not-a-price", **{f"s{index}": "3.0" for index in range(1, 8)}}))
        )

    def test_normalize_market_accepts_only_trusted_sources(self):
        for source, expected in (
            (" SportTery ", "sporttery"),
            ("ZGZCW", "zgzcw"),
            ("竞彩网", "竞彩网"),
            ("中国足彩网", "中国足彩网"),
        ):
            with self.subTest(source=source):
                market = normalize_market(
                    "1001", "had", _raw({"h": "1.90", "d": "3.60", "a": "4.20"}, source=source)
                )
                self.assertIsNotNone(market)
                self.assertEqual(expected, market.source)

        self.assertIsNone(
            normalize_market(
                "1001", "had", _raw({"h": "1.90", "d": "3.60", "a": "4.20"}, source="espn")
            )
        )

    def test_normalize_market_rejects_unknown_root_or_nested_selection_keys(self):
        self.assertIsNone(
            normalize_market(
                "1001",
                "had",
                _raw({"h": "1.90", "d": "3.60", "a": "4.20", "unknown": "8.00"}),
            )
        )
        self.assertIsNone(
            normalize_market(
                "1001",
                "had",
                _raw(
                    {"prices": {"h": "1.90", "d": "3.60", "a": "4.20", "unknown": "8.00"}}
                ),
            )
        )

    def test_normalize_market_allows_metadata_and_handicap_line_in_nested_prices(self):
        market = normalize_market(
            "1001",
            "hhad",
            _raw(
                {"prices": {"h": "2.40", "d": "3.20", "a": "2.80", "goalLine": "+1"}}
            ),
        )

        self.assertIsNotNone(market)
        self.assertEqual(1, market.line)

    def test_normalize_market_rejects_noncanonical_match_ids(self):
        raw = _raw({"h": "1.90", "d": "3.60", "a": "4.20"})
        for match_id in ("", "   ", "1001 2", "1001\t2", "1001\n2", "1001\x00"):
            with self.subTest(match_id=match_id):
                self.assertIsNone(normalize_market(match_id, "had", raw))


def _raw(prices: dict[str, str | dict], *, source: str = "sporttery") -> dict:
    return {
        **prices,
        "source": source,
        "source_record_id": "record-1001",
        "captured_at_bjt": "2026-07-17T10:00:00+08:00",
    }


if __name__ == "__main__":
    unittest.main()
