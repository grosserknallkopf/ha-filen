"""Test helpers from whichever test package matches the installed Home Assistant.

HA 2026.11+ ships `pytest-homeassistant`; older versions use
`pytest-homeassistant-custom-component`.
"""

try:
    from pytest_homeassistant.common import MockConfigEntry
except ImportError:  # HA before 2026.11
    from pytest_homeassistant_custom_component.common import MockConfigEntry

__all__ = ["MockConfigEntry"]