"""Tests for the value handling, which is where the module differences bite.

hid_apple takes integers, hid_magicmouse takes Y/N. Everything here is pure
logic -- no hardware, no root.
"""

import pytest

from magic_pomme import drivers


class TestParse:
    def test_bool_accepts_kernel_yn(self):
        param = drivers.spec("hid_magicmouse", "scroll_acceleration")
        assert param.parse("Y\n") == 1
        assert param.parse("N\n") == 0

    def test_bool_accepts_numeric(self):
        param = drivers.spec("hid_magicmouse", "scroll_acceleration")
        assert param.parse("1") == 1
        assert param.parse("0") == 0

    def test_int_handles_negative(self):
        # iso_layout uses -1 for "auto", so sign must survive.
        assert drivers.spec("hid_apple", "iso_layout").parse("-1\n") == -1

    def test_garbage_is_none_not_an_exception(self):
        assert drivers.spec("hid_apple", "fnmode").parse("wat") is None


class TestFormat:
    def test_bool_renders_as_yn_for_the_kernel(self):
        param = drivers.spec("hid_magicmouse", "emulate_3button")
        assert param.format(1) == "Y"
        assert param.format(0) == "N"

    def test_int_renders_as_digits(self):
        assert drivers.spec("hid_apple", "fnmode").format(2) == "2"

    def test_roundtrip(self):
        for module, params in drivers.PARAMS.items():
            for param in params.values():
                for value in (0, 1):
                    assert param.parse(param.format(value)) == value


class TestValidate:
    def test_rejects_value_outside_choices(self):
        with pytest.raises(ValueError, match="one of"):
            drivers.spec("hid_apple", "fnmode").validate(9)

    def test_accepts_every_declared_choice(self):
        param = drivers.spec("hid_apple", "iso_layout")
        for value in param.choices:
            param.validate(value)

    def test_enforces_numeric_range(self):
        param = drivers.spec("hid_magicmouse", "scroll_speed")
        param.validate(0)
        param.validate(63)
        with pytest.raises(ValueError, match=">="):
            param.validate(-1)
        with pytest.raises(ValueError, match="<="):
            param.validate(64)


class TestSpec:
    def test_unknown_param_lists_the_known_ones(self):
        with pytest.raises(KeyError, match="swap_ctrl_cmd"):
            drivers.spec("hid_apple", "nope")

    def test_unknown_module_is_named(self):
        with pytest.raises(KeyError, match="unknown module"):
            drivers.spec("hid_nonsense", "whatever")

    def test_every_param_has_a_description(self):
        for params in drivers.PARAMS.values():
            for param in params.values():
                assert param.description.strip()
