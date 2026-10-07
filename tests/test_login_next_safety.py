"""The staff login's ?next= must never lead off the site."""

import pytest

from app.rota_login import _safe_next


@pytest.mark.parametrize("target", [
    "/\t/evil.example", "/\n/evil.example", "/\r/evil.example",
    "/\t\\evil.example", "/ok\\..\\evil", "/\x00/evil.example",
])
def test_control_characters_and_backslashes_cannot_smuggle_another_site(target):
    """Browsers strip tabs and newlines out of a URL, so "/<tab>/evil.example"
    is "//evil.example" by the time it is followed (found 2026-10-07)."""
    assert _safe_next(target) is False


def test_an_ordinary_local_path_is_still_accepted():
    assert _safe_next("/v/testvenue/me") is True
