"""One-time privilege setup, so the GUI never needs root.

The driver parameters live in /sys/module/<mod>/parameters/ and are root-owned
0644. Rather than prompting for privilege every time a toggle is flipped, this
grants a group write access to a *whitelist* of those parameters, once.

Why that is defensible: every whitelisted parameter is an input-*behaviour*
toggle -- modifier layout, Fn-key mode, scroll rate. None grant code execution
or data access. The worst a hostile process in the group can do is rearrange
your modifiers or make scrolling strange. One honest edge: swap_ctrl_cmd also
applies at the lock screen, so modifier behaviour could be altered during
password entry. Low severity, but users should be told.

Three pieces are installed:

* a helper script, run as root, which chgrps and chmods only the whitelist
* a udev rule firing that helper when either module loads
* a boot-time oneshot, because modules already loaded before udev replays do
  not reliably emit an "add" event

Distro packagers will generally refuse a package that chmods sysfs. For that
route, expose the same whitelist over a D-Bus system service guarded by a
polkit action; this module is deliberately the only place that needs root.
"""

from __future__ import annotations

import grp
import os
import pwd
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

GROUP = "magicpomme"
HELPER = Path("/usr/libexec/magic-pomme/grant-access")
UDEV_RULE = Path("/etc/udev/rules.d/60-magic-pomme.rules")
SERVICE = Path("/etc/systemd/system/magic-pomme-permissions.service")

# Exactly what the helper is permitted to loosen. Keep this narrow and
# auditable: it is the security boundary of the whole scheme.
WHITELIST: dict[str, tuple[str, ...]] = {
    "hid_apple": ("swap_ctrl_cmd", "fnmode", "iso_layout",
                  "swap_opt_cmd", "swap_fn_leftctrl"),
    "hid_magicmouse": ("scroll_speed", "scroll_acceleration",
                       "emulate_3button", "emulate_scroll_wheel"),
}
# Groups the user needs: ours for sysfs, keyd for live remapping.
REQUIRED_GROUPS = (GROUP, "keyd")


def helper_script() -> str:
    """The root-run helper. Invoked by udev, so it validates its own input.

    The module name is matched against a fixed case list and parameter names
    are never taken from the caller -- udev arguments are not trusted input.
    """
    cases = "\n".join(
        f"  {module})\n    PARAMS='{' '.join(params)}' ;;"
        for module, params in WHITELIST.items()
    )
    return f"""#!/bin/sh
# Installed by `magic-pomme setup`. Grants group '{GROUP}' write access to a
# fixed whitelist of Apple HID driver parameters. Run as root by udev and by
# magic-pomme-permissions.service.
set -eu

GROUP={GROUP}

case "${{1:-}}" in
{cases}
  *)
    echo "grant-access: refusing unknown module '${{1:-}}'" >&2
    exit 1 ;;
esac

# No group means setup was removed; do nothing rather than fail the udev event.
getent group "$GROUP" >/dev/null 2>&1 || exit 0

for param in $PARAMS; do
  file="/sys/module/$1/parameters/$param"
  [ -f "$file" ] || continue
  chgrp "$GROUP" "$file" 2>/dev/null || continue
  chmod g+w "$file" 2>/dev/null || true
done
"""


def udev_rule() -> str:
    lines = [
        "# Installed by `magic-pomme setup`.",
        "#",
        "# /sys/module is a udev subsystem, so module load can be matched here.",
        "# Module parameters are plain sysfs attributes rather than device",
        "# nodes, so udev's GROUP=/MODE= assignments do not apply and the",
        "# permissions have to be set by a helper.",
        "",
    ]
    lines += [
        f'ACTION=="add", SUBSYSTEM=="module", KERNEL=="{module}", '
        f'RUN+="{HELPER} {module}"'
        for module in WHITELIST
    ]
    lines += [
        "",
        "# Unrelated to permissions, but it belongs with the hardware: USB",
        "# autosuspend on a Bluetooth adapter adds noticeable input lag, which",
        "# presents as 'Apple keyboards feel sluggish on Linux'. Interface",
        "# class e0 is Wireless Controller, matching adapters generically",
        "# rather than hardcoding a vendor id.",
        'ACTION=="add", SUBSYSTEM=="usb", ATTR{bInterfaceClass}=="e0", '
        'TEST=="power/control", ATTR{power/control}="on"',
        "",
    ]
    return "\n".join(lines)


