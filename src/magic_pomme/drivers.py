"""Read and write the hid_apple / hid_magicmouse module parameters.

These parameters are the whole reason this project exists: the kernel exposes
them, they take effect live, and no desktop surfaces them.

Two things to know before using this module:

* **They are module-global, not per-device.** One ``swap_ctrl_cmd`` covers
  every Apple keyboard attached at once. Per-device behaviour is impossible
  here and has to go through keyd's ``[ids]`` sections.
* **The two modules use different value formats.** hid_apple takes integers
  while hid_magicmouse takes ``Y``/``N``. Handled by :class:`Param.kind`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MODULES = Path("/sys/module")


@dataclass(frozen=True)
class Param:
    """One module parameter, with enough metadata to render a UI control."""

    name: str
    kind: str  # "int" | "bool"
    description: str
    choices: dict[int, str] | None = None
    minimum: int | None = None
    maximum: int | None = None

    def parse(self, raw: str) -> int | None:
        """Turn the sysfs text into an int, normalising Y/N to 1/0."""
        raw = raw.strip()
        if self.kind == "bool":
            if raw in ("Y", "y", "1"):
                return 1
            if raw in ("N", "n", "0"):
                return 0
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def format(self, value: int) -> str:
        """Turn an int back into what sysfs expects."""
        return ("Y" if value else "N") if self.kind == "bool" else str(value)

    def label(self, value: int | None) -> str:
        if value is None:
            return "unreadable"
        if self.choices and value in self.choices:
            return f"{value} ({self.choices[value]})"
        if self.kind == "bool":
            return "on" if value else "off"
        return str(value)

    def validate(self, value: int) -> None:
        if self.choices and value not in self.choices:
            allowed = ", ".join(str(key) for key in sorted(self.choices))
            raise ValueError(f"{self.name}: must be one of {allowed}, got {value}")
        if self.kind == "bool" and value not in (0, 1):
            raise ValueError(f"{self.name}: must be 0 or 1, got {value}")
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{self.name}: must be >= {self.minimum}, got {value}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"{self.name}: must be <= {self.maximum}, got {value}")


HID_APPLE = {
    "swap_ctrl_cmd": Param(
        "swap_ctrl_cmd",
        "int",
        "Cmd acts as Ctrl, and Ctrl acts as Super. The core of Mac mode.",
        choices={0: "off", 1: "on"},
    ),
    "fnmode": Param(
        "fnmode",
        "int",
        "Whether the top row is media keys or F-keys by default.",
        choices={
            0: "disabled",
            1: "media keys primary - matches the printed legends",
            2: "F-keys primary",
            3: "auto",
        },
    ),
    "iso_layout": Param(
        "iso_layout",
        "int",
        "Apple ISO quirk: swaps the key left of 1 with the key left of Z.",
        choices={-1: "auto", 0: "off", 1: "on"},
    ),
    "swap_opt_cmd": Param(
        "swap_opt_cmd",
        "int",
        "Swap Option and Cmd. Not used by Mac mode.",
        choices={0: "off", 1: "on"},
    ),
    "swap_fn_leftctrl": Param(
        "swap_fn_leftctrl",
        "int",
        "Swap Fn with left Ctrl.",
        choices={0: "off", 1: "on"},
    ),
}

HID_MAGICMOUSE = {
    "scroll_speed": Param(
        "scroll_speed", "int", "Scroll rate, 0-63.", minimum=0, maximum=63
    ),
    "scroll_acceleration": Param(
        "scroll_acceleration",
        "bool",
        "Accelerated scrolling, closer to the macOS feel.",
    ),
    "emulate_3button": Param(
        "emulate_3button", "bool", "Middle click via a centre-surface click."
    ),
    "emulate_scroll_wheel": Param(
        "emulate_scroll_wheel", "bool", "Report surface swipes as wheel events."
    ),
    "report_undeciphered": Param(
        "report_undeciphered", "bool", "Debug: emit raw unparsed HID data."
    ),
}

PARAMS: dict[str, dict[str, Param]] = {
    "hid_apple": HID_APPLE,
    "hid_magicmouse": HID_MAGICMOUSE,
}


def path_for(module: str, param: str) -> Path:
    return MODULES / module / "parameters" / param


def loaded(module: str) -> bool:
    return (MODULES / module).is_dir()


def spec(module: str, param: str) -> Param:
    try:
        return PARAMS[module][param]
    except KeyError:
        known = ", ".join(sorted(PARAMS.get(module, {})))
        raise KeyError(
            f"unknown parameter {module}.{param}"
            + (f" (known: {known})" if known else f" (unknown module {module})")
        ) from None


def read(module: str, param: str) -> int | None:
    """Current value, or None if the module is not loaded or unreadable."""
    try:
        raw = path_for(module, param).read_text()
    except OSError:
        return None
    return spec(module, param).parse(raw)


def read_all(module: str) -> dict[str, int | None]:
    return {name: read(module, name) for name in PARAMS.get(module, {})}


def writable(module: str, param: str) -> bool:
    return os.access(path_for(module, param), os.W_OK)


def write(module: str, param: str, value: int) -> None:
    """Apply a parameter. Takes effect immediately; needs root.

    Raises ValueError for a value the parameter does not accept, and
    PermissionError with actionable guidance when not privileged -- the GUI
    should never run as root, so this is expected to be routed through a
    helper that whitelists exactly these writes.
    """
    param_spec = spec(module, param)
    param_spec.validate(value)

    target = path_for(module, param)
    if not target.exists():
        raise FileNotFoundError(
            f"{target} not present - is the {module} module loaded?"
        )
    try:
        target.write_text(param_spec.format(value))
    except PermissionError:
        raise PermissionError(
            f"{target} is root-owned. Route this write through a privileged "
            f"helper rather than running the GUI as root."
        ) from None
