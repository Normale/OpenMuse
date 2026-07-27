# Changelog

## [0.1.9]

### Fixed
- **Optics16 channel order** (issue #24): the device transmits *wavelength-major*, not location-major. Every channel name previously pointed at a different photodiode, so any name-based selection read the wrong sensor and pulse oximetry (which needs a specific RED/IR pair) could not be done correctly. `OPTICS16_CHANNELS` is now `RED×4, AMB×4, NIR×4, IR×4` in `LI, RI, LO, RO` order. Confirmed on firmware 3.1.23. Optics8 and Optics4 are separate packet types and now have their own explicit layouts (`OPTICS8_CHANNELS`, `OPTICS4_CHANNELS`) instead of being sliced out of the 16-channel list — the 4-channel selection was previously taken from indices `(4,5,6,7)`, which under the corrected order is the ambient group.
- **One malformed subpacket no longer discards the whole decode**: a mixed-width subpacket raised `ValueError: could not broadcast input array from shape (4,4) into shape (4,8)` out of `decode_rawdata()`, losing the entire file. On a 25-minute capture that threw away 386,092 recoverable EEG rows over two bad subpackets. Streams now adopt the layout carrying the most samples, NaN-pad narrower EEG subpackets (channels 0–3 are TP9/AF7/AF8/TP10 in both layouts), drop incompatible ones, and report both counts via a `RuntimeWarning` and `decode_rawdata(..., return_info=True)`.
- **A declared packet length below the header size** crashed `_parse_packets` with `IndexError` and would otherwise have looped forever without advancing. Trailing padding bytes look exactly like this.
- **Battery percentages outside 0–100 are now `NaN`** instead of a number known to be wrong (issue #28). Values of 150.0, 228.0 and 229.0 have been observed in the wild: the TAG scan that locates battery subpackets can desynchronise, and the resulting "percentage" is an arbitrary `uint16 / 256`.
- **`import OpenMuse` no longer requires `mne-lsl` or `vispy`**, which blocked the whole library on platforms without those wheels (e.g. 32-bit armhf Raspberry Pi).

### Added
- **Authoritative battery via the control channel**: the device's reply to `s` (`{"bp": 89.11, "ps": 4161, ...}`) was already requested and then discarded. `ControlChannel` now reassembles it — the replies arrive as length-prefixed 20-byte fragments, are NUL-padded and separated by a checksum byte, so they must be found by brace matching rather than split on `}{`. `record(battery_interval=300)` re-issues `s` on a timer and writes each reply into the raw file, so a whole-night **drain curve** falls out of the normal decode with `source == "status"` and a `battery_millivolts` column. `get_status(address)` reads it standalone.
- **`decode_rawdata(..., timebase="absolute")`**: a timebase anchored to the ISO-8601 arrival stamp on every raw line, split at real dropouts and least-squares fitted per segment. Monotonic, comparable across chunks and files, and it *measures* the sampling rate (255.62 Hz EEG, 64.15 Hz optics, 52.27 Hz accgyro on a reference capture) instead of assuming the nominal one, which is ~0.14% fast — about 40 s of drift over an 8 h night.
- **`decode_rawdata(..., return_info=True)`** reports measured rate, segment count, samples, and skipped/padded subpackets per stream.
- **Stall detection in `record()`**: a dropped BLE link used to leave the coroutine awaiting notifications that never arrive — writing nothing, raising nothing, and returning success when the duration elapsed, so a supervisor never restarted it. `stall_timeout` (default 120 s) now raises `RecordingStalled`. `record()` returns `RecordingResult` objects reporting notification and byte counts.
- **`PRESETS`, `describe_preset()`, `preset_for_sensors()` and `OpenMuse presets`**: the preset string is the only control over which sensors the headset actually runs, and it had no representation in code. The table is derived from the committed captures and re-derived by `tests/test_presets.py`. `record(sensors=["EEG", "ACCGYRO"])` resolves to a preset, so callers can express intent instead of magic strings.
- Optional dependency extras: `OpenMuse[stream]`, `OpenMuse[view]`, `OpenMuse[all]`.

### Changed
- **Low-latency mode is now explicitly stood down rather than merely not requested.** The handshake order now follows the manufacturer's own application, captured from an Android HCI snoop log during a sleep session on firmware 3.1.23: `v7, s, h, p21, s, L1, dc001, h, p1035, s, L1, dc001, L0`. Two corrections fall out of that. `L1` belongs *before* `dc001`, so the stream comes up without waiting on a long connection interval. And there is an explicit **`L0`** stand-down, which the app sends ~4.8 s after streaming begins — omitting `L1` is **not** equivalent, because the headset retains whatever mode it was last put in and can inherit low latency from a previous session. `record()` therefore uses `L1` to start the stream and then sends `L0`; `stream()` keeps `L1`. New `version_command` and `low_latency_settle` arguments on `initialize_device()`.
- Documented that the vendor app's session preset is **`p1035`** (EEG4 + Optics4 + ACCGYRO), not `p1041` — four optics channels rather than sixteen, which keeps PPG affordable.
- `requires-python` relaxed from `>=3.12` to `>=3.10`; the `>=3.12` floor came from `mne-lsl`, which only `stream`/`view` need. CI now also tests 3.10 and 3.11.
- `matplotlib` dropped from the dependency list (unused).
- Control-characteristic notifications are recognised by UUID and no longer parsed as sensor packets.
- README: `p1034` corrected to EEG**4** (its packets carry TAG `0x11`, not `0x12`).


## [0.1.8]

### Added
- **Hyperscanning Support**: The `stream` command now accepts multiple MAC addresses (space-separated) to stream from multiple Muse devices simultaneously in a single process.
- **Multi-device Recording Support**: The `record` command now accepts multiple MAC addresses (space-separated) to record from multiple Muse devices simultaneously in a single process, similar to the `stream` command. Device addresses are automatically appended to filenames to avoid collisions.
- **Stream selection**: Added the --sensors flag to select what sensors to stream. `OpenMuse stream <device> --sensors EEG OPTICS` (should just stream EEG and OPTICS).

### Changed
- **Battery Sampling Rate**: Updated battery sampling rate from 1.0 Hz to 0.2 Hz to reflect actual new firmware behavior (0x88 packets arrive ~every 5 seconds).
- **Documentation**: Updated `decode.py` docstring to accurately describe `pkt_index` behavior - provides 100% correct temporal ordering but indices may have gaps (not strictly sequential).
- **Streaming Stability**: Added channel count validation in `_queue_samples()` to prevent `ValueError: dimension mismatch` crashes when transitioning between different sensor configurations (e.g., 8-channel vs 4-channel EEG modes).
- **Default Clock Model**: Changed default clock synchronization from `adaptive` to `windowed`. Validation testing showed `windowed` provides the most stable timing across different devices (based on internal controlled testing). See `clocks.py` docstring for full validation results.
- **Improved Multi-Device Viewer Behavior**: When multiple Muse devices are streaming, the `view` command now automatically displays only the first device and shows a warning message listing all detected devices. Users can specify which device to view using the `--address` argument. This prevents the confusing behavior where all channels from all devices were mixed in a single viewer window.
- **0x88 Packet Support**: Added handling for new 0x88 packet type found in newer firmware. Contains embedded battery info and ~200 bytes of unknown data (possibly processed signals).
- **Viewer Improvements**:
  - Battery display now hidden when battery stream is unavailable (instead of showing "--%%")
  - Channel order now consistent: EEG at top, then ACC/GYRO, then OPTICS at bottom
  - 5-level battery display colour indicator added (as opposed to previous 3-level colour indicator)
  - Movement of battery display to top-left (from top-right)
  - Tick values, time axis, channel labels, and battery indicator now appropriately scale with window size
  - Position of channel labels, tick values, and grid lines altered for readability
  - New header added above plot area containing idiosyncratic device MAC address
- **Stream Naming Convention**: Updated LSL stream names to include device identifiers for better multi-device support.
  - Muse: `Muse-{sensor_type} ({device_id})` (e.g., `Muse-EEG (0055DA)`)
  - BITalino: `BITalino ({address})` (e.g., `BITalino (20:17:09:18:49:99)`)
- **Clock Synchronization**: Refactored clock synchronization logic into a dedicated `clocks.py` module.
- **CLI**: Added `--clock` argument to `stream` command to select synchronization model.
- **View Address Filter**: Added `--address` argument to `view` command to filter streams by MAC address, enabling multi-device setups.
- **Dependencies**: Replaced `pylsl` with `mne_lsl` for stream resolution.



## [0.1.2]

### Added

### Changed
- **Stream-Relative Timestamps**: Timestamps are now generated relative to stream start (base_time = 0.0) instead of device boot time, eliminating the need for complex re-anchoring in LSL streaming while maintaining device timing precision.
- **Conditional Re-Anchoring**: LSL streaming now only applies timestamp re-anchoring for edge cases (timestamps >30s in past), providing better synchronization with other LSL streams.
- **Global Timestamping**: Replaced per-message timestamping with global subpacket sorting and timestamping in `decode_rawdata()` to account for cross-message timing inversions.
- **Output Change**: The `parse_message()` function now always returns raw subpackets (Dict[str, List[Dict]]) for flexible processing. Users should call `make_timestamps()` explicitly on the subpackets to get numpy arrays.

### Deprecated

### Removed

### Fixed

### Security

---

## [0.1.0] - 2025-10-16

Initial release.
