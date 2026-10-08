"""Profiles: the whole macOS-like setup as one reversible operation.

The product is not eleven switches, it is the bundle. `profile mac` should
give someone the result that otherwise takes a day of reading kernel docs,
keyd syntax and KDE shortcut internals.

Two constraints shape everything here.

**Reversibility.** Several targets are shared files full of the user's own
settings -- `kglobalshortcutsrc` especially. Nothing is overwritten without
first recording what was there, so `profile pc` restores rather than guesses.
Whole-file targets keep a backup; targets where we only touch some keys record
just those keys' previous values.

**Not everything applies live.** Driver parameters and keyd bindings take
effect immediately. The keyboard layout needs the compositor restarted, and
group membership needs a reboot where systemd lingering is on. An action
declares its activation so the caller can tell the user what is pending
instead of appearing to have done nothing.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import subprocess

from . import drivers, keyd, plasma_input

def target_user() -> str:
    """The real user, even under sudo."""
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or pwd.getpwuid(
        os.getuid()).pw_name


def user_home() -> Path:
    """The real user's home.

    Path.home() follows $HOME, which sudo resets to /root. Using it meant
    every user-level action wrote into root's home and reported success while
    changing nothing the user would ever see.
    """
    try:
        return Path(pwd.getpwnam(target_user()).pw_dir)
    except KeyError:
        return Path.home()


def _chown_to_user(path: Path) -> None:
    """Give a file back to the user when we wrote it as root."""
    if os.geteuid() != 0:
        return
    try:
        entry = pwd.getpwnam(target_user())
    except KeyError:
        return
    try:
        os.chown(path, entry.pw_uid, entry.pw_gid)
        for parent in (path.parent, path.parent.parent):
            if str(parent).startswith(str(user_home())) and parent.exists():
                os.chown(parent, entry.pw_uid, entry.pw_gid)
    except OSError:
        pass


STATE_DIR = Path(
    os.environ.get("XDG_STATE_HOME", user_home() / ".local/state")
) / "magic-pomme"
STATE_FILE = STATE_DIR / "state.json"
BACKUP_DIR = STATE_DIR / "backups"

LIVE = "live"
COMPOSITOR = "needs the compositor restarted (log out and in)"
RELOGIN = "needs a new session"


class Action(Protocol):
    """One reversible piece of a profile."""

    key: str
    description: str
    activation: str
    needs_root: bool

    def applied(self) -> bool: ...
    def apply(self, state: State) -> None: ...
    def revert(self, state: State) -> None: ...


@dataclass
class State:
    """What the profile changed, so it can be put back."""

    data: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> State:
        try:
            return cls(json.loads(STATE_FILE.read_text()))
        except (OSError, ValueError):
            return cls()

    def save(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.data, indent=2, sort_keys=True))

    def record(self, key: str, payload: dict) -> None:
        self.data.setdefault("actions", {})[key] = {
            "recorded": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **payload,
        }

    def recall(self, key: str) -> dict | None:
        return self.data.get("actions", {}).get(key)

    def forget(self, key: str) -> None:
        self.data.get("actions", {}).pop(key, None)

    @property
    def profile(self) -> str | None:
        return self.data.get("profile")

    @profile.setter
    def profile(self, name: str | None) -> None:
        self.data["profile"] = name


@dataclass
class DriverParams:
    """Kernel module parameters. Live, but needs the setup group."""

    key: str
    description: str
    values: dict[tuple[str, str], int]
    activation: str = LIVE
    needs_root: bool = False

    def applied(self) -> bool:
        return all(
            drivers.read(module, param) == want
            for (module, param), want in self.values.items()
        )

    def apply(self, state: State) -> None:
        previous = {
            f"{module}.{param}": drivers.read(module, param)
            for (module, param) in self.values
        }
        for (module, param), want in self.values.items():
            drivers.write(module, param, want)
        state.record(self.key, {"previous": previous})

    def revert(self, state: State) -> None:
        record = state.recall(self.key)
        for name, value in (record or {}).get("previous", {}).items():
            module, param = name.split(".", 1)
            if value is not None:
                drivers.write(module, param, value)
        state.forget(self.key)


@dataclass
class NaturalScroll:
    """Per-device Plasma setting. Live."""

    key: str = "natural-scroll"
    description: str = "Natural scrolling for Apple pointers"
    activation: str = LIVE
    needs_root: bool = False
    enabled: bool = True

    def applied(self) -> bool:
        pointers = plasma_input.pointers()
        return bool(pointers) and all(
            plasma_input.natural_scroll(p) is self.enabled for p in pointers
        )

    def apply(self, state: State) -> None:
        previous = {}
        for pointer in plasma_input.pointers():
            previous["|".join(pointer.groups)] = plasma_input.natural_scroll(pointer)
            plasma_input.set_natural_scroll(pointer, self.enabled)
        state.record(self.key, {"previous": previous})

    def revert(self, state: State) -> None:
        record = state.recall(self.key) or {}
        for pointer in plasma_input.pointers():
            was = record.get("previous", {}).get("|".join(pointer.groups))
            # None means the key was never set; restoring the Plasma default
            # is closer to "as we found it" than forcing False.
            plasma_input.set_natural_scroll(pointer, bool(was))
        state.forget(self.key)


@dataclass
class ManagedFile:
    """A whole file we own. The original is backed up before first write."""

    key: str
    description: str
    path: Path
    content: str
    activation: str = LIVE
    needs_root: bool = False
    after: str = ""          # a shell command to run once written, if any

    def applied(self) -> bool:
        try:
            return self.path.read_text() == self.content
        except OSError:
            return False

    def apply(self, state: State) -> None:
        if state.recall(self.key) is None:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                backup = BACKUP_DIR / f"{self.key}{self.path.suffix or '.bak'}"
                shutil.copy2(self.path, backup)
                state.record(self.key, {"backup": str(backup),
                                        "existed": True, "path": str(self.path)})
            else:
                state.record(self.key, {"existed": False, "path": str(self.path)})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.content)
        _chown_to_user(self.path)

    def revert(self, state: State) -> None:
        record = state.recall(self.key)
        if record is None:
            return
        if record.get("existed") and (backup := record.get("backup")):
            if Path(backup).exists():
                shutil.copy2(backup, self.path)
        elif self.path.exists():
            self.path.unlink()
        state.forget(self.key)


@dataclass
class Profile:
    name: str
    description: str
    actions: list[Action]

    def status(self) -> list[tuple[Action, bool]]:
        return [(action, action.applied()) for action in self.actions]

    @property
    def pending(self) -> list[Action]:
        return [action for action in self.actions if not action.applied()]


# After the modifier swap, "Ctrl+" below is what you press as Cmd, and "Meta+"
# is the physical Ctrl key -- which is how macOS splits them too: Cmd for app
# actions, Ctrl for Mission Control and Spaces.
SHORTCUTS: dict[str, dict[str, str]] = {
    "kwin": {
        "Overview": "Meta+Up",                               # Mission Control
        "ExposeClass": "Meta+Down",                          # App Expose
        "Switch One Desktop to the Left": "Meta+Left",       # Spaces
        "Switch One Desktop to the Right": "Meta+Right",
        "Window Quick Tile Left": "Meta+Alt+Left",           # moved off the arrows
        "Window Quick Tile Right": "Meta+Alt+Right",
        "Window Quick Tile Top": "Meta+Alt+Up",
        "Window Quick Tile Bottom": "Meta+Alt+Down",
        "Kill Window": "Ctrl+Alt+Esc",                       # Cmd+Opt+Esc
        "Walk Through Windows": "Ctrl+Tab\\tAlt+Tab",        # Cmd+Tab
        "Walk Through Windows (Reverse)": "Ctrl+Shift+Tab\\tAlt+Shift+Tab",
        "Walk Through Windows of Current Application": "Ctrl+`",
        # Freed so the terminal can receive physical Ctrl+<letter>.
        "Show Desktop": "none",
        "Grid View": "none",
        "Edit Tiles": "none",
    },
    "ksmserver": {
        "Lock Session": "Ctrl+Meta+Q",                       # Cmd+Ctrl+Q
    },
    "plasmashell": {
        # Bare Meta opened the launcher, which fires on every tap of the
        # physical Ctrl key once the modifiers are swapped.
        "activate application launcher": "Alt+F1",
        "show-on-mouse-pos": "Ctrl+Meta+V",
        "manage activities": "none",
        "next activity": "none",
        "stop current activity": "none",
    },
}
SHORTCUT_ADDITIONS: dict[str, dict[str, tuple[str, str]]] = {
    "org.kde.krunner.desktop": {
        "_launch": ("Ctrl+Space", "KRunner"),                # Spotlight
    },
    "org.kde.spectacle.desktop": {
        "FullScreenScreenShot": ("Ctrl+Shift+3", "Capture Entire Desktop"),
        "RectangularRegionScreenShot": ("Ctrl+Shift+4", "Capture Rectangular Region"),
        "_launch": ("Ctrl+Shift+5", "Launch Spectacle"),
    },
}


# KDE stores modifiers in its own canonical order and rewrites whatever you
# give it, so "Ctrl+Meta+Q" comes back as "Meta+Ctrl+Q". Comparing raw strings
# reports a correctly-applied shortcut as pending.
MODIFIER_ORDER = ("Meta", "Ctrl", "Alt", "Shift")


def normalise_shortcut(value: str) -> str:
    """Canonical form, so comparisons survive KDE reordering modifiers."""
    out = []
    for alternative in value.split("\\t"):
        parts = alternative.split("+")
        mods = [m for m in MODIFIER_ORDER if m in parts]
        rest = [p for p in parts if p not in MODIFIER_ORDER]
        out.append("+".join(mods + rest))
    return "\\t".join(out)


def _kglobalaccel(action: str) -> None:
    """Stop or start the shortcut daemon.

    kglobalshortcutsrc must not be edited while kglobalacceld runs: it holds
    the bindings in memory and writes them back over any external change. It
    also resolves conflicts on its own, so reassigning one key can silently
    clear another.
    """
    unit = "plasma-kglobalaccel.service"
    command = ["systemctl", "--user", action, unit]
    if os.geteuid() == 0:
        # `systemctl --user` as root targets root's own manager, not the
        # desktop user's, so it would stop a daemon nobody is running.
        user = target_user()
        try:
            uid = pwd.getpwnam(user).pw_uid
        except KeyError:
            return
        command = ["sudo", "-u", user,
                   f"XDG_RUNTIME_DIR=/run/user/{uid}", *command]
    subprocess.run(command, capture_output=True, check=False, timeout=15)


@dataclass
class KdeShortcuts:
    """Rewrite specific keys in kglobalshortcutsrc.

    This file is shared and full of the user's own bindings, so it is never
    replaced wholesale. Only the keys below are touched, and each one's
    previous value is recorded so revert puts exactly those back and leaves
    everything else alone.
    """

    key: str = "kde-shortcuts"
    description: str = "macOS global shortcuts (Spotlight, Mission Control, screenshots)"
    activation: str = RELOGIN
    needs_root: bool = False
    path: Path = field(
        default_factory=lambda: user_home() / ".config/kglobalshortcutsrc"
    )

    def _current(self) -> dict[str, dict[str, str]]:
        found: dict[str, dict[str, str]] = {}
        section = None
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return found
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1]
            elif section in SHORTCUTS and "=" in line:
                name, value = line.split("=", 1)
                if name in SHORTCUTS[section]:
                    found.setdefault(section, {})[name] = normalise_shortcut(
                        value.split(",")[0])
        return found

    def applied(self) -> bool:
        current = self._current()
        return all(
            current.get(section, {}).get(name) == normalise_shortcut(want)
            for section, entries in SHORTCUTS.items()
            for name, want in entries.items()
        ) and all(
            f"[{section}]" in self.path.read_text()
            for section in SHORTCUT_ADDITIONS
        ) if self.path.exists() else False

    def apply(self, state: State) -> None:
        if state.recall(self.key) is None:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            backup = BACKUP_DIR / "kglobalshortcutsrc"
            if self.path.exists():
                shutil.copy2(self.path, backup)
            state.record(self.key, {"backup": str(backup),
                                    "previous": self._current(),
                                    "existed": self.path.exists()})
        _kglobalaccel("stop")
        try:
            self._write(SHORTCUTS)
        finally:
            _kglobalaccel("start")

    def revert(self, state: State) -> None:
        record = state.recall(self.key)
        if record is None:
            return
        previous = record.get("previous", {})
        if previous:
            _kglobalaccel("stop")
            try:
                self._write(previous)
            finally:
                _kglobalaccel("start")
        state.forget(self.key)

    def _write(self, changes: dict[str, dict[str, str]]) -> None:
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return
        out, section = [], None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1]
            elif section in changes and "=" in line:
                name, value = line.split("=", 1)
                if name in changes[section]:
                    parts = value.split(",")
                    default = parts[1] if len(parts) > 1 else ""
                    label = parts[2] if len(parts) > 2 else ""
                    out.append(f"{name}={changes[section][name]},{default},{label}")
                    continue
            out.append(line)

        text = "\n".join(out)
        for section, entries in SHORTCUT_ADDITIONS.items():
            if f"[{section}]" not in text:
                block = "".join(
                    f"{name}={combo},none,{label}\n"
                    for name, (combo, label) in entries.items()
                )
                text = text.rstrip("\n") + f"\n\n[{section}]\n{block}"
        self.path.write_text(text)
        _chown_to_user(self.path)


def _payload(name: str) -> str:
    return (Path(__file__).parent / "profiles_data" / name).read_text()


def mac() -> Profile:
    """The macOS-like bundle."""
    home = user_home()
    return Profile(
        name="mac",
        description="macOS keyboard, mouse and shortcuts",
        actions=[
            DriverParams(
                key="driver-mac",
                description="Cmd acts as Ctrl; media keys on the top row",
                values={
                    ("hid_apple", "swap_ctrl_cmd"): 1,
                    ("hid_apple", "fnmode"): 1,
                    ("hid_magicmouse", "scroll_acceleration"): 1,
                    ("hid_magicmouse", "emulate_3button"): 1,
                },
            ),
            NaturalScroll(enabled=True),
            ManagedFile(
                key="keyd-mac",
                description="Mac text navigation (Cmd+arrows, Option+arrows)",
                path=Path("/etc/keyd/default.conf"),
                content=_payload("keyd-mac.conf"),
                needs_root=True,
            ),
            ManagedFile(
                key="kitty-mac",
                description="Terminal: Cmd+C copies, physical Ctrl still works",
                path=home / ".config/kitty/kitty.conf",
                content=_payload("kitty-mac.conf"),
            ),
            ManagedFile(
                key="layout-mac",
                description="Swedish Mac keyboard layout",
                path=home / ".config/kxkbrc",
                content=_payload("kxkbrc-mac"),
                activation=COMPOSITOR,
            ),
            KdeShortcuts(),
        ],
    )


def pc() -> Profile:
    """Linux defaults: the inverse of the driver changes, everything else reverted."""
    return Profile(
        name="pc",
        description="Linux defaults",
        actions=[
            DriverParams(
                key="driver-pc",
                description="Ctrl is Ctrl; F-keys on the top row",
                values={
                    ("hid_apple", "swap_ctrl_cmd"): 0,
                    ("hid_apple", "fnmode"): 2,
                },
            ),
        ],
    )


PROFILES = {"mac": mac, "pc": pc}
