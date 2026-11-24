"""Plasma per-device pointer settings, for the ones that belong next to the
driver knobs.

Four settings live here rather than in a module parameter, and the difference
matters: ``hid_magicmouse.scroll_speed`` is a *module* parameter shared by
every device the driver binds, while these are per device. Tuning a trackpad
through the driver changes the mouse with it.

KWin keys them per device in ``kcminputrc`` under a nested group of the form::

    [Libinput][<vendor-decimal>][<product-decimal>][<device name>]
    NaturalScroll=true
    ScrollFactor=0.5
    PointerAcceleration=0.15
    PointerAccelerationProfile=1

Read and written through kreadconfig6/kwriteconfig6 rather than by parsing the
file, because Plasma's nested group syntax is not standard INI and KConfig owns
the escaping.

Two mechanisms, and they are not interchangeable
------------------------------------------------

``NaturalScroll``, ``ScrollFactor`` and ``PointerAcceleration`` are applied by
writing the config and asking KWin to re-read it. Without that reconfigure the
change only lands at the next login.

The acceleration **profile** is different. ``reconfigure`` resets it to the
device default and never reads the stored value, so writing the config and
reconfiguring leaves the profile exactly as it was -- and worse, a reconfigure
for any *other* setting silently throws a flat profile away. It has to be set
live on KWin's own device object instead. KWin then writes
``PointerAccelerationProfile`` to kcminputrc itself, which is how the canonical
values were established by observation: 1 is flat and 2 is adaptive -- the
reverse of libinput's own enum, so do not take that as the mapping. Verified
on KWin 6.3.6.

So: profile through D-Bus and never followed by a reconfigure; everything else
through KConfig followed by one.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

INPUT_CLASS = Path("/sys/class/input")
CONFIG = "kcminputrc"
KEY = "NaturalScroll"          # kept: the original single-setting entry point
IFACE = "org.kde.KWin.InputDevice"

# Apple's USB vendor id and the different one it advertises over Bluetooth.
APPLE_VENDORS = frozenset({0x05AC, 0x004C})

# INPUT_PROP_* bits from linux/input-event-codes.h.
PROP_POINTER = 1 << 0
PROP_DIRECT = 1 << 1

# KWin's stored values, as KWin itself writes them when the profile is set on
# the live device object. Not libinput's enum, which numbers them the other way.
PROFILE_FLAT = 1
PROFILE_ADAPTIVE = 2


class PlasmaToolsMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class Pointer:
    """A pointer device as KWin keys it in kcminputrc."""

    name: str
    vendor: int
    product: int
    sys_name: str = ""          # "event14": KWin's object path for this device
    touchpad: bool = False

    @property
    def groups(self) -> list[str]:
        # KConfig stores the ids in decimal, not hex.
        return ["Libinput", str(self.vendor), str(self.product), self.name]

    def __str__(self) -> str:
        return self.name


@dataclass
class Settings:
    """What KWin currently has for one device. None where it has nothing."""

    natural_scroll: bool | None = None
    scroll_factor: float | None = None
    pointer_acceleration: float | None = None
    flat_profile: bool | None = None
    supports_flat: bool = False


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def _is_pointer(device: Path) -> tuple[bool, bool]:
    """(pointer, touchpad) for a /sys/class/input/eventN/device.

    Relative axes mark a mouse. A touchpad has none -- the Magic Trackpad
    reports absolute multitouch axes and nothing relative, which is why
    filtering on REL alone hid it from this whole module. Absolute axes alone
    would also catch touchscreens and tablets, so the POINTER property has to
    be set and DIRECT (a device you touch where the cursor is) must not be.
    """
    if _read(device / "capabilities" / "rel").strip("0 "):
        return True, False
    if not _read(device / "capabilities" / "abs").strip("0 "):
        return False, False
    try:
        props = int(_read(device / "properties") or "0", 16)
    except ValueError:
        return False, False
    if props & PROP_DIRECT or not props & PROP_POINTER:
        return False, False
    return True, True


def pointers(apple_only: bool = True) -> list[Pointer]:
    """Pointer devices, deduplicated by identity."""
    found: dict[tuple, Pointer] = {}
    if not INPUT_CLASS.is_dir():
        return []

    for event in sorted(INPUT_CLASS.glob("event*")):
        device = event / "device"
        is_pointer, touchpad = _is_pointer(device)
        if not is_pointer:
            continue
        name = _read(device / "name")
        vendor_raw = _read(device / "id" / "vendor")
        product_raw = _read(device / "id" / "product")
        if not (name and vendor_raw and product_raw):
            continue
        vendor, product = int(vendor_raw, 16), int(product_raw, 16)
        if apple_only and vendor not in APPLE_VENDORS:
            continue
        found[(vendor, product, name)] = Pointer(
            name, vendor, product, sys_name=event.name, touchpad=touchpad,
        )
    return list(found.values())


def _as_user(command: list[str]) -> list[str]:
    """Run a KConfig or D-Bus tool as the real user.

    kreadconfig6/kwriteconfig6 resolve the config through $HOME, which sudo
    resets to /root. Run as root they would read and write root's
    kcminputrc -- a file KWin never looks at -- and report success. The session
    bus address is passed for the same reason: root's bus is not the session's.
    """
    if os.geteuid() != 0:
        return command
    user = os.environ.get("SUDO_USER")
    if not user:
        return command
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        return command
    runtime = f"/run/user/{entry.pw_uid}"
    return ["sudo", "-u", user, f"HOME={entry.pw_dir}",
            f"XDG_RUNTIME_DIR={runtime}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus", *command]


def _tool(name: str) -> str:
    for candidate in (f"{name}6", f"{name}5", name):
        if path := shutil.which(candidate):
            return path
    raise PlasmaToolsMissing(f"{name}6 not found - is kde-cli-tools installed?")


def _group_args(pointer: Pointer) -> list[str]:
    args: list[str] = []
    for group in pointer.groups:
        args += ["--group", group]
    return args


def _run(command: list[str], timeout: int = 10) -> str:
    try:
        done = subprocess.run(_as_user(command), capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip()


def _config_read(pointer: Pointer, key: str) -> str:
    try:
        tool = _tool("kreadconfig")
    except PlasmaToolsMissing:
        return ""
    return _run([tool, "--file", CONFIG, *_group_args(pointer), "--key", key])


def _config_write(pointer: Pointer, key: str, value: str) -> None:
    subprocess.run(
        _as_user([_tool("kwriteconfig"), "--file", CONFIG,
                  *_group_args(pointer), "--key", key, value]),
        capture_output=True, text=True, timeout=10, check=True,
    )


# --- KWin's live device object ---------------------------------------------

def _gdbus() -> str:
    if path := shutil.which("gdbus"):
        return path
    raise PlasmaToolsMissing("gdbus not found - is glib2 installed?")


def _device_path(pointer: Pointer) -> str:
    if not pointer.sys_name:
        raise PlasmaToolsMissing("device has no sysfs name, so no KWin object")
    return f"/org/kde/KWin/InputDevice/{pointer.sys_name}"


def live(pointer: Pointer) -> dict[str, str]:
    """Every property KWin exposes for this device, as raw strings.

    KWin is the authority on the current state: kcminputrc only holds what has
    been written, so a device nobody has configured reads as empty there while
    KWin knows its defaults.
    """
    try:
        out = _run([_gdbus(), "call", "--session", "-d", "org.kde.KWin",
                    "-o", _device_path(pointer), "-m",
                    "org.freedesktop.DBus.Properties.GetAll", IFACE])
    except PlasmaToolsMissing:
        return {}
    found: dict[str, str] = {}
    for chunk in out.strip("()[],{} ").split(","):
        name, _, value = chunk.partition(":")
        name = name.strip().strip("'")
        value = value.strip().removeprefix("<").removesuffix(">").strip("'")
        if name and value:
            found[name] = value
    return found


def _live_set(pointer: Pointer, prop: str, value: str) -> bool:
    try:
        _run([_gdbus(), "call", "--session", "-d", "org.kde.KWin",
              "-o", _device_path(pointer), "-m",
              "org.freedesktop.DBus.Properties.Set", IFACE, prop, f"<{value}>"])
    except PlasmaToolsMissing:
        return False
    return True


def settings(pointer: Pointer) -> Settings:
    """Current state, from KWin where it answers and the config otherwise."""
    props = live(pointer)
    if props:
        def number(name: str) -> float | None:
            try:
                return float(props[name])
            except (KeyError, ValueError):
                return None
        return Settings(
            natural_scroll={"true": True, "false": False}.get(
                props.get("naturalScroll", "")),
            scroll_factor=number("scrollFactor"),
            pointer_acceleration=number("pointerAcceleration"),
            flat_profile={"true": True, "false": False}.get(
                props.get("pointerAccelerationProfileFlat", "")),
            supports_flat=props.get("supportsPointerAccelerationProfileFlat")
                          == "true",
        )

    def config_number(key: str) -> float | None:
        try:
            return float(_config_read(pointer, key))
        except ValueError:
            return None
    return Settings(
        natural_scroll={"true": True, "false": False}.get(
            _config_read(pointer, KEY).lower()),
        scroll_factor=config_number("ScrollFactor"),
        pointer_acceleration=config_number("PointerAcceleration"),
        flat_profile=_config_read(pointer, "PointerAccelerationProfile")
                     == str(PROFILE_FLAT),
    )


def natural_scroll(pointer: Pointer) -> bool | None:
    """Current setting, or None when KWin has never been told."""
    return settings(pointer).natural_scroll


def set_natural_scroll(pointer: Pointer, enabled: bool) -> None:
    """Write the setting and make KWin pick it up immediately."""
    _config_write(pointer, KEY, "true" if enabled else "false")
    reconfigure()


def set_scroll_factor(pointer: Pointer, factor: float) -> None:
    """Scroll multiplier. 1.0 is untouched; below that is slower."""
    if factor <= 0:
        raise ValueError("scroll factor must be positive")
    _config_write(pointer, "ScrollFactor", f"{factor:g}")
    reconfigure()


def set_pointer_acceleration(pointer: Pointer, speed: float) -> None:
    """Pointer speed, -1.0 to 1.0, where 0.0 is the device default."""
    if not -1.0 <= speed <= 1.0:
        raise ValueError("pointer acceleration must be between -1.0 and 1.0")
    _config_write(pointer, "PointerAcceleration", f"{speed:g}")
    reconfigure()


def set_flat_profile(pointer: Pointer, flat: bool) -> bool:
    """Switch the acceleration profile, live.

    Deliberately not a config write followed by a reconfigure: KWin resets the
    profile to the device default on reconfigure and ignores the stored value,
    so that route is a no-op. Setting it on the live object works, and KWin
    persists it to kcminputrc on its own. Nothing here may call reconfigure()
    afterwards -- that would undo it.
    """
    prop = ("pointerAccelerationProfileFlat" if flat
            else "pointerAccelerationProfileAdaptive")
    if not _live_set(pointer, prop, "true"):
        return False
    return settings(pointer).flat_profile is flat


def reconfigure() -> bool:
    """Ask KWin to re-read its configuration. Without this, nothing changes.

    Note that this resets the acceleration profile to the device default; see
    set_flat_profile().
    """
    for tool in ("qdbus6", "qdbus"):
        if path := shutil.which(tool):
            done = subprocess.run(
                _as_user([path, "org.kde.KWin", "/KWin",
                          "org.kde.KWin.reconfigure"]),
                capture_output=True, text=True, timeout=10, check=False,
            )
            return done.returncode == 0
    return False
