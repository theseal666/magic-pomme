"""Tests for the profile engine.

The engine's whole job is being reversible, so that is what is tested:
a profile that cannot be undone is worse than no profile, because several of
its targets are shared files full of the user's own settings.
"""

from pathlib import Path

import pytest

from magic_pomme import profiles


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Never touch the real state directory."""
    monkeypatch.setattr(profiles, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(profiles, "STATE_FILE", tmp_path / "state" / "state.json")
    monkeypatch.setattr(profiles, "BACKUP_DIR", tmp_path / "state" / "backups")
    return tmp_path


class TestNormaliseShortcut:
    def test_modifier_order_is_canonicalised(self):
        assert (profiles.normalise_shortcut("Ctrl+Meta+Q")
                == profiles.normalise_shortcut("Meta+Ctrl+Q"))

    def test_kde_order_is_meta_ctrl_alt_shift(self):
        assert profiles.normalise_shortcut("Shift+Alt+Ctrl+Meta+X") == "Meta+Ctrl+Alt+Shift+X"

    def test_alternatives_are_each_normalised(self):
        assert (profiles.normalise_shortcut("Ctrl+Tab\\tShift+Ctrl+Tab")
                == "Ctrl+Tab\\tCtrl+Shift+Tab")

    def test_plain_key_untouched(self):
        assert profiles.normalise_shortcut("none") == "none"

    def test_distinct_shortcuts_stay_distinct(self):
        assert (profiles.normalise_shortcut("Meta+Up")
                != profiles.normalise_shortcut("Meta+Down"))


class TestState:
    def test_roundtrip(self):
        state = profiles.State()
        state.record("thing", {"previous": {"a": 1}})
        state.profile = "mac"
        state.save()
        assert profiles.State.load().recall("thing")["previous"] == {"a": 1}
        assert profiles.State.load().profile == "mac"

    def test_forget_removes(self):
        state = profiles.State()
        state.record("thing", {})
        state.forget("thing")
        assert state.recall("thing") is None

    def test_missing_state_file_is_empty_not_an_error(self):
        assert profiles.State.load().profile is None

    def test_corrupt_state_does_not_raise(self, isolated_state):
        profiles.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        profiles.STATE_FILE.write_text("{ not json")
        assert profiles.State.load().profile is None


class TestManagedFileReversibility:
    def _action(self, path: Path):
        return profiles.ManagedFile(
            key="t", description="d", path=path, content="new content\n")

    def test_existing_file_is_restored_exactly(self, tmp_path):
        target = tmp_path / "conf"
        target.write_text("original\n")
        action, state = self._action(target), profiles.State()

        action.apply(state)
        assert target.read_text() == "new content\n"
        action.revert(state)
        assert target.read_text() == "original\n"

    def test_absent_file_is_removed_again(self, tmp_path):
        target = tmp_path / "sub" / "conf"
        action, state = self._action(target), profiles.State()

        action.apply(state)
        assert target.exists()
        action.revert(state)
        assert not target.exists(), "a file we created must not be left behind"

    def test_reapplying_keeps_the_original_backup(self, tmp_path):
        target = tmp_path / "conf"
        target.write_text("original\n")
        action, state = self._action(target), profiles.State()

        action.apply(state)
        action.apply(state)      # must not back up our own output
        action.revert(state)
        assert target.read_text() == "original\n"

    def test_applied_reflects_content(self, tmp_path):
        target = tmp_path / "conf"
        action = self._action(target)
        assert not action.applied()
        action.apply(profiles.State())
        assert action.applied()

    def test_revert_without_a_record_is_a_no_op(self, tmp_path):
        target = tmp_path / "conf"
        target.write_text("untouched\n")
        self._action(target).revert(profiles.State())
        assert target.read_text() == "untouched\n"


class TestProfileDefinition:
    def test_mac_profile_has_actions(self):
        assert profiles.mac().actions

    def test_every_action_declares_its_activation(self):
        for name in profiles.PROFILES:
            for action in profiles.PROFILES[name]().actions:
                assert action.activation
                assert action.description

    def test_action_keys_are_unique(self):
        keys = [a.key for a in profiles.mac().actions]
        assert len(keys) == len(set(keys))

    def test_keyd_payload_has_no_known_dead_bindings(self):
        # macro(rightalt+N) never worked; it must not ship in a profile.
        assert "macro(rightalt" not in profiles._payload("keyd-mac.conf")

    def test_terminal_payload_restores_control_sequences(self):
        # Without these the modifier swap destroys Ctrl+C in the terminal.
        payload = profiles._payload("kitty-mac.conf")
        assert "copy_or_interrupt" in payload
        assert "map super+c send_text all \\x03" in payload


class TestSudoHomeResolution:
    """Path.home() follows $HOME, which sudo resets to /root.

    Using it meant every user-level action wrote into root's home and the
    profile reported success while changing nothing the user would ever see.
    The failure is silent, which is exactly why it needs a test.
    """

    def test_sudo_user_wins_over_root_home(self, monkeypatch):
        monkeypatch.setenv("HOME", "/root")
        monkeypatch.setenv("SUDO_USER", "theseal")
        assert profiles.target_user() == "theseal"
        assert profiles.user_home() != Path("/root")
        assert str(profiles.user_home()).startswith("/home/")

    def test_user_paths_are_not_under_root(self, monkeypatch):
        monkeypatch.setenv("HOME", "/root")
        monkeypatch.setenv("SUDO_USER", "theseal")
        for action in profiles.mac().actions:
            path = getattr(action, "path", None)
            if path is None or str(path).startswith("/etc"):
                continue
            assert not str(path).startswith("/root"), f"{action.key} -> {path}"

    def test_unknown_sudo_user_falls_back_without_raising(self, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "nosuchuser-zzz")
        assert profiles.user_home()   # must not raise KeyError

    def test_system_paths_stay_absolute(self, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "theseal")
        keyd_action = next(a for a in profiles.mac().actions if a.key == "keyd-mac")
        assert str(keyd_action.path) == "/etc/keyd/default.conf"
