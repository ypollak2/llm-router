"""Tests for thread-safe config singleton."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch


from llm_router.config import RouterConfig, _config_lock, get_config


class TestConfigSingleton:
    """Test thread-safe config singleton."""

    def test_returns_same_instance(self) -> None:
        """Multiple calls return the same instance."""
        # Reset config for testing
        import llm_router.config
        llm_router.config._config = None

        config1 = get_config()
        config2 = get_config()
        assert config1 is config2

    def test_thread_safe_initialization(self) -> None:
        """Concurrent initialization creates only one instance."""
        # Reset config for testing
        import llm_router.config
        llm_router.config._config = None

        instances = []
        lock = threading.Lock()

        def get_and_store():
            """Call get_config and store the instance."""
            config = get_config()
            with lock:
                instances.append(config)

        # Launch multiple threads to call get_config concurrently
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(get_and_store) for _ in range(10)]
            for future in as_completed(futures):
                future.result()  # Wait for completion

        # All instances should be the same object
        assert len(instances) == 10
        first_instance = instances[0]
        for instance in instances[1:]:
            assert instance is first_instance

    def test_concurrent_access_consistency(self) -> None:
        """Concurrent access to config returns consistent state."""
        # Reset config for testing
        import llm_router.config
        llm_router.config._config = None

        results = []
        lock = threading.Lock()

        def access_config():
            """Access config multiple times."""
            configs = []
            for _ in range(100):
                config = get_config()
                configs.append(config)
            with lock:
                results.append(configs)

        # Launch multiple threads
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(access_config) for _ in range(5)]
            for future in as_completed(futures):
                future.result()

        # All should access the same config instance
        first_config = results[0][0]
        for thread_configs in results:
            for config in thread_configs:
                assert config is first_config

    def test_lock_prevents_double_initialization(self) -> None:
        """Lock prevents multiple threads from creating multiple instances."""
        import llm_router.config
        llm_router.config._config = None

        call_count = 0
        original_init = RouterConfig.__init__

        def counting_init(self):
            """RouterConfig.__init__ that counts calls."""
            nonlocal call_count
            call_count += 1
            original_init(self)

        # Patch RouterConfig.__init__ to count initializations
        with patch.object(RouterConfig, "__init__", counting_init):
            # Concurrent calls should result in only one initialization
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(get_config) for _ in range(10)]
                for future in as_completed(futures):
                    future.result()

            # Should only initialize once
            assert call_count == 1

    def test_lock_is_acquired_and_released(self) -> None:
        """Lock is properly acquired and released."""
        import llm_router.config
        llm_router.config._config = None

        # Lock should not be held after get_config returns
        assert not _config_lock.locked()
        get_config()
        # Lock should be released after initialization
        assert not _config_lock.locked()

        # Second call should also not hold lock
        get_config()
        assert not _config_lock.locked()

    def test_double_check_pattern(self) -> None:
        """Implementation uses double-check pattern correctly."""
        import llm_router.config
        llm_router.config._config = None

        init_count = 0
        original_init = RouterConfig.__init__

        def tracking_init(self):
            """Track initialization calls."""
            nonlocal init_count
            init_count += 1
            original_init(self)

        with patch.object(RouterConfig, "__init__", tracking_init):
            # Multiple rapid calls
            for _ in range(5):
                get_config()

            # Should only initialize once thanks to double-check pattern
            assert init_count == 1

    def test_no_deadlock_on_repeated_calls(self) -> None:
        """Repeated calls don't cause deadlock."""
        import llm_router.config
        llm_router.config._config = None

        # This should complete without deadlock
        for _ in range(100):
            config = get_config()
            assert config is not None


class TestConfigIntegration:
    """Test config works correctly after initialization."""

    def test_config_properties_accessible(self) -> None:
        """Config properties are accessible after concurrent initialization."""
        import llm_router.config
        llm_router.config._config = None

        configs = []

        def get_and_check():
            """Get config and verify properties."""
            config = get_config()
            # These should not raise
            _ = config.openai_api_key
            _ = config.gemini_api_key
            configs.append(config)

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(get_and_check) for _ in range(5)]
            for future in as_completed(futures):
                future.result()

        # All should be the same instance
        first = configs[0]
        for config in configs[1:]:
            assert config is first
