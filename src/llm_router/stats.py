"""Download statistics aggregation for llm-routing and claude-code-llm-router.

Fetches combined download stats from PyPI and format for display.
"""

import json
import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

PACKAGES = {
    "llm-routing": "New package (current)",
    "claude-code-llm-router": "Legacy package (deprecated)",
}


class PyPIStats:
    """Fetch and aggregate download statistics from PyPI."""

    BASE_URL = "https://pypistats.org/api/packages"

    @classmethod
    def get_package_stats(cls, package_name: str, period: str = "recent") -> dict:
        """Fetch stats for a single package.

        Args:
            package_name: PyPI package name
            period: 'recent' (last 6 weeks), 'last_month', 'last_quarter', 'all_time'

        Returns:
            Dict with downloads and metadata
        """
        try:
            url = f"{cls.BASE_URL}/{package_name}/{period}"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Failed to fetch stats for {package_name}: {e}")
            return {}

    @classmethod
    def get_combined_stats(cls, period: str = "recent") -> dict:
        """Get combined download stats for all packages.

        Args:
            period: 'recent' (last 6 weeks), 'last_month', 'last_quarter', 'all_time'

        Returns:
            Dict with combined stats and breakdown
        """
        combined = {
            "period": period,
            "total_downloads": 0,
            "packages": {},
            "fetched_at": datetime.utcnow().isoformat(),
        }

        for package_name, description in PACKAGES.items():
            stats = cls.get_package_stats(package_name, period)
            if stats and "data" in stats:
                data = stats["data"]
                downloads = data.get(f"last_{period.split('_')[-1]}", 0)
                combined["packages"][package_name] = {
                    "downloads": downloads,
                    "description": description,
                }
                combined["total_downloads"] += downloads
            else:
                combined["packages"][package_name] = {
                    "downloads": 0,
                    "description": description,
                }

        return combined

    @classmethod
    def format_stats(cls, stats: dict, format_type: str = "text") -> str:
        """Format stats for display.

        Args:
            stats: Stats dict from get_combined_stats()
            format_type: 'text', 'markdown', 'json'

        Returns:
            Formatted string
        """
        if format_type == "json":
            return json.dumps(stats, indent=2)

        if format_type == "markdown":
            total = stats["total_downloads"]
            lines = [
                "## Combined Download Statistics",
                "",
                f"**Total Downloads**: {total:,}",
                f"**Period**: {stats['period']}",
                f"**Updated**: {stats['fetched_at']}",
                "",
                "### Breakdown",
                "",
            ]
            for package_name, pkg_stats in stats["packages"].items():
                downloads = pkg_stats["downloads"]
                description = pkg_stats["description"]
                pct = (
                    f" ({downloads*100/total:.1f}%)"
                    if total > 0
                    else " (0%)"
                )
                lines.append(
                    f"- **{package_name}** ({description}): {downloads:,}{pct}"
                )
            return "\n".join(lines)

        # text format (default)
        total = stats["total_downloads"]
        lines = [
            "Combined Download Statistics",
            "=" * 40,
            f"Total Downloads: {total:,}",
            f"Period: {stats['period']}",
            f"Updated: {stats['fetched_at']}",
            "",
            "Breakdown:",
            "-" * 40,
        ]
        for package_name, pkg_stats in stats["packages"].items():
            downloads = pkg_stats["downloads"]
            description = pkg_stats["description"]
            pct = (
                f" ({downloads*100/total:.1f}%)"
                if total > 0
                else " (0%)"
            )
            lines.append(f"{package_name}: {downloads:,}{pct}")
            lines.append(f"  {description}")

        return "\n".join(lines)


def get_downloads(period: str = "recent") -> int:
    """Get combined total downloads (convenience function).

    Args:
        period: 'recent', 'last_month', 'last_quarter', 'all_time'

    Returns:
        Total downloads across all packages
    """
    stats = PyPIStats.get_combined_stats(period)
    return stats["total_downloads"]


def print_stats(period: str = "recent", format_type: str = "text"):
    """Print stats to stdout.

    Args:
        period: 'recent', 'last_month', 'last_quarter', 'all_time'
        format_type: 'text', 'markdown', 'json'
    """
    stats = PyPIStats.get_combined_stats(period)
    formatted = PyPIStats.format_stats(stats, format_type)
    print(formatted)
