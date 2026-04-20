"""Tests for configurable timeout values."""

import os

from llm_router.timeout_config import (
    get_timeout_config,
    request_timeout,
    media_request_timeout,
    codex_timeout,
    subprocess_timeout,
    http_timeout,
    benchmark_timeout,
    reset_cache,
)


class TestTimeoutConfigDefaults:
    """Test default timeout values."""

    def setup_method(self):
        """Reset cache before each test."""
        reset_cache()
        # Clear any timeout env vars
        for var in [
            "LLM_ROUTER_REQUEST_TIMEOUT",
            "LLM_ROUTER_MEDIA_REQUEST_TIMEOUT",
            "LLM_ROUTER_CODEX_TIMEOUT",
            "LLM_ROUTER_SUBPROCESS_TIMEOUT",
            "LLM_ROUTER_HTTP_TIMEOUT",
            "LLM_ROUTER_BENCHMARK_TIMEOUT",
        ]:
            os.environ.pop(var, None)
        reset_cache()

    def test_defaults(self):
        """Verify all default timeout values."""
        config = get_timeout_config()
        assert config["request_timeout"] == 120
        assert config["media_request_timeout"] == 600
        assert config["codex_timeout"] == 300
        assert config["subprocess_timeout"] == 15
        assert config["http_timeout"] == 10
        assert config["benchmark_timeout"] == 30

    def test_request_timeout_default(self):
        """request_timeout() returns default 120s."""
        assert request_timeout() == 120

    def test_media_request_timeout_default(self):
        """media_request_timeout() returns default 600s."""
        assert media_request_timeout() == 600

    def test_codex_timeout_default(self):
        """codex_timeout() returns default 300s."""
        assert codex_timeout() == 300

    def test_subprocess_timeout_default(self):
        """subprocess_timeout() returns default 15s."""
        assert subprocess_timeout() == 15

    def test_http_timeout_default(self):
        """http_timeout() returns default 10s."""
        assert http_timeout() == 10

    def test_benchmark_timeout_default(self):
        """benchmark_timeout() returns default 30s."""
        assert benchmark_timeout() == 30


class TestTimeoutConfigEnvVars:
    """Test timeout configuration via environment variables."""

    def setup_method(self):
        """Reset cache before each test."""
        reset_cache()
        # Clear any timeout env vars
        for var in [
            "LLM_ROUTER_REQUEST_TIMEOUT",
            "LLM_ROUTER_MEDIA_REQUEST_TIMEOUT",
            "LLM_ROUTER_CODEX_TIMEOUT",
            "LLM_ROUTER_SUBPROCESS_TIMEOUT",
            "LLM_ROUTER_HTTP_TIMEOUT",
            "LLM_ROUTER_BENCHMARK_TIMEOUT",
        ]:
            os.environ.pop(var, None)

    def teardown_method(self):
        """Clean up env vars after each test."""
        for var in [
            "LLM_ROUTER_REQUEST_TIMEOUT",
            "LLM_ROUTER_MEDIA_REQUEST_TIMEOUT",
            "LLM_ROUTER_CODEX_TIMEOUT",
            "LLM_ROUTER_SUBPROCESS_TIMEOUT",
            "LLM_ROUTER_HTTP_TIMEOUT",
            "LLM_ROUTER_BENCHMARK_TIMEOUT",
        ]:
            os.environ.pop(var, None)
        reset_cache()

    def test_request_timeout_env(self):
        """LLM_ROUTER_REQUEST_TIMEOUT overrides default."""
        os.environ["LLM_ROUTER_REQUEST_TIMEOUT"] = "240"
        reset_cache()
        assert request_timeout() == 240

    def test_media_request_timeout_env(self):
        """LLM_ROUTER_MEDIA_REQUEST_TIMEOUT overrides default."""
        os.environ["LLM_ROUTER_MEDIA_REQUEST_TIMEOUT"] = "1200"
        reset_cache()
        assert media_request_timeout() == 1200

    def test_codex_timeout_env(self):
        """LLM_ROUTER_CODEX_TIMEOUT overrides default."""
        os.environ["LLM_ROUTER_CODEX_TIMEOUT"] = "600"
        reset_cache()
        assert codex_timeout() == 600

    def test_subprocess_timeout_env(self):
        """LLM_ROUTER_SUBPROCESS_TIMEOUT overrides default."""
        os.environ["LLM_ROUTER_SUBPROCESS_TIMEOUT"] = "30"
        reset_cache()
        assert subprocess_timeout() == 30

    def test_http_timeout_env(self):
        """LLM_ROUTER_HTTP_TIMEOUT overrides default."""
        os.environ["LLM_ROUTER_HTTP_TIMEOUT"] = "20"
        reset_cache()
        assert http_timeout() == 20

    def test_benchmark_timeout_env(self):
        """LLM_ROUTER_BENCHMARK_TIMEOUT overrides default."""
        os.environ["LLM_ROUTER_BENCHMARK_TIMEOUT"] = "60"
        reset_cache()
        assert benchmark_timeout() == 60


