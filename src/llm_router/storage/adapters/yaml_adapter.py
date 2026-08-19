"""YAML adapter for configuration storage (~/.llm-router/config.yaml).

Handles user-level configuration with version tracking for migrations.
"""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


class YamlAdapter:
    """Persist configuration as YAML files with version tracking."""

    def __init__(self, filepath: Path):
        """Initialize adapter for a YAML file.

        Args:
            filepath: Path to .yaml file (e.g., ~/.llm-router/config.yaml)
        """
        if yaml is None:
            raise ImportError("PyYAML is required for YamlAdapter")
        self.filepath = filepath
        self.filepath.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict | None:
        """Read YAML configuration file.

        Returns:
            Config dict with version field, or None if file missing/unreadable.
        """
        if not self.filepath.exists():
            return None

        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return data if isinstance(data, dict) else None
        except (OSError, yaml.YAMLError, ValueError):
            return None

    def write(self, data: dict, atomic: bool = True) -> None:
        """Write config to YAML file.

        Args:
            data:   Config dict (must contain 'version' field)
            atomic: If True, use temp file + rename

        Raises:
            OSError: On write failure
            ValueError: If 'version' field missing
        """
        if "version" not in data:
            raise ValueError("Config must include 'version' field")

        self.filepath.parent.mkdir(parents=True, exist_ok=True)

        yaml_str = yaml.dump(data, default_flow_style=False, sort_keys=False)

        if atomic:
            # Atomic write: temp file + rename
            import os
            tmp = self.filepath.with_suffix(".yaml.tmp")
            tmp.write_text(yaml_str, encoding="utf-8")
            os.replace(tmp, self.filepath)
        else:
            # Direct write
            self.filepath.write_text(yaml_str, encoding="utf-8")

        # Restrict permissions (config may contain secrets)
        self.filepath.chmod(0o600)

    def append(self, data: dict) -> None:
        """Not supported for YAML files."""
        raise NotImplementedError("YAML adapter does not support append operations")

    def verify_integrity(self) -> tuple[bool, str]:
        """YAML files don't have integrity checks."""
        return True, "n/a"

    def merge(self, new_data: dict) -> dict:
        """Merge new config with existing without data loss.

        Useful for config migrations.

        Args:
            new_data: New config to merge in

        Returns:
            Merged config dict
        """
        existing = self.read() or {}
        # Simple merge: new keys added, old keys preserved if not in new
        merged = {**existing, **new_data}
        return merged
