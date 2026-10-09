"""Tests for device identity logic.

The vendor-id switch between USB and Bluetooth is the subtle part: the same
Magic Keyboard is 05ac:029f wired and 004c:029f wireless.
"""

from magic_pomme import devices


def _device(**kw):
    base = dict(
        hid_ids=["x"], name="kbd", vendor=devices.VENDOR_USB,
        product=0x029F, bus=0x03, driver="apple",
    )
    return devices.Device(**{**base, **kw})


class TestTransport:
    def test_usb_bus(self):
        assert _device(bus=0x03).transport == "usb"

    def test_bluetooth_bus(self):
        assert _device(bus=0x05).transport == "bluetooth"

    def test_unknown_bus_is_rendered_not_crashed(self):
        assert _device(bus=0x99).transport == "bus-0x99"


class TestIdentity:
    def test_both_apple_vendor_ids_recognised(self):
        assert _device(vendor=devices.VENDOR_USB).is_apple
        assert _device(vendor=devices.VENDOR_BLUETOOTH).is_apple

    def test_other_vendor_is_not_apple(self):
        assert not _device(vendor=0x046D).is_apple

    def test_module_mapping(self):
        assert _device(driver="apple").module == "hid_apple"
        assert _device(driver="magicmouse").module == "hid_magicmouse"

    def test_unknown_driver_has_no_module_and_is_not_configurable(self):
        device = _device(driver="hid-generic")
        assert device.module is None
        assert not device.configurable


class TestBattery:
    def test_low_flags_only_when_discharging(self):
        assert devices.Battery(10, "Discharging").low
        assert not devices.Battery(10, "Charging").low

    def test_healthy_is_not_low(self):
        assert not devices.Battery(80, "Discharging").low

    def test_charging_is_case_insensitive(self):
        assert devices.Battery(50, "charging").charging
        assert devices.Battery(50, "Charging").charging


class TestBatteryUnknown:
    """Zero means 'no report yet', not 'flat'.

    After a Bluetooth reconnect the kernel shows capacity 0 until the first
    HID battery report arrives. A device that is connected and in use is not
    actually empty, so rendering a red 0% bar misreports a healthy device.
    """

    def test_zero_is_unknown_not_empty(self):
        assert devices.Battery(0, "Discharging").unknown

    def test_a_real_reading_is_not_unknown(self):
        assert not devices.Battery(1, "Discharging").unknown
        assert not devices.Battery(100, "Charging").unknown

    def test_unknown_is_never_reported_as_low(self):
        assert not devices.Battery(0, "Discharging").low

    def test_a_genuinely_low_reading_still_flags(self):
        assert devices.Battery(8, "Discharging").low


class TestBatteryIsOptional:
    """Enumeration must not be hostage to the battery read.

    Reading capacity makes the kernel request a HID report and wait for the
    device; an unresponsive Bluetooth peripheral blocks the caller for about
    ten seconds. That froze the tray applet's UI thread. Enumeration itself is
    sub-millisecond, so the two must be separable.
    """

    def test_skipping_battery_does_not_call_the_blocking_read(self, monkeypatch):
        called = []
        monkeypatch.setattr(devices, "batteries",
                            lambda: called.append(True) or {})
        devices.devices(with_battery=False)
        assert not called, "batteries() must not be called when skipped"

    def test_battery_read_happens_by_default(self, monkeypatch):
        called = []
        monkeypatch.setattr(devices, "batteries",
                            lambda: called.append(True) or {})
        devices.devices()
        assert called

    def test_devices_still_enumerated_without_battery(self, monkeypatch):
        monkeypatch.setattr(devices, "batteries", lambda: {})
        without = devices.devices(with_battery=False)
        assert all(device.battery is None for device in without)


class TestBatteryUnavailable:
    """ENODATA is a third state, distinct from flat and from absent.

    Apple Bluetooth peripherals intermittently refuse the battery report:
    reads fail with "No data available" after a timeout, for hours, then
    recover. Dropping those devices from the result left the UI saying
    "reading..." forever with no way to tell it apart from a slow first read.
    """

    def test_negative_capacity_means_unavailable(self):
        assert devices.Battery(-1, "unavailable").unavailable

    def test_a_real_reading_is_available(self):
        assert not devices.Battery(95, "Discharging").unavailable

    def test_unavailable_counts_as_unknown_for_display(self):
        assert devices.Battery(-1, "unavailable").unknown

    def test_unavailable_is_never_reported_as_low(self):
        # Otherwise a device that simply will not answer looks flat.
        assert not devices.Battery(-1, "unavailable").low

    def test_zero_is_still_unknown_not_unavailable(self):
        zero = devices.Battery(0, "Discharging")
        assert zero.unknown
        assert not zero.unavailable
