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

Write them on KWin's device object, not into the file
-----------------------------------------------------

Writing the config and calling ``reconfigure`` does **not** apply any of
these. Measured on KWin 6.3.6: ``ScrollFactor`` set to 0.75 in kcminputrc,
followed by a reconfigure, left KWin reporting 0.5 -- and the same for
``PointerAcceleration``. The config is read when the session starts, so a
config write lands at the next login and looks like it worked. That is what
this module used to do for natural scrolling.

Setting the property on KWin's own device object
(``/org/kde/KWin/InputDevice/<sysname>``) applies immediately, and KWin writes
the key to kcminputrc itself. That is how the profile's stored values were
established by observation: 1 is flat and 2 is adaptive -- the reverse of
libinput's own enum, so do not take that as the mapping.

KWin only writes on an actual change, though: setting a property to the value
it already holds persists nothing, so a stale key in the file survives
untouched and wins at the next login. Measured with ScrollFactor at 0.75 in
the file while KWin ran 0.5. The setters here therefore write the config as
well -- same value, so the two cannot disagree.

``reconfigure`` is worse than useless here: it resets the acceleration profile
to the device default, so calling it after setting a flat profile undoes it.
Nothing in this module calls it. The config write is kept only as the fallback
for a session where KWin's object cannot be reached, where it is at least
read at the next login.
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
    """Write one key.

    The ``--`` is load-bearing. kwriteconfig6 parses with QCommandLineParser,
    so a value beginning with a dash is read as options: ``-0.25`` came back as
    "Unknown options: 0, ., 2, 5." and exit 1, which made every negative
    pointer speed fail. Positive values worked, so this hid until someone
    picked a value left of centre.
    """
    subprocess.run(
        _as_user([_tool("kwriteconfig"), "--file", CONFIG,
                  *_group_args(pointer), "--key", key, "--", value]),
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


def _apply_live(pointer: Pointer, prop: str, value: str,
                config_key: str, config_value: str) -> bool:
    """Set the property on KWin, or fall back to the config for next login.

    True means the running session changed. False means only the config was
    written, which takes effect when the session next starts -- the caller
    should say so rather than reporting success.
    """
    live = _live_set(pointer, prop, value)
    # Written either way: KWin persists only what changes, so this is what
    # keeps a stale key in the file from winning at the next login.
    _config_write(pointer, config_key, config_value)
    return live


def _double(value: float) -> str:
    """Format for gdbus, which types a bare 1 as int32 and rejects it."""
    return repr(float(value))


def set_natural_scroll(pointer: Pointer, enabled: bool) -> bool:
    """Natural scrolling: content follows the fingers, as macOS does."""
    return _apply_live(pointer, "naturalScroll", "true" if enabled else "false",
                       KEY, "true" if enabled else "false")


def set_scroll_factor(pointer: Pointer, factor: float) -> bool:
    """Scroll multiplier. 1.0 is untouched; below that is slower."""
    if factor <= 0:
        raise ValueError("scroll factor must be positive")
    return _apply_live(pointer, "scrollFactor", _double(factor),
                       "ScrollFactor", f"{factor:g}")


def set_pointer_acceleration(pointer: Pointer, speed: float) -> bool:
    """Pointer speed, -1.0 to 1.0, where 0.0 is the device default."""
    if not -1.0 <= speed <= 1.0:
        raise ValueError("pointer acceleration must be between -1.0 and 1.0")
    return _apply_live(pointer, "pointerAcceleration", _double(speed),
                       "PointerAcceleration", f"{speed:g}")


def set_flat_profile(pointer: Pointer, flat: bool) -> bool:
    """Switch the acceleration profile, live.

    As with every setting here, the config route does nothing; see the module
    docstring. Nothing may call reconfigure() afterwards either -- that resets
    the profile to the device default.
    """
    prop = ("pointerAccelerationProfileFlat" if flat
            else "pointerAccelerationProfileAdaptive")
    if not _live_set(pointer, prop, "true"):
        return False
    return settings(pointer).flat_profile is flat


def reconfigure() -> bool:
    """Ask KWin to re-read its configuration.

    Kept for callers outside this module. It does **not** apply the per-device
    pointer settings -- measured, see the module docstring -- and it resets the
    acceleration profile to the device default, so the setters here never use
    it.
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
