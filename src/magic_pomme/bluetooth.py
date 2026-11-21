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
5. Check the kernel actually attached it -- see ``attached()``.

``trust()`` is not optional: without it the device will not auto-reconnect
after a reboot.

Bonding is the trap that costs the afternoon. BlueZ will report ``Paired: yes``
for a device whose link key was never stored, leaving ``Bonded: no``, and then
refuse the HID connection outright::

    bluetoothd: hidp_add_connection() Rejected connection from !bonded device

The device sits there reporting itself connected, no input device is ever
created, and nothing in ``bluetoothctl info`` says why -- the refusal is only
in the journal. A half-paired device cannot be repaired in place: it has to be
forgotten and paired again, which needs a fresh scan because ``remove`` drops
the device object. ``setup()`` does all of that.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass

from . import devices as hid
from .devices import APPLE_VENDORS

# "Modalias: bluetooth:v004Cp029Fd0206" -> vendor 0x004C, product 0x029F.
# The most reliable way to tell an Apple device from its advertisement.
_MODALIAS = re.compile(r"bluetooth:v([0-9A-Fa-f]{4})p([0-9A-Fa-f]{4})")
_DEVICE_LINE = re.compile(r"^Device\s+((?:[0-9A-F]{2}:){5}[0-9A-F]{2})\s*(.*)$", re.I)
_RSSI = re.compile(r"\((-?\d+)\)")

TIMEOUT = 30
ATTACH_WAIT = 8.0
REDISCOVER_SECONDS = 15


class BluetoothUnavailable(RuntimeError):
    """bluetoothctl is missing, or no adapter is present."""


@dataclass
class BluetoothDevice:
    mac: str
    name: str = ""
    paired: bool = False
    bonded: bool = False
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
        """Paired, bonded, trusted and connected -- i.e. it works and comes back.

        ``bonded`` carries as much weight as ``paired`` here. Without a stored
        link key the device is refused at HID connect time, so a device that is
        paired, trusted and connected but not bonded is not ready for anything
        -- it will never type.
        """
        return self.paired and self.bonded and self.trusted and self.connected

    @property
    def half_paired(self) -> bool:
        """Paired without a link key: the state that needs a forget-and-retry."""
        return self.paired and not self.bonded

    def __str__(self) -> str:
        state = []
        if self.connected:
            state.append("connected")
        if self.paired:
            state.append("paired")
        if self.half_paired:
            state.append("NOT BONDED")
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
            case "Bonded":
                device.bonded = value == "yes"
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

    if "Bonded:" not in text:
        # BlueZ before 5.65 has no Bonded flag at all; there Paired carries the
        # whole meaning. Treating the missing field as "not bonded" would fail
        # every pairing on an older stack, so take Paired at its word instead.
        device.bonded = device.paired
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

    bluetoothctl scans only while it is running, and it exits as soon as its
    stdin reaches EOF. Passing the commands via subprocess.run(input=...) therefore
    closes stdin straight away and the process dies in milliseconds without
    ever scanning -- a silent no-op that looks like "nothing was found".
    So the pipe is held open deliberately for the whole window.
    """
    proc = subprocess.Popen(
        [_binary()],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write("agent KeyboardDisplay\ndefault-agent\npower on\nscan on\n")
        proc.stdin.flush()
        time.sleep(seconds)          # stdin stays open: this is the scan window
        try:
            proc.stdin.write("scan off\nquit\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    return devices(apple_only=apple_only)


def pair(mac: str, timeout: int = 25) -> bool:
    """Pair, and insist a link key was stored.

    Both "Pairing successful" and ``Paired: yes`` are also true of a device
    BlueZ never bonded with, and that device is refused when the HID profile
    tries to connect. Only a bonded device is paired in any useful sense, so
    the state is read back rather than trusting the message.
    """
    _run(["--timeout", str(timeout), "pair", mac], timeout=timeout + 10)
    state = info(mac)
    return bool(state and state.paired and state.bonded)


def trust(mac: str) -> bool:
    """Mark trusted, verified against the device rather than the message.

    bluetoothctl prints "trust succeeded" for a trust issued while pairing is
    still in flight, and the flag does not stick. Read it back.
    """
    _run(["trust", mac], timeout=10)
    return (info(mac) or BluetoothDevice(mac)).trusted


def connect_with_reason(mac: str, timeout: int = 20) -> tuple[bool, str]:
    """Connect, and keep bluetoothctl's reason when it fails.

    The reason matters: ``br-connection-create-socket`` is what a device that
    has gone to sleep or been switched off answers, and no amount of retrying
    from here wakes it -- observed on a Magic Mouse that only came back when
    its switch was physically power-cycled. Swallowing that text leaves the
    user with a bare "connect failed" and nothing to act on.
    """
    text = _run(["--timeout", str(timeout), "connect", mac], timeout=timeout + 10)
    if "Connection successful" in text:
        return True, ""
    if (info(mac) or BluetoothDevice(mac)).connected:
        return True, ""
    reason = ""
    for line in text.splitlines():
        if "Failed to connect" in line:
            reason = line.split(":", 1)[-1].strip()
    return False, reason


def connect(mac: str, timeout: int = 20) -> bool:
    return connect_with_reason(mac, timeout)[0]


def disconnect(mac: str) -> bool:
    """Disconnect, verified against the device.

    This used to match on "Successful disconnected", which bluetoothctl does
    not say -- it says "Disconnection successful". So this reported failure on
    every successful disconnect, including the ones inside repair(), where it
    hid the fact that the device really had been dropped.
    """
    _run(["disconnect", mac], timeout=15)
    return not (info(mac) or BluetoothDevice(mac, connected=True)).connected


def remove(mac: str) -> bool:
    """Forget a device, verified by the device object being gone.

    Needed before re-pairing one that fails to authenticate. Checked by
    reading back rather than by matching output, because the wording of these
    messages is not a contract -- see disconnect().
    """
    _run(["remove", mac], timeout=10)
    return info(mac) is None


def attached(mac: str) -> bool:
    """Whether the kernel has this device as a HID device.

    The only honest test of a pairing. BlueZ reporting paired, trusted and
    connected says nothing about whether an input device exists -- with a
    missing link key the HID connection is refused and the device types
    nothing. The HID layer records the Bluetooth address as the device serial,
    which is what makes the match possible.
    """
    wanted = mac.lower()
    try:
        return any(device.serial.lower() == wanted
                   for device in hid.devices(apple_only=False, with_battery=False))
    except OSError:
        return False


def wait_attached(mac: str, seconds: float = ATTACH_WAIT) -> bool:
    """Give the kernel a moment to bind the driver after connect() returns."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if attached(mac):
            return True
        time.sleep(0.5)
    return attached(mac)


