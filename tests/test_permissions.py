"""Tests for the setup generator.

This module writes files that run as root, so the whitelist is the security
boundary of the whole scheme and is tested as such.
"""

import os
import stat
from pathlib import Path

import pytest

from magic_pomme import drivers, permissions


class TestWhitelistIntegrity:
    def test_every_whitelisted_module_is_known(self):
        assert set(permissions.WHITELIST) <= set(drivers.PARAMS)

    def test_every_whitelisted_param_actually_exists(self):
        # Catches drift: a renamed or dropped parameter would otherwise be
        # silently chmodded into nothing.
        for module, params in permissions.WHITELIST.items():
            for param in params:
                assert param in drivers.PARAMS[module], f"{module}.{param}"

    def test_debug_only_parameters_are_not_exposed(self):
        assert "report_undeciphered" not in permissions.WHITELIST["hid_magicmouse"]

    def test_whitelist_is_not_empty(self):
        assert all(params for params in permissions.WHITELIST.values())


class TestHelperScript:
    def test_mentions_every_whitelisted_param(self):
        script = permissions.helper_script()
        for module, params in permissions.WHITELIST.items():
            assert module in script
            for param in params:
                assert param in script

    def test_refuses_unknown_modules(self):
        assert "refusing unknown module" in permissions.helper_script()

    def test_parameter_names_are_never_taken_from_input(self):
        # The loop must iterate the hardcoded $PARAMS, not anything derived
        # from "$2" or beyond -- udev arguments are untrusted.
        script = permissions.helper_script()
        assert "for param in $PARAMS" in script
        assert '"$2"' not in script

    def test_is_posix_sh_not_bashism(self):
        assert permissions.helper_script().startswith("#!/bin/sh")


class TestUdevRule:
    def test_one_rule_per_module(self):
        rule = permissions.udev_rule()
        for module in permissions.WHITELIST:
            assert f'KERNEL=="{module}"' in rule

    def test_matches_the_module_subsystem(self):
        assert 'SUBSYSTEM=="module"' in permissions.udev_rule()

    def test_includes_the_bluetooth_autosuspend_fix(self):
        assert 'ATTR{power/control}="on"' in permissions.udev_rule()


class TestFiles:
    def test_three_files_installed(self):
        assert len(permissions.files()) == 3

    def test_helper_is_executable_others_are_not(self):
        installed = permissions.files()
        assert installed[permissions.HELPER][1] & stat.S_IXUSR
        assert not installed[permissions.UDEV_RULE][1] & stat.S_IXUSR

    def test_all_paths_absolute(self):
        assert all(path.is_absolute() for path in permissions.files())


class TestStatus:
    def test_incomplete_when_files_absent(self):
        report = permissions.Status(group_exists=True, absent=[Path("/x")])
        assert not report.complete

    def test_complete_when_nothing_outstanding(self):
        assert permissions.Status(group_exists=True).complete

    def test_relogin_only_when_groups_are_stale_not_missing(self):
        assert permissions.Status(True, stale_groups=["keyd"]).needs_relogin
        assert not permissions.Status(
            True, missing_groups=["keyd"], stale_groups=["x"]
        ).needs_relogin


class TestTargetUser:
    def test_prefers_sudo_user(self, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "realuser")
        monkeypatch.setenv("USER", "root")
        assert permissions.target_user() == "realuser"

    def test_falls_back_to_user(self, monkeypatch):
        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.setenv("USER", "someone")
        assert permissions.target_user() == "someone"


class TestPrivilegeGuard:
    @pytest.mark.skipif(os.geteuid() == 0, reason="running as root")
    def test_install_refuses_without_root(self):
        with pytest.raises(PermissionError, match="root"):
            permissions.install()

    @pytest.mark.skipif(os.geteuid() == 0, reason="running as root")
    def test_uninstall_refuses_without_root(self):
        with pytest.raises(PermissionError, match="root"):
            permissions.uninstall()
