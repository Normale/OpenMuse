"""Utilities for interacting with the Muse S Athena headset."""

import asyncio
import json
from typing import Any, Callable, ClassVar, Dict, Optional, Sequence

from .backends import BleakBackend, _run
from .decode import ControlChannel


# ===================================
# Find MAC addresses of Muse devices
# ===================================
def find_muse(timeout=10, verbose=True):
    """Scan for Muse devices via Bluetooth Low Energy (BLE).

    This is the canonical scanned used throughout the package and CLI.
    """
    backend = BleakBackend()
    if verbose:
        print(f"Searching for Muses (max. {timeout} seconds)...")
    devices = backend.scan(timeout=timeout)
    muses = []
    for d in devices:
        name = d.get("name")
        try:
            if isinstance(name, str) and "muse" in name.lower():
                muses.append(d)
        except Exception:
            continue

    if verbose:
        if muses:
            for m in muses:
                print(f'Found device {m["name"]}, MAC Address {m["address"]}')
        else:
            print("No Muses found. Ensure the device is on and Bluetooth is enabled.")

    return muses


# ===================================
# Presets
# ===================================
# The preset string is the only thing that controls *which sensors the headset
# actually runs*, so it is the only lever that saves power at the source --
# `stream(--sensors ...)` filters after reception and saves nothing.  It was
# also entirely undocumented.
#
# The table below is measured, not guessed: every entry was produced by
# decoding this repo's own committed captures in tests/test_data/data_p*.txt
# (~60 s each) and reading off which TAGs appear and how many channels each
# carries.  tests/test_presets.py re-derives it from those files, so it cannot
# silently drift.
#
#   preset   EEG   OPTICS   ACCGYRO   BATTERY
#   p20       4      --        6         1
#   p21       4      --        6         1
#   p50       4      --        6         1
#   p51       4      --        6         1
#   p60       4      --        6         1
#   p61       4      --        6         1
#   p1034     4       8        6         1
#   p1035     4       4        6         1
#   p1041     8      16        6         1
#   p1042     8      16        6         1
#   p1043     8       8        6         1
#   p1044     8       8        6         1
#   p1045     8       4        6         1
#   p1046     8       4        6         1
#   p4129     8       4        6         1
#
# Battery ships with every preset. Presets that share a composition (p20/p21,
# p50/p51, p60/p61, p1041/p1042, ...) differ in something not observable in
# these captures -- the EEG in all of them is rail-to-rail, so no filtering or
# gain difference can be read out of them either. That distinction remains
# undocumented; only the composition below is established.
#
# Choosing for battery life on long unattended recordings:
#   * OPTICS is ~30% of the payload AND drives sixteen NIR/IR/RED LEDs, which
#     is typically a dominant draw in a wearable. If you do not need PPG,
#     p21 (EEG4 + motion) is by far the cheapest useful preset.
#   * p1041 (the default) is the most expensive: every sensor, widest layouts.
#   * EEG channels 4-7 in the 8-channel layouts are AUX, not scalp electrodes;
#     the 4-channel presets already give you TP9/AF7/AF8/TP10.
PRESETS: Dict[str, Dict[str, Optional[int]]] = {
    "p20": {"EEG": 4, "OPTICS": None, "ACCGYRO": 6, "BATTERY": 1},
    "p21": {"EEG": 4, "OPTICS": None, "ACCGYRO": 6, "BATTERY": 1},
    "p50": {"EEG": 4, "OPTICS": None, "ACCGYRO": 6, "BATTERY": 1},
    "p51": {"EEG": 4, "OPTICS": None, "ACCGYRO": 6, "BATTERY": 1},
    "p60": {"EEG": 4, "OPTICS": None, "ACCGYRO": 6, "BATTERY": 1},
    "p61": {"EEG": 4, "OPTICS": None, "ACCGYRO": 6, "BATTERY": 1},
    "p1034": {"EEG": 4, "OPTICS": 8, "ACCGYRO": 6, "BATTERY": 1},
    "p1035": {"EEG": 4, "OPTICS": 4, "ACCGYRO": 6, "BATTERY": 1},
    "p1041": {"EEG": 8, "OPTICS": 16, "ACCGYRO": 6, "BATTERY": 1},
    "p1042": {"EEG": 8, "OPTICS": 16, "ACCGYRO": 6, "BATTERY": 1},
    "p1043": {"EEG": 8, "OPTICS": 8, "ACCGYRO": 6, "BATTERY": 1},
    "p1044": {"EEG": 8, "OPTICS": 8, "ACCGYRO": 6, "BATTERY": 1},
    "p1045": {"EEG": 8, "OPTICS": 4, "ACCGYRO": 6, "BATTERY": 1},
    "p1046": {"EEG": 8, "OPTICS": 4, "ACCGYRO": 6, "BATTERY": 1},
    "p4129": {"EEG": 8, "OPTICS": 4, "ACCGYRO": 6, "BATTERY": 1},
}

