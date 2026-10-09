"""Discovery of Apple HID devices and their batteries, straight from sysfs.

No D-Bus and no root required. /sys/bus/hid/devices carries the identity and
bound driver for every HID device, and /sys/class/power_supply carries the
battery. UPower reads the same files, so going direct costs nothing and drops a
runtime dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

HID_DEVICES = Path("/sys/bus/hid/devices")
POWER_SUPPLY = Path("/sys/class/power_supply")

# Apple uses a different vendor id over Bluetooth than over USB. The same
# Magic Keyboard is 05ac:029f on USB and 004c:029f on Bluetooth -- a trap for
# anything that scopes config to a single id. See docs/platform-notes.md.
VENDOR_USB = 0x05AC
VENDOR_BLUETOOTH = 0x004C
APPLE_VENDORS = frozenset({VENDOR_USB, VENDOR_BLUETOOTH})

BUS_NAMES = {0x03: "usb", 0x05: "bluetooth", 0x18: "virtual"}

# Drivers we can actually configure. Anything else is reported but not tunable.
APPLE_DRIVERS = frozenset({"apple", "magicmouse"})

_HID_ID = re.compile(r"^([0-9a-fA-F]+):([0-9a-fA-F]+):([0-9a-fA-F]+)$")


@dataclass(frozen=True)
class Battery:
    """A HID device's battery, as the kernel reports it.

    ``capacity`` is -1 when the node exists but the device did not answer.
    Apple Bluetooth peripherals do this intermittently: reads return ENODATA
    ("No data available") after a timeout, for hours at a stretch, then start
    working again. That is genuinely different from a flat battery and from
    having no battery at all, and the three must not be conflated.
    """

    capacity: int
    status: str

    @property
    def charging(self) -> bool:
        return self.status.lower() == "charging"

    @property
    def unavailable(self) -> bool:
        """The device refused to answer; there is no value to show."""
        return self.capacity < 0

    @property
    def unknown(self) -> bool:
        """True when the kernel has no reading yet, rather than a flat battery.

        Apple devices report battery over HID periodically, so after a
        Bluetooth reconnect sysfs shows capacity 0 until the first report
        lands -- UPower calls the same state "unknown". A device that is
        connected and in use is not at 0%, so reporting an empty battery there
        is simply wrong. A genuinely flat device powers off rather than
        staying connected, which makes 0 safe to read as "not yet known".
        """
        return self.capacity == 0 or self.unavailable

    @property
    def low(self) -> bool:
        return not self.unknown and 0 <= self.capacity <= 20 and not self.charging


@dataclass
class Device:
    """One physical Apple input device."""

    hid_ids: list[str]
    name: str
    vendor: int
    product: int
    bus: int
    driver: str
    serial: str = ""
    battery: Battery | None = None
    syspaths: list[Path] = field(default_factory=list)

    @property
    def transport(self) -> str:
        return BUS_NAMES.get(self.bus, f"bus-{self.bus:#04x}")

    @property
    def is_apple(self) -> bool:
        return self.vendor in APPLE_VENDORS

    @property
    def configurable(self) -> bool:
        """Whether magic-pomme has driver knobs for this device."""
        return self.driver in APPLE_DRIVERS

    @property
    def module(self) -> str | None:
        """The kernel module whose parameters affect this device."""
        return {"apple": "hid_apple", "magicmouse": "hid_magicmouse"}.get(self.driver)

    def __str__(self) -> str:
        bits = [f"{self.name} [{self.transport}]"]
        if self.battery:
            flag = " (charging)" if self.battery.charging else ""
            bits.append(f"{self.battery.capacity}%{flag}")
        return "  ".join(bits)


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def _uevent(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _read(path / "uevent").splitlines():
        key, _, value = line.partition("=")
        if value:
            out[key] = value
    return out


def batteries() -> dict[str, Battery]:
    """Map HID device id -> Battery.

    **This can block for ~10 seconds per device.** Reading `capacity` makes the
    kernel request a HID battery report, and a Bluetooth device that does not
    answer is waited on until timeout (the reading thread sits in
    `__uhid_report_queue_and_wait`). Never call this on a UI thread; see
    `devices(with_battery=False)`.

    Keyed on the HID id rather than the power_supply name, because that name is
    a MAC address over Bluetooth and a serial number over USB. The
    power_supply `device` symlink resolves to the owning HID device, whose
    basename is the id we want -- a stable key across both transports.
    """
    found: dict[str, Battery] = {}
    if not POWER_SUPPLY.is_dir():
        return found

    for supply in POWER_SUPPLY.iterdir():
        if not (supply / "capacity").exists():
            continue
        try:
            owner = (supply / "device").resolve(strict=True).name
        except OSError:
            continue
        capacity = _read(supply / "capacity")
        if capacity.isdigit():
            found[owner] = Battery(int(capacity),
                                   _read(supply / "status") or "unknown")
        else:
            # The node is there but the read failed (ENODATA). Report that,
            # rather than dropping the device and leaving the UI to show
            # "reading..." forever.
            found[owner] = Battery(-1, "unavailable")
    return found


def devices(apple_only: bool = True, with_battery: bool = True) -> list[Device]:
    """Enumerate input devices, one entry per *physical* device.

    A single keyboard shows up as several HID devices (one per interface), so
    entries are merged on identity and the battery attached to whichever
    interface reports it.

    Pass ``with_battery=False`` to skip the battery read, which blocks for
    seconds per unresponsive Bluetooth device. Enumeration itself is fast.
    """
    power = batteries() if with_battery else {}
    merged: dict[tuple, Device] = {}

    if not HID_DEVICES.is_dir():
        return []

    for entry in sorted(HID_DEVICES.iterdir()):
        info = _uevent(entry)
        match = _HID_ID.match(info.get("HID_ID", ""))
        if not match:
            continue
        bus, vendor, product = (int(group, 16) for group in match.groups())
        if apple_only and vendor not in APPLE_VENDORS:
            continue

        serial = info.get("HID_UNIQ", "")
        key = (vendor, product, bus, serial)
        device = merged.get(key)
        if device is None:
            device = Device(
                hid_ids=[],
                name=info.get("HID_NAME", entry.name),
                vendor=vendor,
                product=product,
                bus=bus,
                driver=info.get("DRIVER", "none"),
                serial=serial,
            )
            merged[key] = device

        device.hid_ids.append(entry.name)
        device.syspaths.append(entry)
        if device.battery is None and entry.name in power:
            device.battery = power[entry.name]

    return list(merged.values())
