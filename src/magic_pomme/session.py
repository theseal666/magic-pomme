"""How a user picks up new group membership on this machine.

Supplementary groups are resolved when a process starts and inherited by its
children, so "log out and back in" is the usual advice. That advice is wrong
when systemd user lingering is enabled: `systemd --user` then survives logout
and keeps running with the group set it had at boot. Every desktop process is
spawned by it, so a fresh login inherits the stale groups and the change never
takes effect. Only a reboot -- or terminating the user manager outright --
actually refreshes it.

This was found the hard way: a login after the group change still showed the
old gids, because the user manager had been up for six days.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

LINGER_DIR = Path("/var/lib/systemd/linger")


def current_user() -> str:
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or ""


def linger_enabled(user: str | None = None) -> bool:
    """Whether systemd keeps this user's manager alive across logout."""
    user = user or current_user()
    if not user:
        return False
    if (LINGER_DIR / user).exists():
        return True
    try:
        done = subprocess.run(
            ["loginctl", "show-user", user, "-p", "Linger"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "Linger=yes" in done.stdout


def refresh_action(user: str | None = None) -> str:
    """Just the verb, for embedding mid-sentence."""
    return "reboot" if linger_enabled(user) else "log out and back in"


def refresh_hint(user: str | None = None) -> str:
    """The thing that will actually make new group membership take effect."""
    if linger_enabled(user):
        return (
            "reboot to pick up group membership - logging out is not enough "
            "on this machine, because systemd user lingering is enabled and "
            "`systemd --user` keeps the group set it started with"
        )
    return "log out and back in to pick up group membership"
