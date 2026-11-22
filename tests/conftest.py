"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Start a recorder (Phase Finder depends on it), then allow custom integrations.

    recorder_mock must come first: it has to be set up before the hass fixture.
    """
    yield
