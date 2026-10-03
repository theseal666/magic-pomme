"""Tests for group-refresh advice.

"Log out and back in" is the reflex answer for new group membership, and it is
wrong whenever systemd user lingering is on: the user manager survives logout
and keeps its boot-time group set, so every freshly spawned desktop process
inherits the stale groups. Telling someone to log out then sends them round a
loop that cannot succeed.
"""

from pathlib import Path

from magic_pomme import session


class TestLingerDetection:
    def test_linger_file_is_authoritative(self, monkeypatch, tmp_path):
        (tmp_path / "someone").touch()
        monkeypatch.setattr(session, "LINGER_DIR", tmp_path)
        assert session.linger_enabled("someone")

    def test_absent_linger_file_falls_through_to_loginctl(self, monkeypatch, tmp_path):
        monkeypatch.setattr(session, "LINGER_DIR", tmp_path)
        monkeypatch.setattr(session.subprocess, "run",
                            lambda *a, **k: type("R", (), {"stdout": "Linger=no\n"})())
        assert not session.linger_enabled("someone")

    def test_loginctl_reporting_yes_counts(self, monkeypatch, tmp_path):
        monkeypatch.setattr(session, "LINGER_DIR", tmp_path)
        monkeypatch.setattr(session.subprocess, "run",
                            lambda *a, **k: type("R", (), {"stdout": "Linger=yes\n"})())
        assert session.linger_enabled("someone")

    def test_missing_loginctl_is_not_fatal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(session, "LINGER_DIR", tmp_path)
        def boom(*a, **k):
            raise FileNotFoundError
        monkeypatch.setattr(session.subprocess, "run", boom)
        assert not session.linger_enabled("someone")

    def test_no_user_is_not_fatal(self, monkeypatch):
        monkeypatch.setattr(session, "current_user", lambda: "")
        assert not session.linger_enabled()


class TestRefreshHint:
    def test_lingering_says_reboot_not_logout(self, monkeypatch):
        monkeypatch.setattr(session, "linger_enabled", lambda user=None: True)
        hint = session.refresh_hint()
        assert "reboot" in hint
        assert "logging out is not enough" in hint

    def test_without_lingering_logout_is_enough(self, monkeypatch):
        monkeypatch.setattr(session, "linger_enabled", lambda user=None: False)
        hint = session.refresh_hint()
        assert "log out" in hint
        assert "reboot" not in hint


class TestRefreshAction:
    def test_verb_only_for_inline_use(self, monkeypatch):
        monkeypatch.setattr(session, "linger_enabled", lambda user=None: True)
        assert session.refresh_action() == "reboot"
        monkeypatch.setattr(session, "linger_enabled", lambda user=None: False)
        assert session.refresh_action() == "log out and back in"
