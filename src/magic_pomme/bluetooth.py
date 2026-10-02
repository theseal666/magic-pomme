"""Bluetooth discovery and pairing for Apple input devices.

Implemented over ``bluetoothctl`` rather than BlueZ D-Bus. That is a deliberate
v1 trade: it keeps the package dependency-free, and pairing is an interactive,
one-off action where a subprocess round-trip costs nothing. The surface here is
kept narrow and dataclass-shaped specifically so it can be swapped for a
``org.bluez`` D-Bus client later without touching callers.

Pairing an Apple device, in order:

1. Unplug it -- Magic devices do not advertise over Bluetooth while on USB.
2. Power-cycle the switch, so it stops seeking its last known host.
3. Make sure any previously paired Mac nearby has Bluetooth off, or it will
   reclaim the device before the scan sees it.
4. ``scan()``, then ``pair()``, then ``trust()``, then ``connect()``.

``trust()`` is not optional: without it the device will not auto-reconnect
after a reboot.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

from .devices import APPLE_VENDORS

# "Modalias: bluetooth:v004Cp029Fd0206" -> vendor 0x004C, product 0x029F.
# The most reliable way to tell an Apple device from its advertisement.
_MODALIAS = re.compile(r"bluetooth:v([0-9A-Fa-f]{4})p([0-9A-Fa-f]{4})")
_DEVICE_LINE = re.compile(r"^Device\s+((?:[0-9A-F]{2}:){5}[0-9A-F]{2})\s*(.*)$", re.I)
_RSSI = re.compile(r"\((-?\d+)\)")

TIMEOUT = 30


class BluetoothUnavailable(RuntimeError):
    """bluetoothctl is missing, or no adapter is present."""


@dataclass
class BluetoothDevice:
    mac: str
    name: str = ""
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    vendor: int | None = None
    product: int | None = None
    icon: str = ""
    rssi: int | None = None

    @property
    def is_apple(self) -> bool:
        return self.vendor in APPLE_VENDORS if self.vendor is not None else False

    @property
    def is_input(self) -> bool:
        return self.icon.startswith("input-")

    @property
    def ready(self) -> bool:
        """Paired, trusted and connected -- i.e. will come back after reboot."""
        return self.paired and self.trusted and self.connected

    def __str__(self) -> str:
        state = []
        if self.connected:
            state.append("connected")
        if self.paired:
            state.append("paired")
        if self.trusted:
            state.append("trusted")
        if not state:
            state.append("new")
        signal = f"  {self.rssi} dBm" if self.rssi is not None else ""
        return f"{self.name or self.mac}  [{', '.join(state)}]{signal}"


def _binary() -> str:
    found = shutil.which("bluetoothctl")
    if not found:
        raise BluetoothUnavailable("bluetoothctl not found - install bluez")
    return found


def _run(args: list[str], timeout: int = TIMEOUT, stdin: str | None = None) -> str:
    try:
        done = subprocess.run(
            [_binary(), *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ""
    return done.stdout


def available() -> bool:
    try:
        _binary()
    except BluetoothUnavailable:
        return False
    return "Powered: yes" in _run(["show"], timeout=5)


def info(mac: str) -> BluetoothDevice | None:
    """Full state for one device."""
    text = _run(["info", mac], timeout=10)
    if "not available" in text or not text.strip():
        return None

    device = BluetoothDevice(mac=mac)
    for line in text.splitlines():
        key, _, value = line.strip().partition(":")
        value = value.strip()
        match key:
            case "Name" | "Alias":
                device.name = device.name or value
            case "Paired":
                device.paired = value == "yes"
            case "Trusted":
                device.trusted = value == "yes"
            case "Connected":
                device.connected = value == "yes"
            case "Icon":
                device.icon = value
            case "Modalias":
                if found := _MODALIAS.search(value):
                    device.vendor = int(found.group(1), 16)
                    device.product = int(found.group(2), 16)
            case "RSSI":
                if found := _RSSI.search(value):
                    device.rssi = int(found.group(1))
    return device


def devices(apple_only: bool = False, detailed: bool = True) -> list[BluetoothDevice]:
    """Known devices. Without ``detailed`` only MAC and name are populated."""
    listed: list[BluetoothDevice] = []
    for line in _run(["devices"], timeout=10).splitlines():
        if found := _DEVICE_LINE.match(line.strip()):
            listed.append(BluetoothDevice(mac=found.group(1), name=found.group(2)))

    if not detailed:
        return listed

    full = [info(entry.mac) or entry for entry in listed]
    return [entry for entry in full if entry.is_apple] if apple_only else full


def scan(seconds: int = 20, apple_only: bool = False) -> list[BluetoothDevice]:
    """Discover advertising devices, then return everything now known.

    bluetoothctl needs its scan held open for the whole window, so this drives
    it through stdin and lets the timeout expire rather than passing ``scan on``
    as a one-shot argument.
    """
    script = "agent KeyboardDisplay\ndefault-agent\npower on\nscan on\n"
    try:
        subprocess.run(
            [_binary()],
            input=script,
            capture_output=True,
            text=True,
            timeout=seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pass  # expected - the timeout *is* the scan duration
    return devices(apple_only=apple_only)


def pair(mac: str, timeout: int = 25) -> bool:
    text = _run(["--timeout", str(timeout), "pair", mac], timeout=timeout + 10)
    return "Pairing successful" in text or (info(mac) or BluetoothDevice(mac)).paired


def trust(mac: str) -> bool:
    text = _run(["trust", mac], timeout=10)
    return "trust succeeded" in text


def connect(mac: str, timeout: int = 20) -> bool:
    text = _run(["--timeout", str(timeout), "connect", mac], timeout=timeout + 10)
    return "Connection successful" in text


def disconnect(mac: str) -> bool:
    return "Successful disconnected" in _run(["disconnect", mac], timeout=15)


def remove(mac: str) -> bool:
    """Forget a device. Needed before re-pairing one that fails to authenticate."""
    return "Device has been removed" in _run(["remove", mac], timeout=10)


def setup(mac: str) -> tuple[bool, str]:
    """pair -> trust -> connect, the full sequence, with a readable outcome."""
    if not pair(mac):
        return False, "pairing failed - try remove() and pair again, or power-cycle"
    if not trust(mac):
        return False, "paired, but trust failed - it will not auto-reconnect"
    if not connect(mac):
        return False, "paired and trusted, but connect failed"
    return True, "paired, trusted and connected"