def repair(mac: str, rediscover_seconds: int = REDISCOVER_SECONDS) -> bool:
    """Forget a half-paired device and pair it again.

    There is no way to add the missing link key to an existing pairing, so the
    device has to go. ``remove`` also drops the device object, which makes it
    unpairable until it is discovered again -- hence the scan, without which
    the retry fails with a bare "not available".
    """
    disconnect(mac)
    remove(mac)
    scan(rediscover_seconds)
    if info(mac) is None:
        return False
    return pair(mac)


def setup(mac: str, rediscover_seconds: int = REDISCOVER_SECONDS) -> tuple[bool, str]:
    """pair -> trust -> connect -> verify, recovering the half-paired state.

    Returns a readable outcome. Success here means the kernel attached the
    device, not merely that BlueZ was content.
    """
    if not pair(mac):
        state = info(mac)
        if state is None:
            return False, ("the device is not advertising - unplug it, power-cycle "
                           "the switch, and scan again")
        if not state.half_paired:
            return False, "pairing failed - power-cycle the switch and scan again"
        # Paired with no link key. Forget it and start over; pairing on top of
        # this state always fails the same way.
        if not repair(mac, rediscover_seconds):
            return False, ("paired without a link key (Bonded: no) and pairing "
                           "again did not fix it - unplug it, power-cycle the "
                           "switch, and scan again")

    if not trust(mac):
        return False, "paired, but trust failed - it will not auto-reconnect"
    connected, reason = connect_with_reason(mac)
    if not connected:
        if "br-connection-create-socket" in reason:
            return False, ("paired and trusted, but it is asleep or switched off "
                           "- power-cycle its switch and try again")
        return False, f"paired and trusted, but connect failed{f': {reason}' if reason else ''}"
    if not wait_attached(mac):
        return False, ("BlueZ reports it connected, but the kernel never attached "
                       "it - look for '!bonded device' in the journal")
    return True, "paired, bonded, trusted, connected and attached"
