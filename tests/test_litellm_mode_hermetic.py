"""Regression check for the 2026-11-22 test-order pollution (see the repo-root conftest.py).

A module-level ``import litellm`` in any test file runs litellm's load_dotenv() unless
LITELLM_MODE is PROD, which can copy an ancestor .env into os.environ for the rest of the
pytest process. The root conftest sets LITELLM_MODE=PROD before collection; this fails if
that line is dropped.
"""

import os


def test_litellm_mode_is_prod_for_the_suite():
    assert os.environ.get("LITELLM_MODE") == "PROD"
