"""Tests for the Plasma pointer settings.

The group path is the whole contract: KConfig keys these per device as
[Libinput][vendor][product][name] with the ids in *decimal*. Writing hex, or
getting the order wrong, silently creates a section KWin never reads -- the
setting appears to save and nothing happens.
"""

from magic_pomme import plasma_input


def _mouse():
    return plasma_input.Pointer("Niclas's Magic Mouse", 0x004C, 0x0269)


class TestGroupPath:
    def test_ids_are_decimal_not_hex(self):
        groups = _mouse().groups
        assert groups[1] == "76"      # 0x004C
        assert groups[2] == "617"     # 0x0269
        assert "4c" not in " ".join(groups).lower()

    def test_order_matches_kconfig(self):
        assert _mouse().groups == [
            "Libinput", "76", "617", "Niclas's Magic Mouse"
        ]

    def test_name_is_passed_through_unaltered(self):
        # Apple device names carry a typographic apostrophe; KConfig escapes
        # it, we must not pre-mangle it.
        fancy = plasma_input.Pointer("Niclas’s Magic Mouse", 76, 617)
        assert fancy.groups[3] == "Niclas’s Magic Mouse"


class TestVendors:
    def test_both_apple_vendor_ids_are_known(self):
        assert 0x05AC in plasma_input.APPLE_VENDORS   # USB
        assert 0x004C in plasma_input.APPLE_VENDORS   # Bluetooth


class TestNaturalScrollParsing:
    def _with_output(self, monkeypatch, text):
        monkeypatch.setattr(plasma_input, "_tool", lambda name: "/bin/true")
        monkeypatch.setattr(
            plasma_input.subprocess, "run",
            lambda *a, **k: type("R", (), {"stdout": text, "returncode": 0})())

    def test_true(self, monkeypatch):
        self._with_output(monkeypatch, "true\n")
        assert plasma_input.natural_scroll(_mouse()) is True

    def test_false(self, monkeypatch):
        self._with_output(monkeypatch, "false\n")
        assert plasma_input.natural_scroll(_mouse()) is False

    def test_unset_is_none_not_false(self, monkeypatch):
        # An absent key means "Plasma default", which is not the same as the
        # user having chosen off.
        self._with_output(monkeypatch, "\n")
        assert plasma_input.natural_scroll(_mouse()) is None

    def test_missing_tools_do_not_raise(self, monkeypatch):
        def boom(name):
            raise plasma_input.PlasmaToolsMissing("nope")
        monkeypatch.setattr(plasma_input, "_tool", boom)
        assert plasma_input.natural_scroll(_mouse()) is None


class TestSudoHandling:
    """kreadconfig6/kwriteconfig6 resolve config through $HOME.

    Run as root they read and write /root/.config/kcminputrc, which KWin never
    looks at, and report success -- the same silent class of failure that made
    the profile appear to apply while changing nothing.
    """

    def test_unprivileged_command_is_unchanged(self, monkeypatch):
        monkeypatch.setattr(plasma_input.os, "geteuid", lambda: 1000)
        assert plasma_input._as_user(["kreadconfig6"]) == ["kreadconfig6"]

    def test_root_with_sudo_user_drops_privilege(self, monkeypatch):
        monkeypatch.setattr(plasma_input.os, "geteuid", lambda: 0)
        monkeypatch.setenv("SUDO_USER", "theseal")
        command = plasma_input._as_user(["kwriteconfig6"])
        assert command[:3] == ["sudo", "-u", "theseal"]
        assert any(part.startswith("HOME=/home/") for part in command)
        assert not any(part == "HOME=/root" for part in command)

    def test_root_without_sudo_user_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(plasma_input.os, "geteuid", lambda: 0)
        monkeypatch.delenv("SUDO_USER", raising=False)
        assert plasma_input._as_user(["kreadconfig6"]) == ["kreadconfig6"]

    def test_unknown_sudo_user_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(plasma_input.os, "geteuid", lambda: 0)
        monkeypatch.setenv("SUDO_USER", "nosuchuser-zzz")
        assert plasma_input._as_user(["kreadconfig6"]) == ["kreadconfig6"]


def _trackpad():
    return plasma_input.Pointer("niclasdahlgren’s trackpad", 0x05AC, 0x030E,
                                sys_name="event14", touchpad=True)


