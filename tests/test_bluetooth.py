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

    def test_ready_requires_bonding_as_well(self):
        """Paired, trusted and connected is not enough, and this is not pedantry.

        A real Magic Keyboard reached exactly this state: BlueZ said paired,
        trusted and connected, while the journal said "Rejected connection from
        !bonded device" and no input device was ever created. Reporting that
        device as ready is how an afternoon disappears.
        """
        full = dict(mac="x", paired=True, bonded=True, trusted=True, connected=True)
        assert bluetooth.BluetoothDevice(**full).ready
        assert not bluetooth.BluetoothDevice(**{**full, "bonded": False}).ready
        assert not bluetooth.BluetoothDevice(**{**full, "trusted": False}).ready

    def test_half_paired_names_the_broken_state(self):
        device = bluetooth.BluetoothDevice(mac="x", paired=True, connected=True)
        assert device.half_paired
        assert not bluetooth.BluetoothDevice(mac="x", paired=True,
                                             bonded=True).half_paired

    def test_str_shows_a_missing_link_key(self):
        device = bluetooth.BluetoothDevice(mac="x", name="kb", paired=True,
                                           trusted=True, connected=True)
        assert "NOT BONDED" in str(device)

    def test_input_icon_recognised(self):
        assert bluetooth.BluetoothDevice(mac="x", icon="input-mouse").is_input
        assert not bluetooth.BluetoothDevice(mac="x", icon="phone").is_input


_INFO = """Device 08:95:42:ED:BF:B4 (public)
\tName: Magic Keyboard
\tPaired: {paired}
\tBonded: {bonded}
\tTrusted: yes
\tConnected: yes
\tModalias: bluetooth:v004Cp029Fd0206
"""

_INFO_OLD_BLUEZ = """Device 08:95:42:ED:BF:B4 (public)
\tName: Magic Keyboard
\tPaired: yes
\tTrusted: yes
\tConnected: yes
"""


class TestBondedParsing:
    def test_bonded_flag_is_read(self, monkeypatch):
        monkeypatch.setattr(bluetooth, "_run",
                            lambda *a, **k: _INFO.format(paired="yes", bonded="no"))
        device = bluetooth.info("08:95:42:ED:BF:B4")
        assert device.paired and not device.bonded and device.half_paired

    def test_a_bonded_device_is_ready(self, monkeypatch):
        monkeypatch.setattr(bluetooth, "_run",
                            lambda *a, **k: _INFO.format(paired="yes", bonded="yes"))
        assert bluetooth.info("08:95:42:ED:BF:B4").ready

    def test_missing_field_falls_back_to_paired(self, monkeypatch):
        """BlueZ before 5.65 has no Bonded flag; do not fail working pairings."""
        monkeypatch.setattr(bluetooth, "_run", lambda *a, **k: _INFO_OLD_BLUEZ)
        device = bluetooth.info("08:95:42:ED:BF:B4")
        assert device.bonded and device.ready and not device.half_paired


class TestPairInsistsOnALinkKey:
    def test_successful_message_is_not_enough(self, monkeypatch):
        """bluetoothctl says "Pairing successful" for an unbonded pairing too."""
        monkeypatch.setattr(bluetooth, "_run", lambda *a, **k: "Pairing successful")
        monkeypatch.setattr(bluetooth, "info", lambda mac: bluetooth.BluetoothDevice(
            mac=mac, paired=True, bonded=False))
        assert not bluetooth.pair("08:95:42:ED:BF:B4")

    def test_bonded_pairing_is_accepted(self, monkeypatch):
        monkeypatch.setattr(bluetooth, "_run", lambda *a, **k: "")
        monkeypatch.setattr(bluetooth, "info", lambda mac: bluetooth.BluetoothDevice(
            mac=mac, paired=True, bonded=True))
        assert bluetooth.pair("08:95:42:ED:BF:B4")


class TestTrustIsVerified:
    def test_message_is_not_taken_at_face_value(self, monkeypatch):
        """A trust issued while pairing is in flight prints success and does not stick."""
        monkeypatch.setattr(bluetooth, "_run", lambda *a, **k: "trust succeeded")
        monkeypatch.setattr(bluetooth, "info", lambda mac: bluetooth.BluetoothDevice(
            mac=mac, paired=True, bonded=True, trusted=False))
        assert not bluetooth.trust("08:95:42:ED:BF:B4")


class TestAttached:
    def test_matches_the_hid_serial_case_insensitively(self, monkeypatch):
        seen = {}

        def fake(apple_only=True, with_battery=True):
            seen.update(apple_only=apple_only, with_battery=with_battery)
            return [type("D", (), {"serial": "08:95:42:ED:BF:B4"})()]

        monkeypatch.setattr(bluetooth.hid, "devices", fake)
        assert bluetooth.attached("08:95:42:ed:bf:b4")
        assert seen["with_battery"] is False, "attached() must not block on a battery read"

    def test_absent_device_is_not_attached(self, monkeypatch):
        monkeypatch.setattr(bluetooth.hid, "devices", lambda **k: [])
        assert not bluetooth.attached("08:95:42:ed:bf:b4")


