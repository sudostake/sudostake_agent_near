import os
import sys
import pytest
from unittest.mock import MagicMock

# Ensure project root, src, and jobs are importable for tests
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "src"))
sys.path.insert(0, os.path.join(ROOT_DIR, "jobs"))

from tools.context import set_context

@pytest.fixture
def mock_setup():
    """Provide a mocked env and near client, and register them in tools.context."""
    env = MagicMock()
    near = MagicMock()
    set_context(env=env, near=near)
    return (env, near)
