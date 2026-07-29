"""Pytest fixtures for the hosted-agent app tests.

Adds the app directory to sys.path so the flat-module imports (``import
config``) resolve, and provides a config + fake token provider.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from config import HostedAgentConfig  # noqa: E402


@pytest.fixture
def cfg() -> HostedAgentConfig:
    return HostedAgentConfig(
        auth_mode="local",
        foundry_project_endpoint="https://res.services.ai.azure.com/api/projects/proj",
        hosted_agent_name="my-agent",
        model="gpt-4.1",
        container_mode="auto",
        request_timeout_seconds=5.0,
        mcp_server_label="mcp",
    )


@pytest.fixture
def token_provider():
    return lambda scope: "fake-token"
