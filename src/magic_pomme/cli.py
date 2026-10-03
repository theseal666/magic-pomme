"""Command-line interface.

Exists so the whole library is exercisable before any UI is written, and so the
privileged and unprivileged halves stay visibly separate.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from . import (__version__, bluetooth, devices, drivers, keyd,
               permissions, plasma_input, session)

OK, BAD, WARN = "\033[32m", "\033[31m", "\033[33m"
DIM, BOLD, OFF = "\033[2m", "\033[1m", "\033[0m"


def _heading(text: str) -> None:
    print(f"\n{BOLD}{text}{OFF}")


def _bar(percent: int, width: int = 10) -> str:
    filled = round(percent / 100 * width)
    colour = OK if percent > 40 else (WARN if percent > 15 else BAD)
    return f"{colour}{'#' * filled}{DIM}{'.' * (width - filled)}{OFF}"


def cmd_devices(args: argparse.Namespace) -> int:
    found = devices.devices(with_battery=not getattr(args, 'no_battery', False))
    if not found:
        print("  no Apple input devices found")
        return 1
    for device in found:
        print(f"  {BOLD}{device.name}{OFF}")
        print(f"    transport   {device.transport}")
        print(f"    driver      {device.driver}"
              f"{'' if device.configurable else f' {DIM}(not tunable){OFF}'}")
        if device.module:
            print(f"    module      {device.module}")
        if device.serial:
            print(f"    serial      {device.serial}")
        if device.battery and device.battery.unknown:
            print(f"    battery     {DIM}awaiting first report since reconnect{OFF}")
        elif device.battery:
            state = "charging" if device.battery.charging else device.battery.status.lower()
            print(f"    battery     {_bar(device.battery.capacity)} "
                  f"{device.battery.capacity}% ({state})")
        print(f"    hid ids     {', '.join(device.hid_ids)}")
    return 0


def cmd_params(args: argparse.Namespace) -> int:
    modules = [args.module] if args.module else list(drivers.PARAMS)
    for module in modules:
        if module not in drivers.PARAMS:
            print(f"  unknown module {module!r}", file=sys.stderr)
            return 2
        mark = f"{OK}loaded{OFF}" if drivers.loaded(module) else f"{DIM}not loaded{OFF}"
        _heading(f"{module}  [{mark}]")
        for name, value in drivers.read_all(module).items():
            spec = drivers.spec(module, name)
            access = "rw" if drivers.writable(module, name) else "ro"
            print(f"  {name:<22} {spec.label(value):<52} {DIM}[{access}]{OFF}")
            print(f"  {DIM}{'':<22} {spec.description}{OFF}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    if "." not in args.target:
        print("  expected <module>.<param>, e.g. hid_apple.fnmode", file=sys.stderr)
        return 2
    module, param = args.target.split(".", 1)
    try:
        spec = drivers.spec(module, param)
        value = spec.parse(args.value)
        if value is None:
            print(f"  cannot parse {args.value!r} for {param}", file=sys.stderr)
            return 2
        before = drivers.read(module, param)
        if args.dry_run:
            print(f"  would set {module}.{param}: "
                  f"{spec.label(before)} -> {spec.label(value)}")
            return 0
        drivers.write(module, param, value)
    except (KeyError, ValueError) as error:
        print(f"  {BAD}{error}{OFF}", file=sys.stderr)
        return 2
    except PermissionError as error:
        print(f"  {BAD}{error}{OFF}", file=sys.stderr)
        print(f"  {DIM}retry with sudo, or route through the helper service{OFF}")
        return 13
    except FileNotFoundError as error:
        print(f"  {BAD}{error}{OFF}", file=sys.stderr)
        return 1
    print(f"  {OK}{module}.{param} = {spec.label(value)}{OFF} (applied live)")
    return 0


def cmd_keyd(_: argparse.Namespace) -> int:
    report = keyd.diagnose()
    mark = f"{OK}ready{OFF}" if report.ok else f"{WARN}not usable{OFF}"
    _heading(f"keyd  [{mark}]")
    print(f"  binary              {report.binary or f'{BAD}not found{OFF}'}")
    print(f"  socket              {keyd.SOCKET} "
          f"({'writable' if report.socket_writable else 'not writable'})")
    print(f"  group {keyd.GROUP!r:<13} on disk: {report.in_group_on_disk}   "
          f"this session: {report.in_group_this_session}")
    if report.advice:
        print(f"  {WARN}{report.advice}{OFF}")
    if report.ok:
        print(f"  key names           {len(keyd.list_keys())}")
    print(f"  {DIM}panic sequence: {keyd.PANIC_SEQUENCE}{OFF}")
    return 0 if report.ok else 1


def cmd_bt(args: argparse.Namespace) -> int:
    if not bluetooth.available():
        print(f"  {BAD}no powered Bluetooth adapter{OFF}", file=sys.stderr)
        return 1

    match args.bt_action:
        case "scan":
            print(f"  scanning {args.seconds}s ...")
            found = bluetooth.scan(args.seconds, apple_only=not args.all)
        case _:
            found = bluetooth.devices(apple_only=not args.all)

    if args.bt_action in ("list", "scan"):
        if not found:
            print("  nothing found"
                  f"{'' if args.all else ' (use --all for non-Apple devices)'}")
            return 1
        for device in found:
            flag = f"{OK}+{OFF}" if device.ready else " "
            print(f"  {flag} {device.mac}  {device}")
        return 0

    if not args.mac:
        print("  a MAC address is required", file=sys.stderr)
        return 2

    match args.bt_action:
        case "setup":
            ok, message = bluetooth.setup(args.mac)
            print(f"  {OK if ok else BAD}{message}{OFF}")
            return 0 if ok else 1
        case "connect":
            ok = bluetooth.connect(args.mac)
        case "disconnect":
            ok = bluetooth.disconnect(args.mac)
        case "forget":
            ok = bluetooth.remove(args.mac)
        case _:
            print(f"  unknown action {args.bt_action}", file=sys.stderr)
            return 2
    print(f"  {OK if ok else BAD}{args.bt_action}: {'ok' if ok else 'failed'}{OFF}")
    return 0 if ok else 1


def cmd_setup(args: argparse.Namespace) -> int:
    if args.print_only:
        print(f"# magic-pomme setup -- files that `--install` would write.\n"
              f"# Read them before running anything as root.\n")
        for path, (content, mode) in permissions.files().items():
            print(f"{BOLD}# ===== {path}  (mode {mode:o}) ====={OFF}")
            print(content)
        print(f"{BOLD}# ===== commands ====={OFF}")
        print(f"groupadd --system {permissions.GROUP}")
        for group in permissions.REQUIRED_GROUPS:
            print(f"usermod -aG {group} {permissions.target_user()}")
        print("udevadm control --reload-rules")
        print(f"systemctl daemon-reload && "
              f"systemctl enable --now {permissions.SERVICE.name}")
        return 0

    if args.uninstall:
        try:
            for line in permissions.uninstall():
                print(f"  {line}")
        except PermissionError as error:
            print(f"  {BAD}{error}{OFF}", file=sys.stderr)
            return 13
        return 0

    if args.install:
        try:
            for line in permissions.install():
                print(f"  {OK}+{OFF} {line}")
        except PermissionError as error:
            print(f"  {BAD}{error}{OFF}", file=sys.stderr)
            return 13
        except Exception as error:  # noqa: BLE001 - surface the real cause
            print(f"  {BAD}{error}{OFF}", file=sys.stderr)
            return 1
        print(f"\n  {OK}done{OFF} - {session.refresh_hint()}")
        return 0

    report = permissions.status()
    mark = f"{OK}complete{OFF}" if report.complete else f"{WARN}not set up{OFF}"
    _heading(f"setup  [{mark}]")

    for group in permissions.REQUIRED_GROUPS:
        if group in report.missing_groups:
            state = f"{BAD}missing{OFF}"
        elif group in report.stale_groups:
            state = f"{WARN}joined, needs re-login{OFF}"
        else:
            state = f"{OK}ok{OFF}"
        print(f"  group {group:<14} {state}")

    for path in permissions.files():
        state = f"{OK}installed{OFF}" if path in report.installed else f"{BAD}absent{OFF}"
        print(f"  {str(path):<52} {state}")

    total = len(report.writable) + len(report.unwritable)
    print(f"  writable parameters  {len(report.writable)}/{total}")

    if report.needs_relogin:
        print(f"\n  {WARN}{session.refresh_hint()}{OFF}")
    elif not report.complete:
        print(f"\n  review:   magic-pomme setup --print")
        exe = sys.argv[0] if os.path.isabs(sys.argv[0]) else shutil.which("magic-pomme") or "magic-pomme"
        # sudo resets PATH to secure_path, which excludes ~/.local/bin, so a
        # bare "sudo magic-pomme" fails for a pipx install.
        print(f"  apply:    sudo {exe} setup --install")
    return 0 if report.complete else 1


def cmd_scroll(args: argparse.Namespace) -> int:
    pointers = plasma_input.pointers()
    if not pointers:
        print("  no Apple pointing devices found", file=sys.stderr)
        return 1

    for pointer in pointers:
        if args.state is None:
            current = plasma_input.natural_scroll(pointer)
            label = {True: "on", False: "off", None: "not set (Plasma default)"}[current]
            print(f"  {pointer}  natural scrolling: {label}")
            continue
        try:
            plasma_input.set_natural_scroll(pointer, args.state == "on")
        except (plasma_input.PlasmaToolsMissing, OSError) as error:
            print(f"  {BAD}{error}{OFF}", file=sys.stderr)
            return 1
        print(f"  {OK}{pointer}: natural scrolling {args.state}{OFF}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _heading("devices")
    cmd_devices(args)
    cmd_params(argparse.Namespace(module=None))
    cmd_keyd(args)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="magic-pomme",
        description="Control for Apple Magic Keyboard & Magic Mouse on Linux.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subs = parser.add_subparsers(dest="command")

    subs.add_parser("status", help="everything at once (default)")
    listing = subs.add_parser("devices", help="connected Apple input devices")
    listing.add_argument(
        "--no-battery", action="store_true",
        help="skip battery, which blocks ~10s per unresponsive Bluetooth device")

    params = subs.add_parser("params", help="driver parameters")
    params.add_argument("module", nargs="?", help="hid_apple or hid_magicmouse")

    setter = subs.add_parser("set", help="apply a driver parameter (needs root)")
    setter.add_argument("target", help="<module>.<param>, e.g. hid_apple.fnmode")
    setter.add_argument("value")
    setter.add_argument("--dry-run", action="store_true")

    subs.add_parser("keyd", help="keyd readiness for live remapping")

    scroll = subs.add_parser(
        "scroll", help="natural scrolling (macOS direction) for Apple pointers")
    scroll.add_argument("state", nargs="?", choices=["on", "off"],
                        help="omit to show the current setting")

    setup = subs.add_parser(
        "setup", help="one-time privilege setup so the GUI never needs root"
    )
    setup.add_argument("--print", dest="print_only", action="store_true",
                       help="show the files and commands without changing anything")
    setup.add_argument("--install", action="store_true", help="apply (needs root)")
    setup.add_argument("--uninstall", action="store_true", help="undo (needs root)")

    bt = subs.add_parser("bt", help="Bluetooth devices and pairing")
    bt.add_argument(
        "bt_action",
        choices=["list", "scan", "setup", "connect", "disconnect", "forget"],
    )
    bt.add_argument("mac", nargs="?")
    bt.add_argument("--seconds", type=int, default=20, help="scan duration")
    bt.add_argument("--all", action="store_true", help="include non-Apple devices")

    args = parser.parse_args(argv)
    handlers = {
        None: cmd_status,
        "status": cmd_status,
        "devices": cmd_devices,
        "params": cmd_params,
        "set": cmd_set,
        "keyd": cmd_keyd,
        "bt": cmd_bt,
        "setup": cmd_setup,
        "scroll": cmd_scroll,
    }
    if args.command in ("devices", "status", "keyd") and not hasattr(args, "module"):
        args.module = None
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
