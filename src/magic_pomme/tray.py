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
import time
from pathlib import Path

from PyQt6.QtCore import QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import devices, drivers, permissions, plasma_input, session

REFRESH_MS = 30_000
BATTERY_INTERVAL_S = 180.0
ASSETS = Path(__file__).parent / "assets"

# Toggles worth reaching for from the panel. Everything else stays in the CLI:
# a tray menu that mirrors every parameter is a worse tray menu.
QUICK_TOGGLES = (
    ("hid_apple", "swap_ctrl_cmd", "Mac modifiers (Cmd acts as Ctrl)"),
    ("hid_magicmouse", "scroll_acceleration", "Accelerated scrolling"),
    ("hid_magicmouse", "emulate_3button", "Middle click"),
)
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

    def run(self) -> None:
        while not self._stop:
            try:
                self.updated.emit(devices.batteries())
            except OSError:
                pass
            for _ in range(int(BATTERY_INTERVAL_S)):
                if self._stop:
                    return
                time.sleep(1.0)

    def stop(self) -> None:
        self._stop = True


class Tray(QSystemTrayIcon):
    def __init__(self, app: QApplication) -> None:
        super().__init__(QIcon(str(ASSETS / "logo-icon.svg")), app)
        self._app = app
        self._menu = QMenu()
        self.setContextMenu(self._menu)
        self.setToolTip("magic-pomme")

        self._batteries: dict[str, devices.Battery] = {}
        self._worker = BatteryWorker()
        self._worker.updated.connect(self._on_batteries)
        self._worker.start()
        app.aboutToQuit.connect(self._worker.stop)

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

    def _refresh_tooltip(self) -> None:
        found = devices.devices(with_battery=False)
        for device in found:
            for hid_id in device.hid_ids:
                if hid_id in self._batteries:
                    device.battery = self._batteries[hid_id]
                    break
        self.setToolTip(self._tooltip(found))

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.refresh()
            self._menu.popup(self.geometry().center())

    def refresh(self) -> None:
        self._menu.clear()
        # with_battery=False keeps this off the blocking path; levels come
        # from the worker thread via _on_batteries.
        found = devices.devices(with_battery=False)
        for device in found:
            for hid_id in device.hid_ids:
                if hid_id in self._batteries:
                    device.battery = self._batteries[hid_id]
                    break
        status = permissions.status()

        if not found:
            self._menu.addAction("No Apple input devices").setEnabled(False)
        for device in found:
            self._menu.addAction(self._device_label(device)).setEnabled(False)
        self.setToolTip(self._tooltip(found))

        self._menu.addSeparator()
        writable = not status.unwritable

        for module, param, label in QUICK_TOGGLES:
            value = drivers.read(module, param)
            action = QAction(label, self._menu, checkable=True)
            action.setChecked(bool(value))
            action.setEnabled(writable and value is not None)
            action.triggered.connect(
                lambda checked, m=module, p=param: self._write(m, p, int(checked))
            )
            self._menu.addAction(action)

        for pointer in plasma_input.pointers():
            current = plasma_input.natural_scroll(pointer)
            action = QAction(f"Natural scrolling - {pointer.name}",
                             self._menu, checkable=True)
            action.setChecked(bool(current))
            action.triggered.connect(
                lambda checked, pt=pointer: self._set_scroll(pt, checked)
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

        if not writable:
            self._menu.addSeparator()
            hint = self._setup_hint(status)
            self._menu.addAction(hint).setEnabled(False)

        self._menu.addSeparator()
        quit_action = QAction("Quit", self._menu)
        quit_action.triggered.connect(self._app.quit)
        self._menu.addAction(quit_action)

    @staticmethod
    def _setup_hint(status: permissions.Status) -> str:
        if status.absent:
            return "Run: sudo magic-pomme setup --install"
        if status.needs_relogin or status.missing_groups:
            return f"Toggles need a {session.refresh_action()}"
        return "Parameters are not writable"

    @staticmethod
    def _device_label(device: devices.Device) -> str:
        if device.battery is None:
            charge = "battery reading..."
        elif device.battery.unavailable:
            charge = "battery not reported"
        elif device.battery.unknown:
            charge = "battery unknown"
        elif device.battery:
            charge = f"{device.battery.capacity}%"
            if device.battery.charging:
                charge += " charging"
        else:
            charge = "no battery"
        return f"{device.name}  -  {device.transport}, {charge}"

    @staticmethod
    def _tooltip(found: list[devices.Device]) -> str:
        parts = [
            f"{device.name}: {device.battery.capacity}%"
            for device in found
            if device.battery and not device.battery.unknown
               and not device.battery.unavailable
        ]
        return "\n".join(parts) or "magic-pomme"

    def _set_scroll(self, pointer: plasma_input.Pointer, enabled: bool) -> None:
        try:
            plasma_input.set_natural_scroll(pointer, enabled)
        except (plasma_input.PlasmaToolsMissing, OSError) as error:
            self.showMessage("magic-pomme", str(error),
                             QSystemTrayIcon.MessageIcon.Warning, 5000)
        self.refresh()

    def _write(self, module: str, param: str, value: int) -> None:
        try:
            drivers.write(module, param, value)
        except (PermissionError, ValueError, FileNotFoundError) as error:
            self.showMessage("magic-pomme", str(error),
                             QSystemTrayIcon.MessageIcon.Warning, 5000)
        self.refresh()


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
