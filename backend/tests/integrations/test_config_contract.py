from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.provider._base import AuthRequirement
from src.platform.synchronize.config_contract import validate_structured_config


def _oauth_spec():
    return SimpleNamespace(auth=AuthRequirement.OAUTH)


def test_google_calendar_config_requires_calendar_ids():
    with pytest.raises(ValueError, match="config.options.calendar_ids is required"):
        validate_structured_config(
            "google_calendar",
            _oauth_spec(),
            {
                "source": {
                    "provider": "google_calendar",
                    "resource_type": "calendar_set",
                    "resource_id": "primary",
                    "resource_name": "Calendar",
                },
                "options": {},
            },
        )


def test_google_calendar_config_accepts_calendar_ids_in_options():
    validate_structured_config(
        "google_calendar",
        _oauth_spec(),
        {
            "source": {
                "provider": "google_calendar",
                "resource_type": "calendar_set",
                "resource_id": "primary",
                "resource_name": "Calendar",
            },
            "options": {"calendar_ids": ["primary"]},
        },
    )
