import asyncio
import os
import signal
import time
from typing import Iterable, List, Optional, Sequence, Union

import bleak

from .backends import _run
from .decode import sensor_types_in
from .muse import (describe_preset, HandshakeFailed, MuseS,
                   preset_for_sensors)
from .utils import get_utc_timestamp


# Seconds allowed for the vendor teardown (L1, h) on the way out. Short on
# purpose: this is spent inside every dropout's reaction time, which is already
# the dominant cost of a dropout (see the project's CLAUDE.md). Two writes on a
# live link need a fraction of this; on a dead one no amount of waiting helps.
_TEARDOWN_TIMEOUT = 3.0


class RecordingStalled(RuntimeError):
    """No notification arrived within ``stall_timeout`` seconds.

    Raised so a supervisor can act.  A dropped BLE link does not stop
    ``record()`` on its own: the coroutine simply keeps awaiting notifications
    that will never arrive, writes nothing, and -- before this existed --
    returned success when the requested duration finally elapsed.  With the
    default 12 h duration that silently costs an entire night, and systemd or
    any other supervisor sees a clean run and never restarts it.
    """


class RecordingResult:
    """Outcome of one device's recording.

    ``record()`` used to return ``None``, so a caller could not tell a
    productive run from one that connected and received nothing.  Check
    ``notifications`` / ``bytes_written`` to detect a zero-yield run.
    """

    __slots__ = ("address", "outfile", "notifications", "bytes_written", "duration", "stalled")

    def __init__(
        self,
        address: str,
        outfile: str,
        notifications: int,
        bytes_written: int,
        duration: float,
        stalled: bool,
    ) -> None:
        self.address = address
        self.outfile = outfile
        self.notifications = notifications
        self.bytes_written = bytes_written
        self.duration = duration
        self.stalled = stalled

    def __repr__(self) -> str:
        return (
            f"RecordingResult(address={self.address!r}, outfile={self.outfile!r}, "
            f"notifications={self.notifications}, bytes_written={self.bytes_written}, "
            f"duration={self.duration:.1f}, stalled={self.stalled})"
        )