def service_unit() -> str:
    execs = "\n".join(f"ExecStart={HELPER} {module}" for module in WHITELIST)
    return f"""[Unit]
Description=magic-pomme: grant group access to Apple HID driver parameters
Documentation=https://github.com/theseal666/magic-pomme
# The udev rule covers hotplug. This covers the boot case, where a module may
# already be loaded before udev replays events.
After=systemd-udev-settle.service

[Service]
Type=oneshot
RemainAfterExit=yes
{execs}

[Install]
WantedBy=multi-user.target
"""


def files() -> dict[Path, tuple[str, int]]:
    """Every file setup installs, as path -> (contents, mode)."""
    return {
        HELPER: (helper_script(), 0o755),
        UDEV_RULE: (udev_rule(), 0o644),
        SERVICE: (service_unit(), 0o644),
    }


@dataclass
class Status:
    group_exists: bool
    missing_groups: list[str] = field(default_factory=list)
    stale_groups: list[str] = field(default_factory=list)
    installed: list[Path] = field(default_factory=list)
    absent: list[Path] = field(default_factory=list)
    writable: list[str] = field(default_factory=list)
    unwritable: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.absent and not self.missing_groups and not self.unwritable

    @property
    def needs_relogin(self) -> bool:
        """Files and groups are right, but this session predates the change."""
        return bool(self.stale_groups) and not self.missing_groups


def target_user() -> str:
    """The real user, even under sudo, so setup adds the right account."""
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name


def status(user: str | None = None) -> Status:
    user = user or target_user()
    report = Status(group_exists=_group_exists(GROUP))

    for group in REQUIRED_GROUPS:
        if not _group_exists(group):
            report.missing_groups.append(group)
            continue
        entry = grp.getgrnam(group)
        if user not in entry.gr_mem and entry.gr_gid != _primary_gid(user):
            report.missing_groups.append(group)
        elif entry.gr_gid not in os.getgroups() and entry.gr_gid != os.getgid():
            report.stale_groups.append(group)

    for path in files():
        (report.installed if path.exists() else report.absent).append(path)

    for module, params in WHITELIST.items():
        for param in params:
            file = Path(f"/sys/module/{module}/parameters/{param}")
            if not file.exists():
                continue
            target = report.writable if os.access(file, os.W_OK) else report.unwritable
            target.append(f"{module}.{param}")
    return report


def _group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
    except KeyError:
        return False
    return True


def _primary_gid(user: str) -> int | None:
    try:
        return pwd.getpwnam(user).pw_gid
    except KeyError:
        return None


def install(user: str | None = None) -> list[str]:
    """Create the group, write the files, enable the unit. Requires root."""
    if os.geteuid() != 0:
        raise PermissionError("setup --install must run as root (use sudo)")
    user = user or target_user()
    done: list[str] = []

    if not _group_exists(GROUP):
        subprocess.run(["groupadd", "--system", GROUP], check=True)
        done.append(f"created group {GROUP}")

    for group in REQUIRED_GROUPS:
        if _group_exists(group) and user not in grp.getgrnam(group).gr_mem:
            subprocess.run(["usermod", "-aG", group, user], check=True)
            done.append(f"added {user} to {group}")

    for path, (content, mode) in files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
        done.append(f"wrote {path}")

    if shutil.which("udevadm"):
        subprocess.run(["udevadm", "control", "--reload-rules"], check=False)
        done.append("reloaded udev rules")
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "--now", SERVICE.name], check=False)
        done.append(f"enabled {SERVICE.name}")
    return done


def uninstall() -> list[str]:
    """Remove what install() added, leaving the group and its members alone."""
    if os.geteuid() != 0:
        raise PermissionError("setup --uninstall must run as root (use sudo)")
    done: list[str] = []
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "disable", "--now", SERVICE.name], check=False)
    for path in files():
        if path.exists():
            path.unlink()
            done.append(f"removed {path}")
    if shutil.which("udevadm"):
        subprocess.run(["udevadm", "control", "--reload-rules"], check=False)
    done.append(f"group {GROUP} and its members were left in place")
    return done
