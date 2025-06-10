import sys
import os
import pytest
from unittest.mock import MagicMock

# Make both the project-root *and* src/ importable
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "src"))  

from tools import ( # type: ignore[import]
    context,
)


def make_dummy_resp(json_body):
    """Minimal stub mimicking requests.Response for our needs."""
    class DummyResp:
        def raise_for_status(self):          # no-op ⇢ 200 OK
            pass
        def json(self):
            return json_body
    return DummyResp()

@pytest.fixture
def mock_setup():
    """Initialize mock environment, logger, and near — then set context."""
    
    env = MagicMock()
    near = MagicMock()

    # Set the context globally for tools
    context.set_context(env=env, near=near)

    return (env, near)