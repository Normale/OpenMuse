"""The stream watchdog: the requested sensors must actually arrive.

Reproduces 2026-08-05, where the headset streamed ACC/OPTICS at full rate with
no EEG for eight hours and every layer above reported a healthy recording,
because bytes were flowing and nothing checked which bytes.
"""
import pytest

from OpenMuse.decode import SENSORS, sensor_types_in


def _packet(pkt_id: int, data_len: int = 28) -> bytes:
    """Minimal well-formed packet: length byte, header, pkt_id at offset 9."""
    pkt_len = 14 + data_len
    b = bytearray(pkt_len)
    b[0] = pkt_len
    b[9] = pkt_id
    return bytes(b)


def test_detects_each_sensor_type():
    for tag, config in SENSORS.items():
        assert config["type"] in sensor_types_in(_packet(tag))


def test_eeg_present_and_absent():
    eeg = _packet(0x11) + _packet(0x47) + _packet(0x34)      # EEG4 + ACC + OPTICS
    no_eeg = _packet(0x47) + _packet(0x34)                   # the 08-05 failure
    assert "EEG" in sensor_types_in(eeg)
    assert "EEG" not in sensor_types_in(no_eeg)
    # and the streams that *were* flowing are still detected, which is exactly
    # why "bytes are arriving" was not a sufficient health check
    assert {"ACCGYRO", "OPTICS"} <= sensor_types_in(no_eeg)


@pytest.mark.parametrize("payload", [
    b"", b"\x00", b"\x05" + b"\x00" * 20,        # pkt_len below the header size
    b"\xff" + b"\x00" * 10,                       # declares more than it has
    bytes(range(256)),                            # arbitrary garbage
])
def test_never_raises_on_malformed_input(payload):
    """It runs inside the BLE callback; an exception there loses the night."""
    assert isinstance(sensor_types_in(payload), set)


def test_stops_at_a_zero_length_packet():
    """pkt_len 0 would never advance the offset - must not hang."""
    assert sensor_types_in(_packet(0x11) + b"\x00" * 40) == {"EEG"}


def test_required_sensors_exclude_battery():
    """BATTERY answers on the control channel every battery_interval (300 s),
    so requiring it inside a 60 s grace window would fail every session."""
    from OpenMuse.muse import describe_preset
    layout = describe_preset("p1035")
    required = {n for n, c in layout.items() if c and n != "BATTERY"}
    assert required == {"EEG", "OPTICS", "ACCGYRO"}
