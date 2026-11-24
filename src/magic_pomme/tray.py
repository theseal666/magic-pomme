"""System tray applet.

PyQt6 rather than PySide6 because Debian ships python3-pyqt6 and it is already
present on the target; PySide6 would be an extra download for no gain. Qt talks
to Plasma through StatusNotifierItem, so this appears in the panel's system
tray rather than as a legacy XEmbed icon.

The applet is a thin view over the library: it reads devices and parameters,
and writes parameters. If `magic-pomme setup` has not been run the toggles are
shown but disabled, with the reason in the menu, rather than silently failing
or prompting for a password.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import (bluetooth, devices, drivers, permissions, plasma_input,
               session)

REFRESH_MS = 30_000
BATTERY_INTERVAL_S = 180.0
# A device paired while the applet runs has no battery reading yet. Asking for
# one early is cheap; asking on every menu open is not, because an unresponsive
# Bluetooth device blocks the read for ten seconds.
BATTERY_WAKE_FLOOR_S = 15.0
SCAN_SECONDS = 20
BT_CHECK_TTL_S = 60.0
ASSETS = Path(__file__).parent / "assets"

# Toggles worth reaching for from the panel. Everything else stays in the CLI:
# a tray menu that mirrors every parameter is a worse tray menu.
QUICK_TOGGLES = (
    ("hid_apple", "swap_ctrl_cmd", "Mac modifiers (Cmd acts as Ctrl)"),
    ("hid_magicmouse", "scroll_acceleration", "Accelerated scrolling"),
    ("hid_magicmouse", "emulate_3button", "Middle click"),
)
# Presets rather than sliders: the tray menu is exported to Plasma over
# DBusMenu, which carries menu items and not widgets, so a QSlider in here
# renders as nothing at all.
SCROLL_STEPS = (0.3, 0.5, 0.75, 1.0, 1.5)
SPEED_STEPS = (-0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0)

FN_LABELS = {
    1: "Media keys (match printed legends)",
    2: "F-keys",
    3: "Auto",
}


class BatteryWorker(QThread):
    """Reads batteries off the UI thread.

    A battery read blocks in the kernel until the device answers or times out,
    which is about ten seconds per unresponsive Bluetooth device. Doing that on
    the Qt main thread froze the applet for roughly half of every refresh
    cycle. Battery level also changes slowly, so it is polled far less often
    than the menu is rebuilt.
    """

    updated = pyqtSignal(dict)

    def __init__(self) -> None:
        super().__init__()
        self._stop = False
        self._wake = threading.Event()
        self._last_poll = 0.0

    def run(self) -> None:
        while not self._stop:
            try:
                self.updated.emit(devices.batteries())
            except OSError:
                pass
            self._last_poll = time.monotonic()
            self._wake.wait(BATTERY_INTERVAL_S)
            self._wake.clear()

    def poll_now(self) -> None:
        """Read batteries before the next scheduled poll.

        Without this, a device paired just after a poll shows
        "battery reading..." for the rest of the three-minute interval, which
        reads as a broken applet rather than as a pending read. The floor stops
        a menu that is opened repeatedly from queueing blocking reads.
        """
        if time.monotonic() - self._last_poll < BATTERY_WAKE_FLOOR_S:
            return
        self._wake.set()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()


def guarded(action) -> str | None:
    """Run a settings write, returning a message rather than raising.

    PyQt6 calls qFatal when an exception escapes a slot, so an unhandled error
    in a menu handler does not print a traceback and carry on -- it aborts the
    process with a core dump and takes the panel icon with it. That is how a
    kwriteconfig6 exit status killed the applet. Everything is caught here,
    deliberately including the subprocess errors that are not OSError, because
    no failed setting is worth the whole applet.
    """
    try:
        action()
    except Exception as error:          # noqa: BLE001 - see above
        return str(error) or error.__class__.__name__
    return None


class ScanWorker(QThread):
    """Scans off the UI thread: a scan window is twenty blocking seconds."""

    scanned = pyqtSignal(list)

    def __init__(self, seconds: int = SCAN_SECONDS) -> None:
        super().__init__()
        self._seconds = seconds

    def run(self) -> None:
        try:
            found = bluetooth.scan(self._seconds, apple_only=True)
        except (bluetooth.BluetoothUnavailable, OSError):
            found = []
        self.scanned.emit(found)


class PairWorker(QThread):
    """Runs pair -> trust -> connect -> verify, which blocks for up to a minute.

    Recovering a device that paired without a link key costs a second scan on
    top of that, so this cannot be anywhere near the UI thread.
    """

    paired = pyqtSignal(str, str, bool, str)   # mac, name, ok, message

    def __init__(self, mac: str, name: str) -> None:
        super().__init__()
        self._mac = mac
        self._name = name

    def run(self) -> None:
        try:
            ok, message = bluetooth.setup(self._mac)
        except (bluetooth.BluetoothUnavailable, OSError) as error:
            ok, message = False, str(error)
        self.paired.emit(self._mac, self._name, ok, message)


class Tray(QSystemTrayIcon):
    def __init__(self, app: QApplication) -> None:
        super().__init__(QIcon(str(ASSETS / "logo-icon.svg")), app)
        self._app = app
        self._menu = QMenu()
        self.setContextMenu(self._menu)
        self.setToolTip("magic-pomme")

        self._batteries: dict[str, devices.Battery] = {}
        self._battery_nodes: set[str] = set()
        self._worker = BatteryWorker()
        self._worker.updated.connect(self._on_batteries)
        self._worker.start()
        app.aboutToQuit.connect(self._stop_workers)

        self._bt_found: list[bluetooth.BluetoothDevice] = []
        self._scan_worker: ScanWorker | None = None
        self._pair_worker: PairWorker | None = None
        self._pairing = ""
        self._bt_available = False
        self._bt_checked = 0.0

        # Rebuilding the menu on a timer races Plasma's DBusMenu exporter,
        # which logs "Condition failed: menu" and can serve an empty layout.
        # Building it only when it is about to be shown removes the race and
        # the pointless work.
        self._menu.aboutToShow.connect(self.refresh)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_tooltip)
        self._timer.start(REFRESH_MS)

        self.activated.connect(self._on_activated)
        self.refresh()

    def _on_batteries(self, readings: dict) -> None:
        self._batteries = readings
        self._refresh_tooltip()

    def _found_with_batteries(self) -> list[devices.Device]:
        """Devices, with the worker thread's last battery readings attached.

        with_battery=False keeps enumeration off the blocking path. A device
        with no reading yet is almost always one paired since the last poll, so
        ask for an early one rather than leaving it at "battery reading...".
        """
        found = devices.devices(with_battery=False)
        try:
            self._battery_nodes = devices.battery_nodes()
        except OSError:
            self._battery_nodes = set()
        for device in found:
            for hid_id in device.hid_ids:
                if hid_id in self._batteries:
                    device.battery = self._batteries[hid_id]
                    break
        if any(device.battery is None and self._has_battery(device)
               for device in found):
            self._worker.poll_now()
        return found

    def _has_battery(self, device: devices.Device) -> bool:
        """Whether this device has a battery node, so a reading can be expected."""
        return any(hid_id in self._battery_nodes for hid_id in device.hid_ids)

    def _refresh_tooltip(self) -> None:
        self.setToolTip(self._tooltip(self._found_with_batteries()))

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.refresh()
            self._menu.popup(self.geometry().center())

    def refresh(self) -> None:
        self._menu.clear()
        found = self._found_with_batteries()
        status = permissions.status()

        if not found:
            self._menu.addAction("No Apple input devices").setEnabled(False)
        for device in found:
            self._menu.addAction(self._device_label(device)).setEnabled(False)
        self.setToolTip(self._tooltip(found))

        self._menu.addSeparator()
        writable = not status.unwritable
        # Said out loud, because it is the first thing that misleads: these are
        # module parameters, shared by every device the driver binds.
        self._menu.addAction("Driver - affects all devices").setEnabled(False)

        for module, param, label in QUICK_TOGGLES:
            value = drivers.read(module, param)
            action = QAction(label, self._menu, checkable=True)
            action.setChecked(bool(value))
            action.setEnabled(writable and value is not None)
            action.triggered.connect(
                lambda checked, m=module, p=param: self._write(m, p, int(checked))
            )
            self._menu.addAction(action)

        fn_value = drivers.read("hid_apple", "fnmode")
        if fn_value is not None:
            submenu = self._menu.addMenu("Top row")
            group = QActionGroup(submenu)
            group.setExclusive(True)
            for mode, label in FN_LABELS.items():
                action = QAction(label, submenu, checkable=True)
                action.setChecked(fn_value == mode)
                action.setEnabled(writable)
                action.triggered.connect(
                    lambda _checked, m=mode: self._write("hid_apple", "fnmode", m)
                )
                group.addAction(action)
                submenu.addAction(action)

        pointers = plasma_input.pointers()
        if pointers:
            self._menu.addSeparator()
            self._menu.addAction("Per device").setEnabled(False)
        for pointer in pointers:
            self._add_pointer_menu(pointer)

        if not writable:
            self._menu.addSeparator()
            hint = self._setup_hint(status)
            self._menu.addAction(hint).setEnabled(False)

        self._menu.addSeparator()
        self._add_pairing_menu()

        self._menu.addSeparator()
        quit_action = QAction("Quit", self._menu)
        quit_action.triggered.connect(self._app.quit)
        self._menu.addAction(quit_action)

    def _bluetooth_ok(self) -> bool:
        """Whether an adapter is powered, cached.

        refresh() runs on the UI thread every time the menu opens, and
        bluetooth.available() is a subprocess. The TTL is there so an adapter
        switched on after startup still shows up without a restart.
        """
        now = time.monotonic()
        if now - self._bt_checked > BT_CHECK_TTL_S:
            try:
                self._bt_available = bluetooth.available()
            except OSError:
                self._bt_available = False
            self._bt_checked = now
        return self._bt_available

    def _add_pairing_menu(self) -> None:
        """Scan and pair from the panel, instead of a trip to bluetoothctl."""
        if not self._bluetooth_ok():
            self._menu.addAction("No powered Bluetooth adapter").setEnabled(False)
            return

        submenu = self._menu.addMenu("Pair a device")
        if self._pairing:
            submenu.addAction(f"Pairing {self._pairing}...").setEnabled(False)
            return
        if self._scan_worker is not None and self._scan_worker.isRunning():
            submenu.addAction(f"Scanning {SCAN_SECONDS}s...").setEnabled(False)
            return

        action = QAction("Scan for devices", submenu)
        action.triggered.connect(self._start_scan)
        submenu.addAction(action)
        submenu.addSeparator()

        if not self._bt_found:
            submenu.addAction("Unplug it and power-cycle the "
                              "switch first").setEnabled(False)
            return

        for device in self._bt_found:
            if device.ready:
                submenu.addAction(
                    f"{device.name or device.mac}  -  already paired"
                ).setEnabled(False)
                continue
            entry = QAction(self._pair_label(device), submenu)
            entry.triggered.connect(
                lambda _checked, d=device: self._start_pair(d)
            )
            submenu.addAction(entry)

    @staticmethod
    def _pair_label(device: bluetooth.BluetoothDevice) -> str:
        name = device.name or device.mac
        if device.half_paired:
            # Paired with no link key: pairing again on top of this always
            # fails, so say what the entry will actually do.
            return f"{name}  -  pair again (no link key)"
        signal = f"  {device.rssi} dBm" if device.rssi is not None else ""
        return f"{name}{signal}"

    def _start_scan(self) -> None:
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        self._scan_worker = ScanWorker()
        self._scan_worker.scanned.connect(self._on_scanned)
        self._scan_worker.start()
        self.showMessage("magic-pomme",
                         f"Scanning {SCAN_SECONDS}s for Apple devices",
                         QSystemTrayIcon.MessageIcon.Information, 3000)

    def _on_scanned(self, found: list) -> None:
        self._bt_found = found
        if found:
            message = f"{len(found)} found - see Pair a device"
        else:
            message = ("Nothing found. Magic devices do not advertise while on "
                       "USB - unplug it and power-cycle the switch.")
        self.showMessage("magic-pomme", message,
                         QSystemTrayIcon.MessageIcon.Information, 6000)

    def _start_pair(self, device: bluetooth.BluetoothDevice) -> None:
        if self._pairing:
            return
        self._pairing = device.name or device.mac
        self._pair_worker = PairWorker(device.mac, device.name)
        self._pair_worker.paired.connect(self._on_paired)
        self._pair_worker.start()
        self.showMessage("magic-pomme", f"Pairing {self._pairing}...",
                         QSystemTrayIcon.MessageIcon.Information, 4000)

    def _on_paired(self, mac: str, name: str, ok: bool, message: str) -> None:
        self._pairing = ""
        self.showMessage(
            "magic-pomme", f"{name or mac}: {message}",
            QSystemTrayIcon.MessageIcon.Information if ok
            else QSystemTrayIcon.MessageIcon.Warning,
            8000,
        )
        if ok:
            self._bt_found = [d for d in self._bt_found if d.mac != mac]
            self._worker.poll_now()
        self._refresh_tooltip()

    def _stop_workers(self) -> None:
        self._worker.stop()
        for worker in (self._scan_worker, self._pair_worker):
            if worker is not None and worker.isRunning():
                worker.wait(2000)

    @staticmethod
    def _setup_hint(status: permissions.Status) -> str:
        if status.absent:
            return "Run: sudo magic-pomme setup --install"
        if status.needs_relogin or status.missing_groups:
            return f"Toggles need a {session.refresh_action()}"
        return "Parameters are not writable"

    def _device_label(self, device: devices.Device) -> str:
        if device.battery is None:
            charge = "battery reading..." if self._has_battery(device) else "no battery"
        else:
            match device.battery.state:
                case "refused":
                    charge = "battery not reported"
                case "no-report-yet":
                    charge = "battery unknown"
                case _:
                    charge = f"{device.battery.capacity}%"
                    if device.battery.charging:
                        charge += " charging"
        return f"{device.name}  -  {device.transport}, {charge}"

    @staticmethod
    def _tooltip(found: list[devices.Device]) -> str:
        parts = [
            f"{device.name}: {device.battery.capacity}%"
            for device in found
            if device.battery and device.battery.state == "level"
        ]
        return "\n".join(parts) or "magic-pomme"

    def _add_pointer_menu(self, pointer: plasma_input.Pointer) -> None:
        """One submenu per pointer device.

        These settings are KWin's and are keyed to the device, so a trackpad
        can be tuned without touching the mouse -- which the driver parameters
        above cannot do.
        """
        settings = plasma_input.settings(pointer)
        submenu = self._menu.addMenu(pointer.name)

        natural = QAction("Natural scrolling", submenu, checkable=True)
        natural.setChecked(bool(settings.natural_scroll))
        natural.triggered.connect(
            lambda checked, pt=pointer: self._apply(
                lambda: plasma_input.set_natural_scroll(pt, checked))
        )
        submenu.addAction(natural)

        self._add_steps(
            submenu, "Scroll speed", SCROLL_STEPS, settings.scroll_factor, 1.0,
            lambda value, pt=pointer: self._apply(
                lambda: plasma_input.set_scroll_factor(pt, value)),
        )
        self._add_steps(
            submenu, "Pointer speed", SPEED_STEPS,
            settings.pointer_acceleration, 0.0,
            lambda value, pt=pointer: self._apply(
                lambda: plasma_input.set_pointer_acceleration(pt, value)),
        )

        if not settings.supports_flat:
            return
        accel = submenu.addMenu("Acceleration")
        group = QActionGroup(accel)
        group.setExclusive(True)
        for label, flat in (("Adaptive - ramps with speed", False),
                            ("Flat - no ramp", True)):
            entry = QAction(label, accel, checkable=True)
            entry.setChecked(settings.flat_profile is flat)
            entry.triggered.connect(
                lambda _checked, pt=pointer, f=flat: self._set_profile(pt, f)
            )
            group.addAction(entry)
            accel.addAction(entry)

    @staticmethod
    def _add_steps(parent: QMenu, title: str, steps: tuple[float, ...],
                   current: float | None, default: float, apply) -> None:
        """A preset submenu, with the live value shown when it is off-grid.

        Showing "current" rather than silently checking the nearest step keeps
        a hand-tuned value from being misreported as one of these presets.
        """
        submenu = parent.addMenu(title)
        matches = [step for step in steps
                   if current is not None and abs(current - step) < 0.01]
        if current is not None and not matches:
            submenu.addAction(f"current: {current:g}").setEnabled(False)
            submenu.addSeparator()

        group = QActionGroup(submenu)
        group.setExclusive(True)
        for step in steps:
            label = f"{step:g}"
            if abs(step - default) < 0.01:
                label += "  (default)"
            entry = QAction(label, submenu, checkable=True)
            entry.setChecked(step in matches)
            entry.triggered.connect(lambda _checked, value=step: apply(value))
            group.addAction(entry)
            submenu.addAction(entry)

    def _set_profile(self, pointer: plasma_input.Pointer, flat: bool) -> None:
        def run() -> None:
            if not plasma_input.set_flat_profile(pointer, flat):
                raise OSError("KWin would not change the acceleration profile")
        self._apply(run)

    def _apply(self, action) -> None:
        if message := guarded(action):
            self.showMessage("magic-pomme", message,
                             QSystemTrayIcon.MessageIcon.Warning, 6000)
        self.refresh()

    def _write(self, module: str, param: str, value: int) -> None:
        self._apply(lambda: drivers.write(module, param, value))


TRAY_WAIT_SECONDS = 90.0


def wait_for_tray(app: QApplication, timeout: float = TRAY_WAIT_SECONDS) -> bool:
    """Wait for a system tray to exist, rather than exiting if it does not yet.

    Started as a user service, this can win the race against the panel: on a
    lingering systemd user instance, default.target is reached at boot while
    graphical-session.target follows nearly a minute later. Ordering the unit
    after graphical-session.target handles the usual case, but the panel still
    needs a moment to claim the tray, so give it one instead of dying and
    leaving systemd to restart-loop.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if QSystemTrayIcon.isSystemTrayAvailable():
            return True
        app.processEvents()
        time.sleep(1.0)
    return QSystemTrayIcon.isSystemTrayAvailable()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("magic-pomme")
    app.setQuitOnLastWindowClosed(False)   # a tray app has no windows

    if not wait_for_tray(app):
        print(f"no system tray appeared within {TRAY_WAIT_SECONDS:.0f}s",
              file=sys.stderr)
        return 1

    tray = Tray(app)
    tray.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
