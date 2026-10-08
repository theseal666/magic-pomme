"""Plasma pointer settings, for the ones that belong next to the driver knobs.

Natural scrolling -- content follows the fingers, as macOS does by default --
is not a driver parameter. It lives in KWin's input configuration, keyed per
device in ``kcminputrc`` under a nested group of the form::

    [Libinput][<vendor-decimal>][<product-decimal>][<device name>]
    NaturalScroll=true

Read and written through kreadconfig6/kwriteconfig6 rather than by parsing the
file, because Plasma's nested group syntax is not standard INI and KConfig
owns the escaping. KWin is then asked to re-read its configuration; without
that the change only takes effect at the next login.
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
KEY = "NaturalScroll"

# Apple's USB vendor id and the different one it advertises over Bluetooth.
APPLE_VENDORS = frozenset({0x05AC, 0x004C})


class PlasmaToolsMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class Pointer:
    """A pointer device as KWin keys it in kcminputrc."""

    name: str
    vendor: int
    product: int

    @property
    def groups(self) -> list[str]:
        # KConfig stores the ids in decimal, not hex.
        return ["Libinput", str(self.vendor), str(self.product), self.name]

    def __str__(self) -> str:
        return self.name


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def pointers(apple_only: bool = True) -> list[Pointer]:
    """Pointer devices, deduplicated by identity."""
    found: dict[tuple, Pointer] = {}
    if not INPUT_CLASS.is_dir():
        return []

    for event in sorted(INPUT_CLASS.glob("event*")):
        device = event / "device"
        # REL_* capabilities mark a relative pointing device; keyboards lack them.
        if not _read(device / "capabilities" / "rel").strip("0 "):
            continue
        name = _read(device / "name")
        vendor_raw = _read(device / "id" / "vendor")
        product_raw = _read(device / "id" / "product")
        if not (name and vendor_raw and product_raw):
            continue
        vendor, product = int(vendor_raw, 16), int(product_raw, 16)
        if apple_only and vendor not in APPLE_VENDORS:
            continue
        found[(vendor, product, name)] = Pointer(name, vendor, product)
    return list(found.values())


def _as_user(command: list[str]) -> list[str]:
    """Run a KConfig tool as the real user.

    kreadconfig6/kwriteconfig6 resolve the config through $HOME, which sudo
    resets to /root. Run as root they would read and write root's
    kcminputrc -- a file KWin never looks at -- and report success.
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
    return ["sudo", "-u", user, f"HOME={entry.pw_dir}",
            f"XDG_RUNTIME_DIR=/run/user/{entry.pw_uid}", *command]


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


def natural_scroll(pointer: Pointer) -> bool | None:
    """Current setting, or None when KWin has never been told."""
    try:
        done = subprocess.run(
            _as_user([_tool("kreadconfig"), "--file", CONFIG,
                      *_group_args(pointer), "--key", KEY]),
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (PlasmaToolsMissing, OSError, subprocess.SubprocessError):
        return None
    value = done.stdout.strip().lower()
    return {"true": True, "false": False}.get(value)


def set_natural_scroll(pointer: Pointer, enabled: bool) -> None:
    """Write the setting and make KWin pick it up immediately."""
    subprocess.run(
        _as_user([_tool("kwriteconfig"), "--file", CONFIG,
                  *_group_args(pointer), "--key", KEY,
                  "true" if enabled else "false"]),
        capture_output=True, text=True, timeout=10, check=True,
    )
    reconfigure()


def reconfigure() -> bool:
    """Ask KWin to re-read its configuration. Without this, nothing changes."""
    for tool in ("qdbus6", "qdbus"):
        if path := shutil.which(tool):
            done = subprocess.run(
                _as_user([path, "org.kde.KWin", "/KWin",
                          "org.kde.KWin.reconfigure"]),
                capture_output=True, text=True, timeout=10, check=False,
            )
            return done.returncode == 0
    return False
