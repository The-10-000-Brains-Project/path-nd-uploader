"""The console entry point (main) should turn the common, actionable Google
Cloud errors into a plain one-line message with the fix — not the giant rich
traceback Typer renders by default. A real expired-auth run produced a
~100-line wall that buried the actual cause ("Reauthentication is needed").
"""

from unittest.mock import patch

import pytest
from google.api_core import exceptions as gapi_exceptions
from google.auth import exceptions as gauth_exceptions

from pathnd_uploader import cli


def _run_main_with(exc):
    with patch("pathnd_uploader.cli.app", side_effect=exc):
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
    return exit_info.value


def test_expired_auth_becomes_friendly_message(capsys):
    code = _run_main_with(gauth_exceptions.RefreshError("Reauthentication is needed."))
    err = capsys.readouterr().err
    assert code.code == 2
    assert "expired" in err.lower()
    assert "gcloud auth application-default login" in err
    assert "Traceback" not in err  # no wall of stack frames


def test_missing_credentials_becomes_friendly_message(capsys):
    code = _run_main_with(gauth_exceptions.DefaultCredentialsError("no creds"))
    err = capsys.readouterr().err
    assert code.code == 2
    assert "gcloud auth application-default login" in err


def test_permission_denied_becomes_friendly_message(capsys):
    code = _run_main_with(gapi_exceptions.Forbidden("caller lacks storage.objects.list"))
    err = capsys.readouterr().err
    assert code.code == 2
    assert "denied access" in err.lower()
    assert "permission" in err.lower()


def test_not_found_becomes_friendly_message(capsys):
    code = _run_main_with(gapi_exceptions.NotFound("bucket nope does not exist"))
    err = capsys.readouterr().err
    assert code.code == 2
    assert "couldn't find" in err.lower()


def test_normal_exit_is_not_swallowed():
    # A clean SystemExit(0) from the app (Click's normal completion) must
    # pass straight through, not get remapped to the error exit code.
    with patch("pathnd_uploader.cli.app", side_effect=SystemExit(0)):
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
    assert exit_info.value.code == 0


def test_unexpected_error_still_propagates():
    # We only befriend known operational errors; a genuine bug must still
    # surface (as a normal exception), not be hidden behind a tidy message.
    with patch("pathnd_uploader.cli.app", side_effect=ValueError("a real bug")):
        with pytest.raises(ValueError, match="a real bug"):
            cli.main()
