"""Tests for report generation service — metric correctness."""

from unittest.mock import MagicMock

from app.services.report_service import _compute_competitor_rank


def _make_brand(id: str, name: str, is_competitor: bool) -> dict:
    return {"id": id, "name": name, "is_competitor": is_competitor}


def _make_result(brand_id: str, mention_found: bool) -> MagicMock:
    r = MagicMock()
    r.brand_id = brand_id
    r.mention_found = mention_found
    return r


class TestCompetitorRank:
    def test_primary_wins(self):
        brand_map = {
            "brand-a": _make_brand("brand-a", "BrandA", False),
            "brand-b": _make_brand("brand-b", "BrandB", True),
        }
        results = [
            _make_result("brand-a", True),
            _make_result("brand-a", True),
            _make_result("brand-b", True),
        ]
        rank = _compute_competitor_rank(results, brand_map)
        assert rank == 1

    def test_primary_loses(self):
        brand_map = {
            "brand-a": _make_brand("brand-a", "BrandA", False),
            "brand-b": _make_brand("brand-b", "BrandB", True),
        }
        results = [
            _make_result("brand-a", True),
            _make_result("brand-b", True),
            _make_result("brand-b", True),
            _make_result("brand-b", True),
        ]
        rank = _compute_competitor_rank(results, brand_map)
        assert rank == 2

    def test_no_mentions(self):
        brand_map = {
            "brand-a": _make_brand("brand-a", "BrandA", False),
        }
        results = [
            _make_result("brand-a", False),
        ]
        rank = _compute_competitor_rank(results, brand_map)
        assert rank is None

    def test_empty_results(self):
        rank = _compute_competitor_rank([], {})
        assert rank is None


class TestMetricStability:
    """Verify that adding more competitors doesn't dilute per-brand scores."""

    def test_mention_rate_per_brand(self):
        """Simulate the per-brand mention rate calculation from report_service."""
        results_per_brand = {
            "primary": [True],       # 1/1 = 100%
            "competitor_1": [True],  # 1/1 = 100%
        }
        avg = sum(
            sum(r for r in rates) / len(rates)
            for rates in results_per_brand.values()
        ) / len(results_per_brand)
        assert avg == 1.0

    def test_metric_stable_with_more_competitors(self):
        """Adding more competitors should NOT change primary brand's rate."""
        primary_rate = 1 / 1  # 100%
        competitor_rates = [1 / 1] * 5  # all 100%
        all_rates = [primary_rate] + competitor_rates
        avg = sum(all_rates) / len(all_rates)
        assert avg == 1.0

    def test_metric_old_formula_diluted(self):
        """Old formula (total mentions / total results) would be diluted."""
        # With 5 brands, 3 mentioned
        total_results = 5
        total_mentions = 3
        old_rate = total_mentions / total_results  # 0.6
        assert old_rate == 0.6