class TestPointerDetection:
    """A trackpad has no relative axes, and filtering on REL hid it entirely.

    The Magic Trackpad reports absolute multitouch axes and nothing relative,
    so it was invisible to this whole module -- no natural-scroll entry, no
    scroll or pointer speed -- while the Magic Mouse right next to it worked.
    """

    def _device(self, tmp_path, rel="0", absolute="0", props="0"):
        caps = tmp_path / "capabilities"
        caps.mkdir(parents=True, exist_ok=True)
        (caps / "rel").write_text(rel + "\n")
        (caps / "abs").write_text(absolute + "\n")
        (tmp_path / "properties").write_text(props + "\n")
        return tmp_path

    def test_a_mouse_is_a_pointer(self, tmp_path):
        device = self._device(tmp_path, rel="1943")
        assert plasma_input._is_pointer(device) == (True, False)

    def test_a_trackpad_without_rel_axes_is_still_a_pointer(self, tmp_path):
        # Real values from a Magic Trackpad 1: abs multitouch, rel none,
        # properties 0x5 = POINTER | BUTTONPAD.
        device = self._device(tmp_path, absolute="273800000000003", props="5")
        assert plasma_input._is_pointer(device) == (True, True)

    def test_a_touchscreen_is_not_a_pointer(self, tmp_path):
        # INPUT_PROP_DIRECT: you touch it where the cursor already is.
        device = self._device(tmp_path, absolute="2650000 3", props="2")
        assert plasma_input._is_pointer(device) == (False, False)

    def test_a_keyboard_is_not_a_pointer(self, tmp_path):
        assert plasma_input._is_pointer(self._device(tmp_path)) == (False, False)


class TestLiveSettings:
    GETALL = (
        "({'name': <'niclasdahlgren’s trackpad'>, 'scrollFactor': <0.5>, "
        "'pointerAcceleration': <0.14999999999999999>, "
        "'pointerAccelerationProfileFlat': <false>, "
        "'supportsPointerAccelerationProfileFlat': <true>, "
        "'naturalScroll': <true>},)"
    )

    def test_kwin_is_the_authority_when_it_answers(self, monkeypatch):
        monkeypatch.setattr(plasma_input, "_gdbus", lambda: "/bin/true")
        monkeypatch.setattr(plasma_input, "_run", lambda *a, **k: self.GETALL)
        found = plasma_input.settings(_trackpad())
        assert found.scroll_factor == 0.5
        assert round(found.pointer_acceleration, 2) == 0.15
        assert found.flat_profile is False
        assert found.supports_flat
        assert found.natural_scroll is True

    def test_config_is_the_fallback_without_a_kwin_object(self, monkeypatch):
        """kcminputrc only holds what has been written, so KWin comes first."""
        monkeypatch.setattr(plasma_input, "_tool", lambda name: "/bin/true")
        monkeypatch.setattr(plasma_input, "_run", lambda *a, **k: "0.75")
        found = plasma_input.settings(_mouse())   # no sys_name, so no object
        assert found.scroll_factor == 0.75


class TestProfileGoesThroughDBus:
    """reconfigure() resets the profile, so writing the config is a no-op.

    Worse, a reconfigure for any other setting throws a flat profile away. The
    profile therefore has to be set on KWin's live device object, and nothing
    may reconfigure afterwards. KWin persists it to kcminputrc by itself.
    """

    def _traced(self, monkeypatch):
        calls = []
        monkeypatch.setattr(plasma_input, "_gdbus", lambda: "/bin/true")
        monkeypatch.setattr(plasma_input, "_tool", lambda name: "/bin/true")
        monkeypatch.setattr(plasma_input, "_run",
                            lambda *a, **k: calls.append("run") or "")
        monkeypatch.setattr(plasma_input, "_config_write",
                            lambda *a: calls.append("config"))
        monkeypatch.setattr(plasma_input, "reconfigure",
                            lambda: calls.append("reconfigure") or True)
        return calls

    def test_setting_the_profile_never_reconfigures(self, monkeypatch):
        calls = self._traced(monkeypatch)
        monkeypatch.setattr(plasma_input, "settings",
                            lambda p: plasma_input.Settings(flat_profile=True))
        assert plasma_input.set_flat_profile(_trackpad(), True)
        assert "reconfigure" not in calls

    def test_a_profile_that_did_not_take_is_reported(self, monkeypatch):
        self._traced(monkeypatch)
        monkeypatch.setattr(plasma_input, "settings",
                            lambda p: plasma_input.Settings(flat_profile=False))
        assert not plasma_input.set_flat_profile(_trackpad(), True)

    def test_scroll_factor_does_reconfigure(self, monkeypatch):
        """The config-backed settings need it, or they land at next login."""
        calls = self._traced(monkeypatch)
        plasma_input.set_scroll_factor(_trackpad(), 0.5)
        assert calls == ["config", "reconfigure"]

    def test_kwins_flat_value_is_one(self):
        """Established by watching KWin write it, not from libinput's enum,
        which numbers the profiles the other way round."""
        assert plasma_input.PROFILE_FLAT == 1
        assert plasma_input.PROFILE_ADAPTIVE == 2


class TestRangesAreRefused:
    def test_scroll_factor_must_be_positive(self):
        import pytest
        with pytest.raises(ValueError):
            plasma_input.set_scroll_factor(_trackpad(), 0)

    def test_pointer_acceleration_stays_within_libinputs_range(self):
        import pytest
        for bad in (-1.5, 1.5):
            with pytest.raises(ValueError):
                plasma_input.set_pointer_acceleration(_trackpad(), bad)