# Tie-break between presets with identical measured composition (p20/p21,
# p50/p51, p60/p61, p1041/p1042, ...). They differ in something these captures
# cannot show, so the choice is arbitrary; these are the ones the package has
# always named, which makes them the best-tested.
_PRESET_PREFERENCE = ("p21", "p1041", "p1035")


def describe_preset(preset: str) -> Optional[Dict[str, Optional[int]]]:
    """What a preset enables: ``{"EEG": 8, "OPTICS": 16, ...}``, or None."""
    return PRESETS.get(preset)


def preset_for_sensors(
    sensors: Sequence[str],
    eeg_channels: Optional[int] = None,
    optics_channels: Optional[int] = None,
) -> str:
    """Resolve a sensor list to a preset string.

    ``preset_for_sensors(["EEG", "ACCGYRO"])`` -> ``"p21"`` (no optics: the
    LEDs stay off and the payload drops by ~30%).
    ``preset_for_sensors(["EEG", "ACCGYRO", "OPTICS"])`` -> ``"p1041"``.

    ``eeg_channels`` (4 or 8) and ``optics_channels`` (4, 8 or 16) narrow the
    choice; without them the widest available layout is used, except that
    omitting OPTICS is always honoured.  Raises ``ValueError`` if no known
    preset matches.
    """
    wanted = {s.upper() for s in sensors}
    unknown = wanted - {"EEG", "OPTICS", "ACCGYRO", "BATTERY"}
    if unknown:
        raise ValueError(f"unknown sensor(s): {sorted(unknown)}")
    if not wanted:
        raise ValueError("at least one sensor must be requested")

    candidates = []
    for name, layout in PRESETS.items():
        available = {k for k, v in layout.items() if v}
        if not wanted.issubset(available):
            continue
        # Never hand back optics when the caller did not ask for them: that is
        # the whole point of choosing a preset.
        if "OPTICS" not in wanted and layout["OPTICS"]:
            continue
        if eeg_channels is not None and layout["EEG"] != eeg_channels:
            continue
        if optics_channels is not None and layout["OPTICS"] != optics_channels:
            continue
        candidates.append(name)

    if not candidates:
        detail = ""
        if "OPTICS" not in wanted and eeg_channels == 8:
            detail = (
                " -- no preset combines 8-channel EEG with optics disabled; "
                "the 4-channel presets (p20/p21/p50/p51/p60/p61) are the only "
                "optics-free ones, and EEG channels 4-7 are AUX anyway"
            )
        raise ValueError(
            f"no known preset provides {sorted(wanted)} "
            f"(eeg_channels={eeg_channels}, optics_channels={optics_channels})"
            f"{detail}; see OpenMuse.PRESETS"
        )

    # Widest layout wins, then the best-tested name among equals.
    def rank(name: str):
        layout = PRESETS[name]
        return (layout["EEG"] or 0, layout["OPTICS"] or 0)

    best = max(rank(name) for name in candidates)
    equals = [name for name in candidates if rank(name) == best]
    for preferred in _PRESET_PREFERENCE:
        if preferred in equals:
            return preferred
    return sorted(equals)[0]