class TestTimeoutConfigValidation:
    """Test timeout value validation."""

    def setup_method(self):
        """Reset cache before each test."""
        reset_cache()
        # Clear any timeout env vars
        for var in [
            "LLM_ROUTER_REQUEST_TIMEOUT",
            "LLM_ROUTER_MEDIA_REQUEST_TIMEOUT",
            "LLM_ROUTER_CODEX_TIMEOUT",
            "LLM_ROUTER_SUBPROCESS_TIMEOUT",
            "LLM_ROUTER_HTTP_TIMEOUT",
            "LLM_ROUTER_BENCHMARK_TIMEOUT",
        ]:
            os.environ.pop(var, None)

    def teardown_method(self):
        """Clean up env vars after each test."""
        for var in [
            "LLM_ROUTER_REQUEST_TIMEOUT",
            "LLM_ROUTER_MEDIA_REQUEST_TIMEOUT",
            "LLM_ROUTER_CODEX_TIMEOUT",
            "LLM_ROUTER_SUBPROCESS_TIMEOUT",
            "LLM_ROUTER_HTTP_TIMEOUT",
            "LLM_ROUTER_BENCHMARK_TIMEOUT",
        ]:
            os.environ.pop(var, None)
        reset_cache()

    def test_invalid_timeout_non_numeric(self):
        """Invalid non-numeric value falls back to default."""
        os.environ["LLM_ROUTER_REQUEST_TIMEOUT"] = "not_a_number"
        reset_cache()
        assert request_timeout() == 120  # Falls back to default

    def test_invalid_timeout_zero(self):
        """Zero timeout falls back to default."""
        os.environ["LLM_ROUTER_REQUEST_TIMEOUT"] = "0"
        reset_cache()
        assert request_timeout() == 120  # Falls back to default

    def test_invalid_timeout_negative(self):
        """Negative timeout falls back to default."""
        os.environ["LLM_ROUTER_REQUEST_TIMEOUT"] = "-10"
        reset_cache()
        assert request_timeout() == 120  # Falls back to default

    def test_valid_timeout_large_value(self):
        """Large but valid timeout value is accepted."""
        os.environ["LLM_ROUTER_MEDIA_REQUEST_TIMEOUT"] = "3600"
        reset_cache()
        assert media_request_timeout() == 3600

    def test_valid_timeout_one(self):
        """Timeout of 1 second is valid."""
        os.environ["LLM_ROUTER_HTTP_TIMEOUT"] = "1"
        reset_cache()
        assert http_timeout() == 1


class TestTimeoutConfigCaching:
    """Test that timeout config is cached properly."""

    def setup_method(self):
        """Reset cache before each test."""
        reset_cache()
        # Clear any timeout env vars
        for var in [
            "LLM_ROUTER_REQUEST_TIMEOUT",
            "LLM_ROUTER_MEDIA_REQUEST_TIMEOUT",
            "LLM_ROUTER_CODEX_TIMEOUT",
            "LLM_ROUTER_SUBPROCESS_TIMEOUT",
            "LLM_ROUTER_HTTP_TIMEOUT",
            "LLM_ROUTER_BENCHMARK_TIMEOUT",
        ]:
            os.environ.pop(var, None)

    def teardown_method(self):
        """Clean up env vars after each test."""
        for var in [
            "LLM_ROUTER_REQUEST_TIMEOUT",
            "LLM_ROUTER_MEDIA_REQUEST_TIMEOUT",
            "LLM_ROUTER_CODEX_TIMEOUT",
            "LLM_ROUTER_SUBPROCESS_TIMEOUT",
            "LLM_ROUTER_HTTP_TIMEOUT",
            "LLM_ROUTER_BENCHMARK_TIMEOUT",
        ]:
            os.environ.pop(var, None)
        reset_cache()

    def test_config_is_cached(self):
        """get_timeout_config() returns cached result."""
        config1 = get_timeout_config()
        config2 = get_timeout_config()
        assert config1 is config2  # Same object reference (cached)

    def test_env_var_change_requires_reset(self):
        """Changing env var requires reset_cache() to take effect."""
        os.environ["LLM_ROUTER_REQUEST_TIMEOUT"] = "240"
        reset_cache()
        assert request_timeout() == 240

        # Change env var without reset
        os.environ["LLM_ROUTER_REQUEST_TIMEOUT"] = "360"
        assert request_timeout() == 240  # Still cached

        # Reset and verify new value is used
        reset_cache()
        assert request_timeout() == 360

    def test_cache_is_independent_per_module(self):
        """Each timeout function accesses same cached config."""
        os.environ["LLM_ROUTER_REQUEST_TIMEOUT"] = "240"
        os.environ["LLM_ROUTER_CODEX_TIMEOUT"] = "600"
        reset_cache()

        # Both should use same cached config
        config = get_timeout_config()
        assert config["request_timeout"] == 240
        assert config["codex_timeout"] == 600
        assert request_timeout() == 240
        assert codex_timeout() == 600
