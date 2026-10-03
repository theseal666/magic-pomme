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
