"""Shared deterministic-test safety fixtures."""

from __future__ import annotations

import os

import pytest
import requests


@pytest.fixture(autouse=True)
def block_live_http_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail every offline test that attempts unmocked HTTP."""

    if os.getenv("RUN_LIVE_PROVIDER_TESTS") == "1":
        return

    def blocked_request(
        _session: requests.Session,
        method: str,
        url: str,
        **_kwargs: object,
    ) -> object:
        raise AssertionError(
            "Automated tests must not call live HTTP services: "
            f"{method.upper()} {url}"
        )

    monkeypatch.setattr(
        requests.sessions.Session,
        "request",
        blocked_request,
    )
