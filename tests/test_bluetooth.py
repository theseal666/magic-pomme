"""Tests for the Bluetooth layer.

The scan regression test exists because the original implementation passed its
commands through subprocess.run(input=...), which closes stdin immediately.
bluetoothctl exits on EOF, so it died in milliseconds and never scanned -- a
silent no-op indistinguishable from "no devices nearby". Forty-four tests
passed while it was broken, because none of them checked that the scan window
actually elapsed before the process was reaped.
"""

import pytest

from magic_pomme import bluetooth


class _FakeStdin:
    def __init__(self, log):
        self._log = log

    def write(self, text):
        self._log.append(("write", text))

    def flush(self):
        pass


class _FakeProc:
    def __init__(self, log):
        self.stdin = _FakeStdin(log)
        self._log = log

    def wait(self, timeout=None):
        self._log.append(("wait", timeout))
        return 0

    def kill(self):
        self._log.append(("kill", None))


@pytest.fixture
def traced(monkeypatch):
    log = []
    monkeypatch.setattr(bluetooth, "_binary", lambda: "/bin/true")
    monkeypatch.setattr(bluetooth.subprocess, "Popen", lambda *a, **k: _FakeProc(log))
    monkeypatch.setattr(bluetooth.time, "sleep", lambda s: log.append(("sleep", s)))
    monkeypatch.setattr(bluetooth, "devices", lambda **k: [])
    return log


class TestScanWindow:
    def test_sleeps_for_the_full_requested_duration(self, traced):
        bluetooth.scan(17)
        assert ("sleep", 17) in traced

    def test_scan_window_elapses_before_the_process_is_reaped(self, traced):
        bluetooth.scan(5)
        kinds = [entry[0] for entry in traced]
        assert kinds.index("sleep") < kinds.index("wait"), (
            "bluetoothctl was reaped before the scan window elapsed"
        )

    def test_scan_is_started_before_sleeping(self, traced):
        bluetooth.scan(3)
        kinds = [entry[0] for entry in traced]
        assert kinds.index("write") < kinds.index("sleep")
        assert any("scan on" in e[1] for e in traced if e[0] == "write")

    def test_registers_an_agent_so_pairing_can_proceed(self, traced):
        bluetooth.scan(1)
        written = "".join(e[1] for e in traced if e[0] == "write")
        assert "default-agent" in written

    def test_survives_a_broken_pipe_when_stopping(self, monkeypatch, traced):
        def explode(text):
            if "scan off" in text:
                raise BrokenPipeError
            traced.append(("write", text))
        monkeypatch.setattr(
            bluetooth.subprocess, "Popen",
            lambda *a, **k: type("P", (), {
                "stdin": type("S", (), {"write": staticmethod(explode),
                                        "flush": staticmethod(lambda: None)})(),
                "wait": staticmethod(lambda timeout=None: 0),
                "kill": staticmethod(lambda: None),
            })(),
        )
        bluetooth.scan(1)  # must not raise


class TestDeviceParsing:
    def test_apple_vendor_detected_from_modalias(self):
        device = bluetooth.BluetoothDevice(mac="x", vendor=0x004C)
        assert device.is_apple

    def test_non_apple_vendor(self):
        assert not bluetooth.BluetoothDevice(mac="x", vendor=0x046D).is_apple

    def test_unknown_vendor_is_not_claimed_as_apple(self):
        assert not bluetooth.BluetoothDevice(mac="x").is_apple

    def test_ready_requires_all_three_states(self):
        full = dict(mac="x", paired=True, trusted=True, connected=True)
        assert bluetooth.BluetoothDevice(**full).ready
        assert not bluetooth.BluetoothDevice(**{**full, "trusted": False}).ready

    def test_input_icon_recognised(self):
        assert bluetooth.BluetoothDevice(mac="x", icon="input-mouse").is_input
        assert not bluetooth.BluetoothDevice(mac="x", icon="phone").is_input
