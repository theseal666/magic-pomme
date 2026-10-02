"""Thin wrapper over the keyd IPC.

keyd does the work that driver parameters cannot: per-device bindings (via its
``[ids]`` sections) and the mac text-navigation layers. Two platform details
matter here and are easy to get wrong:

* **Debian renames the binary to ``keyd.rvaiya``.** Upstream calls it ``keyd``.
  Both are probed.
* **The socket is group-owned by ``keyd``, so this needs no root.** A user in
  that group can apply bindings live. Group membership is resolved at login, so
  a freshly-added user needs a new session before it takes effect -- which is
  why :func:`diagnose` reports the live and on-disk answers separately.

Also worth knowing when generating bindings: **keyd runs below xkb, so binding
values are keycodes, not characters.** ``8 = [`` emits the US bracket keycode,
which under a Swedish layout produces ``å``.
"""

from __future__ import annotations

import grp
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

SOCKET = Path("/run/keyd.socket")
GROUP = "keyd"
BINARIES = ("keyd.rvaiya", "keyd")
CONFIG = Path("/etc/keyd/default.conf")

# If a bad config ever leaves the keyboard unusable, this kills keyd.
PANIC_SEQUENCE = "backspace + escape + enter"


class KeydUnavailable(RuntimeError):
    pass


@dataclass
class Readiness:
    """Whether live remapping is actually possible right now, and if not, why."""

    binary: str | None
    socket_exists: bool
    socket_writable: bool
    in_group_on_disk: bool
    in_group_this_session: bool

    @property
    def ok(self) -> bool:
        return bool(self.binary) and self.socket_writable

    @property
    def advice(self) -> str | None:
        if not self.binary:
            return "keyd is not installed (apt install keyd)"
        if not self.socket_exists:
            return "keyd is not running (systemctl enable --now keyd)"
        if self.socket_writable:
            return None
        if self.in_group_on_disk and not self.in_group_this_session:
            return (
                f"you are in the {GROUP!r} group but this session predates it - "
                f"log out and back in, or use: sg {GROUP} -c '...'"
            )
        if not self.in_group_on_disk:
            return f"add yourself to the group: sudo usermod -aG {GROUP} $USER"
        return f"{SOCKET} is not writable"


def binary() -> str:
    for name in BINARIES:
        if found := shutil.which(name):
            return found
    raise KeydUnavailable(f"none of {BINARIES} found on PATH")


def diagnose() -> Readiness:
    try:
        found = binary()
    except KeydUnavailable:
        found = None

    try:
        members = grp.getgrnam(GROUP).gr_mem
        user = os.environ.get("USER") or Path.home().name
        on_disk = user in members
    except KeyError:
        on_disk = False

    try:
        gid = grp.getgrnam(GROUP).gr_gid
        # getgroups() lists supplementary groups only, so a shell started with
        # `sg keyd` -- where keyd is the *primary* group -- needs getgid() too.
        this_session = gid in os.getgroups() or gid == os.getgid()
    except KeyError:
        this_session = False

    return Readiness(
        binary=found,
        socket_exists=SOCKET.exists(),
        socket_writable=os.access(SOCKET, os.W_OK),
        in_group_on_disk=on_disk,
        in_group_this_session=this_session,
    )


def _run(args: list[str], timeout: int = 10) -> str:
    done = subprocess.run(
        [binary(), *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip()
        raise KeydUnavailable(f"keyd {' '.join(args)} failed: {detail}")
    return done.stdout


def list_keys() -> list[str]:
    """Every valid key name. Useful for validating generated bindings."""
    return _run(["list-keys"]).split()


def bind(*bindings: str) -> None:
    """Apply bindings live, without touching the config file.

    e.g. ``bind("control.left = home", "alt.left = C-left")``
    Pass ``"reset"`` as the first binding to drop previous live bindings.
    """
    if not bindings:
        return
    _run(["bind", *bindings])


def reload() -> None:
    """Re-read the config files from disk."""
    _run(["reload"])


def do(expression: str) -> None:
    """Execute a macro expression, e.g. ``do("C-c")``."""
    _run(["do", expression])


def config_text() -> str | None:
    try:
        return CONFIG.read_text()
    except OSError:
        return None
