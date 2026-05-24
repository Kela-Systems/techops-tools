"""Shared test fixtures — mock gRPC channel and stubs."""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_channel():
    return MagicMock()
