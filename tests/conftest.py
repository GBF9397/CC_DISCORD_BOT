import sys
from pathlib import Path

import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.mock_lmstudio import MockLMStudio  # noqa: E402


@pytest_asyncio.fixture
async def mock_api():
    server = MockLMStudio()
    server.base_url = await server.start()
    yield server
    await server.stop()
