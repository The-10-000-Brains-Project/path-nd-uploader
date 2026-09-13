"""Tests for retry.py — classifying and retrying transient GCS/network
errors, so a network hiccup while checking a file doesn't get reported as a
finding about that file. time.sleep is mocked throughout so these stay fast
regardless of the configured backoff durations.
"""

from unittest.mock import patch

import google.api_core.exceptions as gax_exceptions
import pytest

from pathnd_uploader.retry import is_transient, retry_transient


def test_is_transient_recognizes_known_transient_types():
    assert is_transient(gax_exceptions.RetryError("x", None))
    assert is_transient(gax_exceptions.ServiceUnavailable("x"))
    assert is_transient(gax_exceptions.TooManyRequests("x"))
    assert is_transient(ConnectionError("x"))
    assert is_transient(TimeoutError("x"))


def test_is_transient_rejects_unrelated_exceptions():
    assert not is_transient(ValueError("bad input"))
    assert not is_transient(RuntimeError("something else"))
    assert not is_transient(KeyError("missing"))


def test_retry_transient_succeeds_after_transient_failures():
    calls = []

    @retry_transient
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise gax_exceptions.ServiceUnavailable("temporarily down")
        return "ok"

    with patch("time.sleep"):
        result = flaky()

    assert result == "ok"
    assert len(calls) == 3


def test_retry_transient_gives_up_after_exhausting_attempts():
    calls = []

    @retry_transient
    def always_flaky():
        calls.append(1)
        raise gax_exceptions.ServiceUnavailable("persistently down")

    with patch("time.sleep"), pytest.raises(gax_exceptions.ServiceUnavailable):
        always_flaky()

    assert len(calls) == 3  # stop_after_attempt(3)


def test_retry_transient_does_not_retry_non_transient_exceptions():
    calls = []

    @retry_transient
    def bad_input():
        calls.append(1)
        raise ValueError("not a network issue")

    with pytest.raises(ValueError, match="not a network issue"):
        bad_input()

    assert len(calls) == 1  # no retry attempted at all