# ===================================
# Muse S device
# ===================================
class MuseS:
    """Constants and helpers shared across Muse S interactions."""

    CONTROL_UUID: ClassVar[str] = "273e0001-4c4d-454d-96be-f03bac821358"
    EEG_UUID: ClassVar[str] = "273e0013-4c4d-454d-96be-f03bac821358"
    OTHER_UUID: ClassVar[str] = "273e0014-4c4d-454d-96be-f03bac821358"

    DATA_CHARACTERISTICS: ClassVar[tuple[str, str]] = (
        EEG_UUID,
        OTHER_UUID,
    )

    @staticmethod
    def encode_command(token: str) -> bytes:
        """Encode a Muse command token as length-prefixed ASCII."""
        if not isinstance(token, str) or not token:
            raise ValueError("command token must be a non-empty string")
        try:
            payload = (token + "\n").encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("command token must be ASCII") from exc
        if len(payload) > 255:
            raise ValueError("command too long (max 254 chars plus newline)")
        return bytes([len(payload)]) + payload

    @staticmethod
    async def send_command(client, token: str, verbose: bool = False) -> None:
        """Send a command to the Muse device via the control characteristic."""
        data = MuseS.encode_command(token)
        await client.write_gatt_char(MuseS.CONTROL_UUID, data, response=False)

    @staticmethod
    async def initialize_device(
        client,
        preset: str,
        verbose: bool = False,
        low_latency: bool = False,
    ) -> None:
        """
        Initialize Muse device with standard handshake and preset configuration.

        Performs:
        1. Version query (v6)
        2. Status query (s)
        3. Halt/reset (h)
        4. Apply preset
        5. Start streaming (dc001)
        6. Optionally enable low-latency mode (L1)

        Parameters
        ----------
        low_latency : bool
            Send ``L1``, which asks the headset for a short BLE connection
            interval.  The radio then wakes far more often than the data rate
            requires, which is a large share of the device's power budget.

            Real-time use (``stream``) wants this.  **Recording does not** --
            nothing cares whether a sample arrives 15 ms or 200 ms after
            acquisition, only that it arrives -- so it defaults to ``False``
            and ``record()`` leaves it off.  The full payload of preset
            ``p1041`` (EEG 8ch @256 Hz + OPTICS 16ch @64 Hz + ACCGYRO 6ch
            @52 Hz) is only ~6.7 KB/s, which fits comfortably in a 50 ms
            connection interval (~335 B/event).
        """
        # Version/status handshake (best-effort)
        try:
            await MuseS.send_command(client, "v6", verbose)
            await asyncio.sleep(0.2)
            await MuseS.send_command(client, "s", verbose)
            await asyncio.sleep(0.2)
        except Exception as exc:
            if verbose:
                print(f"Warning: version/status failed: {exc}")

        # Halt/reset
        try:
            await MuseS.send_command(client, "h", verbose)
            await asyncio.sleep(0.2)
        except Exception as exc:
            if verbose:
                print(f"Warning: halt failed: {exc}")

        # Preset selection
        if verbose:
            print(f"Sending preset {preset!r} and start commands ...")
        try:
            await MuseS.send_command(client, preset, verbose)
        except Exception as exc:
            if verbose:
                print(f"Warning: preset {preset!r} failed: {exc}")
        await asyncio.sleep(0.2)

        # Query status again after preset change
        try:
            await MuseS.send_command(client, "s", verbose)
            await asyncio.sleep(0.2)
        except Exception:
            pass

        # Start streaming (SEND TWICE for reliability), then optionally L1
        await MuseS.send_command(client, "dc001", verbose)
        await asyncio.sleep(0.05)
        await MuseS.send_command(client, "dc001", verbose)
        await asyncio.sleep(0.1)
        if low_latency:
            try:
                await MuseS.send_command(client, "L1", verbose)
                await asyncio.sleep(0.3)
            except Exception:
                pass
        elif verbose:
            print("Low-latency mode (L1) not requested; keeping the radio duty cycle low.")

        # Final status query
        try:
            await MuseS.send_command(client, "s", verbose)
            await asyncio.sleep(0.2)
        except Exception:
            pass

    @staticmethod
    async def connect_and_initialize(
        client,
        preset: str,
        data_callbacks: dict[str, Callable],
        verbose: bool = False,
        low_latency: bool = False,
        control: Optional[ControlChannel] = None,
        on_control_raw: Optional[Callable[[bytes], None]] = None,
    ) -> ControlChannel:
        """
        Complete connection routine: subscribe to notifications, initialize device, and log info.

        Parameters:
        - client: BleakClient instance (already connected)
        - preset: Preset string to apply (e.g., "p1035")
        - data_callbacks: Dict mapping characteristic UUIDs to notification callbacks
        - verbose: Print progress messages
        - low_latency: Send ``L1`` (see :meth:`initialize_device`). Off by default.
        - control: Reuse an existing :class:`ControlChannel` (one per connection).
        - on_control_raw: Called with every raw control notification payload, so
          a recorder can persist the status replies alongside the sensor data.

        Returns the :class:`ControlChannel`, whose ``state`` holds the parsed
        ``v6``/``s`` replies (firmware, hardware, serial, battery percent).

        This function:
        1. Subscribes to control notifications (for device info)
        2. Subscribes to all requested data characteristics
        3. Initializes device with preset
        4. Waits for and logs device info (firmware, battery)
        """
        if control is None:
            control = ControlChannel()
        device_info_logged = False

        def _on_control(_, data: bytearray):
            nonlocal device_info_logged
            payload = bytes(data)
            if on_control_raw is not None:
                try:
                    on_control_raw(payload)
                except Exception:
                    pass
            control.feed(payload)

            if device_info_logged:
                return
            firmware = control.firmware
            battery = control.battery_percent
            if firmware is not None and battery is not None:
                print(f"Connected to device (firmware {firmware}): battery {battery}%")
                device_info_logged = True

        # Subscribe to control notifications first
        try:
            await client.start_notify(MuseS.CONTROL_UUID, _on_control)
            if verbose:
                print(f"Subscribed to control notifications on {MuseS.CONTROL_UUID}")
        except Exception as e:
            if verbose:
                print(
                    f"Warning: could not subscribe to control {MuseS.CONTROL_UUID}: {e}"
                )

        # Subscribe to data characteristics
        for uuid, callback in data_callbacks.items():
            try:
                await client.start_notify(uuid, callback)
                if verbose:
                    print(f"Subscribed to notifications on {uuid}")
            except Exception as e:
                if verbose:
                    print(f"Warning: could not subscribe to {uuid}: {e}")

        # Initialize device (sends commands and triggers control responses)
        await MuseS.initialize_device(
            client, preset, verbose, low_latency=low_latency
        )

        # Wait for device info to be logged (up to 2 seconds)
        if verbose:
            print("Waiting for device info...")
        for _ in range(20):
            if device_info_logged:
                break
            await asyncio.sleep(0.1)

        if not device_info_logged and verbose:
            print("Warning: Did not receive device info (firmware/battery)")

        return control

    @staticmethod
    async def query_status(
        client,
        control: ControlChannel,
        timeout: float = 2.0,
    ) -> Optional[Dict[str, Any]]:
        """Send ``s`` and wait for the device's status reply.

        Requires an existing subscription to ``CONTROL_UUID`` feeding
        ``control`` (``connect_and_initialize`` sets one up).  Returns the
        parsed reply dict -- ``{"hn", "sn", "ma", "hs", "id", "bp", "ps",
        "ln", "ch", "rc"}`` on firmware 3.1.23 -- or ``None`` on timeout.
        """
        seen = len(control.replies)
        try:
            await MuseS.send_command(client, "s")
        except Exception:
            return None

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
            for reply in control.replies[seen:]:
                if "bp" in reply:
                    return reply
            seen = len(control.replies)
        return None

    @staticmethod
    async def stop_streaming(client, verbose: bool = False) -> None:
        """Send halt command to stop streaming."""
        try:
            await MuseS.send_command(client, "h", verbose)
        except Exception:
            pass