@pytest.fixture
def sequence(monkeypatch):
    """Stub the whole bluetoothctl layer and record the call order."""
    calls = []
    state = {"bonded": False}

    def fake_pair(mac, timeout=25):
        calls.append("pair")
        return state["bonded"]

    def fake_scan(seconds=20, apple_only=False):
        calls.append("scan")
        state["bonded"] = state.pop("bond_on_retry", False)
        return []

    monkeypatch.setattr(bluetooth, "pair", fake_pair)
    monkeypatch.setattr(bluetooth, "scan", fake_scan)
    monkeypatch.setattr(bluetooth, "info", lambda mac: bluetooth.BluetoothDevice(
        mac=mac, paired=True, bonded=state["bonded"], trusted=True, connected=True))
    for name in ("disconnect", "remove", "trust"):
        monkeypatch.setattr(bluetooth, name,
                            lambda mac, _n=name: (calls.append(_n), True)[1])
    monkeypatch.setattr(bluetooth, "connect",
                        lambda mac, timeout=20: (calls.append("connect"), True)[1])
    # setup() goes through connect_with_reason, not connect. Leaving it unstubbed
    # made this suite open a real Bluetooth connection to whatever hardware was
    # paired on the machine running the tests -- 40 seconds of it.
    monkeypatch.setattr(bluetooth, "connect_with_reason",
                        lambda mac, timeout=20: (calls.append("connect"), (True, ""))[1])
    monkeypatch.setattr(bluetooth, "wait_attached",
                        lambda mac, seconds=8.0: (calls.append("attach"), True)[1])
    return calls, state


class TestSetupRecoversTheHalfPairedState:
    def test_forgets_rediscovers_and_pairs_again(self, sequence):
        calls, state = sequence
        state["bond_on_retry"] = True
        ok, message = bluetooth.setup("08:95:42:ED:BF:B4")
        assert ok, message
        assert calls[:5] == ["pair", "disconnect", "remove", "scan", "pair"], calls
        assert calls.index("remove") < calls.index("scan") < calls.index("pair", 1), (
            "removing drops the device object, so it has to be rediscovered "
            "before the retry can pair at all"
        )

    def test_reports_failure_when_the_retry_does_not_bond_either(self, sequence):
        calls, _ = sequence
        ok, message = bluetooth.setup("08:95:42:ED:BF:B4")
        assert not ok
        assert "link key" in message and "power-cycle" in message

    def test_a_clean_pairing_does_not_touch_the_existing_bond(self, sequence):
        calls, state = sequence
        state["bonded"] = True
        ok, _ = bluetooth.setup("08:95:42:ED:BF:B4")
        assert ok
        assert "remove" not in calls, "a working pairing must never be forgotten"


class TestOutcomesAreReadBackNotMatched:
    """bluetoothctl's wording is not a contract, and betting on it has cost twice.

    disconnect() matched "Successful disconnected". bluetoothctl says
    "Disconnection successful", so every successful disconnect was reported as
    a failure -- including inside repair(), where it masked a device that had
    in fact been dropped.
    """

    def test_disconnect_believes_the_device_over_the_wording(self, monkeypatch):
        monkeypatch.setattr(bluetooth, "_run",
                            lambda *a, **k: "Disconnection successful")
        monkeypatch.setattr(bluetooth, "info", lambda mac: bluetooth.BluetoothDevice(
            mac=mac, paired=True, bonded=True, connected=False))
        assert bluetooth.disconnect("08:95:42:ED:BF:B4")

    def test_disconnect_fails_when_the_device_is_still_connected(self, monkeypatch):
        monkeypatch.setattr(bluetooth, "_run", lambda *a, **k: "whatever")
        monkeypatch.setattr(bluetooth, "info", lambda mac: bluetooth.BluetoothDevice(
            mac=mac, paired=True, bonded=True, connected=True))
        assert not bluetooth.disconnect("08:95:42:ED:BF:B4")

    def test_remove_is_confirmed_by_the_device_being_gone(self, monkeypatch):
        monkeypatch.setattr(bluetooth, "_run", lambda *a, **k: "")
        monkeypatch.setattr(bluetooth, "info", lambda mac: None)
        assert bluetooth.remove("08:95:42:ED:BF:B4")

    def test_remove_fails_while_the_device_is_still_known(self, monkeypatch):
        monkeypatch.setattr(bluetooth, "_run", lambda *a, **k: "Device has been removed")
        monkeypatch.setattr(bluetooth, "info", lambda mac: bluetooth.BluetoothDevice(
            mac=mac, paired=True))
        assert not bluetooth.remove("08:95:42:ED:BF:B4")


class TestConnectKeepsTheReason:
    def test_a_sleeping_device_is_named_as_such(self, monkeypatch):
        monkeypatch.setattr(
            bluetooth, "_run",
            lambda *a, **k: ("Attempting to connect to 58:73:D8:E7:AE:72\n"
                             "Failed to connect: org.bluez.Error.Failed "
                             "br-connection-create-socket\n"))
        monkeypatch.setattr(bluetooth, "info", lambda mac: bluetooth.BluetoothDevice(
            mac=mac, paired=True, bonded=True, connected=False))
        ok, reason = bluetooth.connect_with_reason("58:73:D8:E7:AE:72")
        assert not ok
        assert "br-connection-create-socket" in reason

    def test_setup_turns_that_into_an_instruction(self, monkeypatch):
        monkeypatch.setattr(bluetooth, "pair", lambda mac, timeout=25: True)
        monkeypatch.setattr(bluetooth, "trust", lambda mac: True)
        monkeypatch.setattr(bluetooth, "connect_with_reason",
                            lambda mac, timeout=20: (False, "br-connection-create-socket"))
        ok, message = bluetooth.setup("58:73:D8:E7:AE:72")
        assert not ok
        assert "power-cycle" in message
