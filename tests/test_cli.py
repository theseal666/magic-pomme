"""Tests for the CLI's renderers.

cmd_devices tested ``unknown`` before ``unavailable``. Since ``unknown``
deliberately subsumes ``unavailable``, a Magic Mouse that refused to answer
was printed as "awaiting first report since reconnect" -- the "still loading"
rendering that docs/platform-notes.md explicitly forbids for that state. The
tray rendered the same device correctly, so the two interfaces disagreed about
the same hardware at the same moment.
"""

from magic_pomme import cli
from magic_pomme.devices import Battery


class TestBatteryLine:
    def test_a_refusal_is_not_shown_as_a_pending_read(self):
        line = cli._battery_line(Battery(-1, "unavailable"))
        assert "refused" in line
        assert "awaiting" not in line

    def test_a_refusal_is_not_shown_as_a_percentage(self):
        assert "%" not in cli._battery_line(Battery(-1, "unavailable"))

    def test_a_pending_report_says_so(self):
        assert "awaiting" in cli._battery_line(Battery(0, "unknown"))

    def test_a_pending_report_is_not_shown_as_flat(self):
        assert "0%" not in cli._battery_line(Battery(0, "unknown"))

    def test_a_level_is_shown_with_its_status(self):
        line = cli._battery_line(Battery(66, "Discharging"))
        assert "66%" in line and "discharging" in line

    def test_charging_overrides_the_status_text(self):
        assert "charging" in cli._battery_line(Battery(25, "Charging"))