# ===================================
# Standalone status query
# ===================================
async def _get_status_async(
    address: str, timeout: float = 15.0, verbose: bool = False
) -> Dict[str, Any]:
    import bleak

    control = ControlChannel()
    async with bleak.BleakClient(address, timeout=timeout) as client:
        await client.start_notify(
            MuseS.CONTROL_UUID, lambda _, data: control.feed(bytes(data))
        )
        # "v6" first, then "s": a "s" sent immediately after a fresh
        # start_notify is frequently answered with silence.
        await MuseS.send_command(client, "v6")
        await asyncio.sleep(0.3)
        await MuseS.query_status(client, control)
        try:
            await client.stop_notify(MuseS.CONTROL_UUID)
        except Exception:
            pass
    if verbose:
        print(json.dumps(control.state, indent=2))
    return dict(control.state)


def get_status(address: str, timeout: float = 15.0, verbose: bool = False) -> Dict[str, Any]:
    """Connect to a Muse and read its self-reported status.

    Returns the merged ``v6`` + ``s`` replies, e.g.::

        {"fw": "3.1.23", "hw": "01.1", "sp": "Athena_RevE",
         "hn": "MuseS-1E2B", "sn": "7011-AH29-1E2B", "bp": 89.11, "ps": 4161}

    ``bp`` is battery percent and ``ps`` is pack millivolts.  This is the
    authoritative source: the device has no standard BLE Battery Service (GATT
    enumeration shows only the proprietary ``0000fe8d`` service), so the
    control channel is the only place battery is reported reliably.

    .. warning::
       Only one BLE client may own a Muse at a time.  Do **not** call this
       while :func:`~OpenMuse.record` or :func:`~OpenMuse.stream` holds the
       device -- connecting alongside them can leave the headset in a
       transitional state and kill the recording.  During a recording, battery
       is sampled automatically into the ``BATTERY`` stream instead (see the
       ``battery_interval`` argument of :func:`~OpenMuse.record`).
    """
    return _run(_get_status_async(address, timeout=timeout, verbose=verbose))