async def _record_async(
    address: str,
    duration: float,
    outfile: str,
    preset: str = "p1041",
    subscribe_chars: Optional[Iterable[str]] = None,
    verbose: bool = True,
    low_latency: bool = False,
    stall_timeout: Optional[float] = 120.0,
    sensor_timeout: Optional[float] = 60.0,
    battery_interval: Optional[float] = 300.0,
) -> RecordingResult:
    if subscribe_chars is None:
        subscribe_chars = list(MuseS.DATA_CHARACTERISTICS)

    notified = 0
    bytes_written = 0
    last_notification = time.monotonic()
    stream_started = asyncio.Event()

    # Stream watchdog. A Muse multiplexes every sensor onto one characteristic,
    # so "notifications are arriving" says nothing about *which* sensors are.
    # Observed 2026-08-05: after a dropout the headset streamed ACC/OPTICS at
    # full rate with no EEG for eight hours, and the recorder called that a
    # healthy session because bytes were flowing.
    #
    # BATTERY is excluded: it is answered on the control channel every
    # battery_interval (default 300 s), so it cannot be expected inside the
    # grace window.
    layout = describe_preset(preset) or {}
    sensors_required = {
        name for name, count in layout.items() if count and name != "BATTERY"
    }
    sensors_seen: set = set()
    sensors_pending = bool(sensors_required)

    # Ensure output directory exists
    try:
        outdir = os.path.dirname(os.path.abspath(outfile))
        if outdir and not os.path.exists(outdir):
            os.makedirs(outdir, exist_ok=True)
    except Exception:
        pass

    # Open output file in text mode and append
    f = open(outfile, "a", encoding="utf-8")

    def _write(uuid: str, payload: bytes) -> None:
        nonlocal bytes_written
        line = f"{get_utc_timestamp()}\t{uuid}\t{payload.hex()}\n"
        f.write(line)
        bytes_written += len(line)

    # Raw recording only
    def _callback(uuid: str):
        def inner(_, data: bytearray):
            nonlocal notified, last_notification, sensors_pending
            notified += 1
            last_notification = time.monotonic()
            payload = bytes(data)
            _write(uuid, payload)
            # Which sensors are actually arriving. Only while something is
            # still missing - once every expected stream has been seen this
            # costs nothing for the rest of the night.
            if sensors_pending:
                try:
                    sensors_seen.update(sensor_types_in(payload))
                    sensors_pending = bool(sensors_required - sensors_seen)
                except Exception:
                    sensors_pending = False
            if not stream_started.is_set():
                stream_started.set()

        return inner

    def _on_control_raw(payload: bytes) -> None:
        # Persist status replies alongside the sensor data. decode_rawdata()
        # reassembles them into authoritative BATTERY rows; parse_message()
        # skips them, so older readers are unaffected.
        _write(MuseS.CONTROL_UUID, payload)

    if verbose:
        print(f"Connecting to {address} ...")

    stalled = False
    cancelled = False
    start = time.monotonic()

    try:
        async with bleak.BleakClient(address, timeout=15.0) as client:
            if verbose:
                print("Connected. Subscribing and configuring ...")

            # Build callbacks dict for all data characteristics
            data_callbacks = {uuid: _callback(uuid) for uuid in subscribe_chars}

            # A signal must unwind through the teardown below rather than kill
            # us where we stand. Python's default disposition for SIGTERM ends
            # the process immediately, so the `finally` that halts the headset
            # never ran on the path that matters most: a supervisor's
            # file-growth watchdog `kill`ing this process on a dropout. The
            # CancelledError arm below has always been written for this and
            # never received one. Cancelling the task delivers it.
            task = asyncio.current_task()
            loop = asyncio.get_running_loop()
            installed = []
            stopping = False

            def _request_stop():
                # Once only. A second signal must not re-cancel a task that is
                # already inside its teardown; a supervisor that loses patience
                # has SIGKILL, which is the correct backstop.
                nonlocal stopping
                if stopping:
                    return
                stopping = True
                if verbose:
                    print("Signal received; halting the headset before exit ...")
                task.cancel()

            for signame in ("SIGTERM", "SIGINT"):
                sig = getattr(signal, signame, None)
                if sig is None:
                    continue
                try:
                    loop.add_signal_handler(sig, _request_stop)
                except (NotImplementedError, RuntimeError, ValueError):
                    # Windows has no add_signal_handler, and off the main thread
                    # it is refused. Leave the default disposition rather than
                    # fail the recording; the loss is a graceful exit, not data.
                    pass
                else:
                    installed.append(sig)

            # The handshake is inside the guarded region on purpose. It sends
            # the preset and dc001, so from its first write onwards the headset
            # may already be streaming -- and the two ways it goes wrong are
            # exactly the ways that leave it configured with nobody attached: a
            # supervisor's connect-timeout kill, and HandshakeFailed, which is
            # raised *because* the headset came up in a configuration nobody
            # asked for. Both now halt it on the way out.
            try:
                control = await MuseS.connect_and_initialize(
                    client,
                    preset,
                    data_callbacks,
                    verbose,
                    low_latency=low_latency,
                    on_control_raw=_on_control_raw,
                )

                # Streaming is now active (callbacks are registered and device
                # is configured); data starts flowing asynchronously.
                last_notification = time.monotonic()

                if verbose:
                    print(f"Recording for {duration} seconds to {outfile} ...")

                start = time.monotonic()
                next_battery = (
                    start + battery_interval
                    if battery_interval and battery_interval > 0
                    else None
                )

                while time.monotonic() - start < duration:
                    await asyncio.sleep(0.05)
                    now = time.monotonic()

                    # A dead link and a quiet one look identical from here
                    # unless the gap since the last notification is tracked.
                    if (
                        stall_timeout
                        and stall_timeout > 0
                        and now - last_notification > stall_timeout
                    ):
                        stalled = True
                        break

                    # Stream watchdog: the requested sensors must actually turn
                    # up, not merely *some* bytes. Checked once, at the end of
                    # the grace window; after that sensors_pending is False and
                    # this costs a comparison per loop.
                    if (
                        sensors_pending
                        and sensor_timeout
                        and sensor_timeout > 0
                        and now - start > sensor_timeout
                    ):
                        missing = sorted(sensors_required - sensors_seen)
                        raise HandshakeFailed(
                            f"{address}: preset {preset!r} asked for "
                            f"{sorted(sensors_required)} but {missing} never "
                            f"arrived in {sensor_timeout:.0f}s "
                            f"({notified} notifications, "
                            f"{sorted(sensors_seen) or 'nothing'} seen). The "
                            f"headset is streaming a configuration it was not "
                            f"asked for; it usually needs a power cycle."
                        )

                    # Periodic status query: battery over the whole recording
                    # is the useful artefact, not a single reading at connect
                    # time. Only this process can do it -- one BLE client owns
                    # the headset, and that is this one.
                    if next_battery is not None and now >= next_battery:
                        next_battery = now + battery_interval
                        try:
                            reply = await MuseS.query_status(client, control)
                            if verbose and reply and "bp" in reply:
                                print(f"Battery: {reply['bp']}%")
                        except Exception as exc:
                            if verbose:
                                print(f"Warning: status query failed: {exc}")
            except asyncio.CancelledError:
                cancelled = True
            finally:
                for sig in installed:
                    try:
                        loop.remove_signal_handler(sig)
                    except Exception:
                        pass
                # Bounded: on a dead link a GATT write does not fail, it hangs
                # until BlueZ gives up, and this sits inside a supervisor's own
                # patience. Shielded because we may arrive here already
                # cancelled, which would otherwise abort the teardown on its
                # first await - the exact thing this change exists to prevent.
                try:
                    await asyncio.wait_for(
                        asyncio.shield(MuseS.stop_streaming(client, verbose)),
                        timeout=_TEARDOWN_TIMEOUT,
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    if verbose:
                        print(
                            f"Warning: teardown did not complete within "
                            f"{_TEARDOWN_TIMEOUT:.0f}s; the link was probably "
                            f"already gone"
                        )

            # Unsubscribe
            for cuuid in subscribe_chars:
                try:
                    await client.stop_notify(cuuid)
                except Exception:
                    pass
            # Also try to stop control notify
            try:
                await client.stop_notify(MuseS.CONTROL_UUID)
            except Exception:
                pass

    finally:
        try:
            f.flush()
            f.close()
        except Exception:
            pass

    elapsed = time.monotonic() - start
    if verbose:
        why = "Stopped by signal" if cancelled else "Done"
        print(f"{why}. Wrote {notified} notifications ({bytes_written} bytes) to {outfile}.")

    if stalled:
        raise RecordingStalled(
            f"{address}: no BLE notification for {stall_timeout:.0f}s after "
            f"{elapsed:.0f}s of a {duration:.0f}s recording "
            f"({notified} notifications, {bytes_written} bytes written to {outfile}). "
            f"The link is down; the process would otherwise have sat idle until "
            f"the duration elapsed and then reported success."
        )

    return RecordingResult(
        address=address,
        outfile=outfile,
        notifications=notified,
        bytes_written=bytes_written,
        duration=elapsed,
        stalled=False,
    )


def record(
    address: Union[str, List[str]],
    duration: float = 30.0,
    outfile: str = "muse_record.txt",
    preset: Optional[str] = None,
    verbose: bool = True,
    sensors: Optional[Sequence[str]] = None,
    low_latency: bool = False,
    stall_timeout: Optional[float] = 120.0,
    sensor_timeout: Optional[float] = 60.0,
    battery_interval: Optional[float] = 300.0,
) -> List[RecordingResult]:
    """
    Connect to one or more Muse devices, stream notifications, and append raw packets to text file(s).
    Supports recording from multiple devices simultaneously (e.g., for hyperscanning studies).

    Each line written: ISO8601 UTC timestamp, characteristic UUID, hex payload.

    Parameters
    - address: Device MAC address (Windows) or identifier (platform-dependent).
               Can be a single address (str) or a list of addresses (List[str]) for multi-device recording.
    - duration: Recording duration in seconds.
    - outfile: Path to output text file. For multiple devices, device address will be appended to filename.
    - preset: Preset string to send (e.g., "p1041" or "p21"). Defaults to
      "p1041" (every sensor) unless ``sensors`` is given. See
      ``OpenMuse.PRESETS`` for what each preset enables -- it is the only
      control over which sensors the *headset* runs, and therefore the only
      one that saves power at the source.
    - sensors: Sensor types to enable, e.g. ``["EEG", "ACCGYRO"]``, resolved to
      a preset via :func:`OpenMuse.preset_for_sensors`. Mutually exclusive with
      ``preset``. Leaving OPTICS out turns off sixteen LEDs and drops ~30% of
      the payload.
    - verbose: Print progress messages.
    - low_latency: Send ``L1``, requesting a short BLE connection interval.
      Off by default: recording does not need it (nothing cares whether a
      sample arrives 15 ms or 200 ms late) and the radio duty cycle it forces
      is a large part of the power budget. Turn it on only if you are
      consuming the file live.
    - stall_timeout: Seconds without any BLE notification after which the
      recording is abandoned and :class:`RecordingStalled` is raised, so a
      supervisor can restart it. ``None`` restores the old behaviour of waiting
      out the full duration in silence. Default 120 s.
    - battery_interval: Seconds between status queries. Each reply is written
      to the raw file and decodes into an authoritative ``BATTERY`` row
      (percent *and* pack millivolts), so a drain curve for the whole night
      falls out of the normal decode. ``None`` disables it. Default 300 s.

    Returns a list of :class:`RecordingResult`, one per device, reporting the
    notification and byte counts so a caller can detect a zero-yield run.

    Raises :class:`RecordingStalled` if the link dies (see ``stall_timeout``).
    """
    # Convert single address to list for uniform handling
    addresses = [address] if isinstance(address, str) else address

    # Remove duplicates if any
    addresses = list(set(addresses))

    if not addresses:
        raise ValueError("address must be a non-empty string or list of strings")
    if duration <= 0:
        raise ValueError("duration must be positive")
    if not isinstance(outfile, str) or not outfile:
        raise ValueError("outfile must be a non-empty path string")

    if sensors is not None:
        if preset is not None:
            raise ValueError("pass either 'preset' or 'sensors', not both")
        preset = preset_for_sensors(sensors)
        if verbose:
            print(f"Sensors {list(sensors)} -> preset {preset!r}")
    elif preset is None:
        preset = "p1041"

    chars = list(MuseS.DATA_CHARACTERISTICS)

    async def run_multirecord():
        if verbose and len(addresses) > 1:
            print(f"Starting recording for {len(addresses)} device(s)...")

        tasks = []
        for addr in addresses:
            # Generate unique filename for each device
            if len(addresses) > 1:
                # Sanitize address for filename
                sanitized_addr = addr.replace(":", "").replace("-", "")

                # Insert address into provided filename
                if "." in outfile:
                    parts = outfile.rsplit(".", 1)
                    filename = f"{parts[0]}_{sanitized_addr}.{parts[1]}"
                else:
                    filename = f"{outfile}_{sanitized_addr}"
            else:
                filename = outfile

            # Create task for this device
            tasks.append(
                _record_async(
                    address=addr,
                    duration=duration,
                    outfile=filename,
                    preset=preset,
                    subscribe_chars=chars,
                    verbose=verbose,
                    low_latency=low_latency,
                    stall_timeout=stall_timeout,
                    sensor_timeout=sensor_timeout,
                    battery_interval=battery_interval,
                )
            )

        # Run all recordings concurrently
        return await asyncio.gather(*tasks)

    return _run(run_multirecord())
