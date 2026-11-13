"""Keep automated tests independent of the developer's live tracking tunnel."""

import pytest

import mail_tracking


@pytest.fixture(autouse=True)
def isolated_tracking_connection(monkeypatch):
    monkeypatch.delenv("TRACKING_PUBLIC_BASE_URL", raising=False)
    mail_tracking._HEALTH_CACHE.clear()
    yield
    mail_tracking._HEALTH_CACHE.clear()
