"""Tests for the tray applet's error boundary.

PyQt6 does not merely log an exception that escapes a slot: it calls qFatal,
which aborts the process. A kwriteconfig6 exit status therefore killed the
whole applet, core dump and all, when a pointer speed failed to write. Any
handler reachable from the menu has to come back with a message instead.
"""

import subprocess

import pytest

pytest.importorskip("PyQt6")

from magic_pomme.tray import guarded          # noqa: E402


class TestGuarded:
    def test_a_subprocess_failure_comes_back_as_a_message(self):
        def boom():
            raise subprocess.CalledProcessError(1, ["kwriteconfig6"])
        assert "kwriteconfig6" in guarded(boom)

    def test_subprocess_errors_are_not_oserrors_which_is_the_whole_point(self):
        assert not issubclass(subprocess.SubprocessError, OSError)

    def test_success_returns_nothing(self):
        assert guarded(lambda: None) is None

    def test_an_exception_with_no_message_still_names_itself(self):
        def boom():
            raise RuntimeError
        assert guarded(boom) == "RuntimeError"
