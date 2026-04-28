"""Tests for download statistics aggregation."""

from unittest.mock import patch


from llm_router.stats import PyPIStats


class TestPyPIStats:
    """Tests for PyPI stats fetching and formatting."""

    def test_get_package_stats_success(self):
        """Test fetching stats for a single package."""
        mock_response = {
            "data": {
                "last_recent": 1250,
                "last_month": 500,
            }
        }

        with patch("llm_router.stats.requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.raise_for_status.return_value = None

            result = PyPIStats.get_package_stats("llm-routing", period="recent")

            assert result == mock_response
            mock_get.assert_called_once_with(
                "https://pypistats.org/api/packages/llm-routing/recent",
                timeout=10,
            )

    def test_get_package_stats_error(self):
        """Test error handling when package stats fetch fails."""
        with patch("llm_router.stats.requests.get") as mock_get:
            mock_get.side_effect = Exception("Network error")

            result = PyPIStats.get_package_stats("nonexistent", period="recent")

            assert result == {}

    def test_get_combined_stats_success(self):
        """Test combining stats from multiple packages."""
        mock_response_llm_routing = {
            "data": {"last_recent": 1000}
        }
        mock_response_claude_code = {
            "data": {"last_recent": 500}
        }

        with patch("llm_router.stats.PyPIStats.get_package_stats") as mock_get:
            def side_effect(package_name, period):
                if package_name == "llm-routing":
                    return mock_response_llm_routing
                elif package_name == "claude-code-llm-router":
                    return mock_response_claude_code
                return {}

            mock_get.side_effect = side_effect

            result = PyPIStats.get_combined_stats(period="recent")

            assert result["total_downloads"] == 1500
            assert result["packages"]["llm-routing"]["downloads"] == 1000
            assert result["packages"]["claude-code-llm-router"]["downloads"] == 500
            assert result["period"] == "recent"

    def test_format_stats_text(self):
        """Test formatting stats as plain text."""
        stats = {
            "period": "recent",
            "total_downloads": 1500,
            "fetched_at": "2026-04-28T10:00:00",
            "packages": {
                "llm-routing": {
                    "downloads": 1000,
                    "description": "New package (current)",
                },
                "claude-code-llm-router": {
                    "downloads": 500,
                    "description": "Legacy package (deprecated)",
                },
            },
        }

        result = PyPIStats.format_stats(stats, format_type="text")

        assert "Combined Download Statistics" in result
        assert "1,500" in result
        assert "llm-routing" in result
        assert "claude-code-llm-router" in result
        assert "66.7%" in result  # 1000/1500
        assert "33.3%" in result  # 500/1500

    def test_format_stats_markdown(self):
        """Test formatting stats as markdown."""
        stats = {
            "period": "recent",
            "total_downloads": 1500,
            "fetched_at": "2026-04-28T10:00:00",
            "packages": {
                "llm-routing": {
                    "downloads": 1000,
                    "description": "New package (current)",
                },
                "claude-code-llm-router": {
                    "downloads": 500,
                    "description": "Legacy package (deprecated)",
                },
            },
        }

        result = PyPIStats.format_stats(stats, format_type="markdown")

        assert "## Combined Download Statistics" in result
        assert "**Total Downloads**: 1,500" in result
        assert "**Period**: recent" in result
        assert "**llm-routing**" in result
        assert "**claude-code-llm-router**" in result

    def test_format_stats_json(self):
        """Test formatting stats as JSON."""
        stats = {
            "period": "recent",
            "total_downloads": 1500,
            "fetched_at": "2026-04-28T10:00:00",
            "packages": {
                "llm-routing": {
                    "downloads": 1000,
                    "description": "New package (current)",
                },
            },
        }

        result = PyPIStats.format_stats(stats, format_type="json")

        import json
        parsed = json.loads(result)
        assert parsed["total_downloads"] == 1500
        assert parsed["period"] == "recent"

    def test_format_stats_zero_downloads(self):
        """Test formatting when no downloads exist."""
        stats = {
            "period": "recent",
            "total_downloads": 0,
            "fetched_at": "2026-04-28T10:00:00",
            "packages": {
                "llm-routing": {
                    "downloads": 0,
                    "description": "New package",
                },
                "claude-code-llm-router": {
                    "downloads": 0,
                    "description": "Legacy package",
                },
            },
        }

        result = PyPIStats.format_stats(stats, format_type="text")

        assert "0" in result
        assert "0%" in result
