"""
Muse BLE Message Parser
============================================

Implementation that follows the actual message structure:
MESSAGE → PACKET → DATA SUBPACKETS

Message Structure:
------------------
Each BLE MESSAGE contains one or more PACKETS. Each PACKET has a 14-byte header
followed by a data section containing multiple DATA SUBPACKETS:

MESSAGE (BLE transmission with timestamp)
  └─ PACKET (14-byte header + data section)
       ├─ First Subpacket: Raw sensor data (no TAG, no header)
       └─ Additional Subpackets: [TAG (1 byte)][Header (4 bytes)][Data (variable)]
            ├─ TAG: Sensor type identifier (e.g., 0x47=ACCGYRO, 0x12=EEG8)
            ├─ subpkt_index: Per-sensor-type sequence counter (0-255, wraps)
            ├─ Unknown bytes: 3 metadata bytes (purpose unknown)
            └─ Sensor data: Variable length depending on sensor type

Timestamp Calculation & Device Timing:
---------------------------------------
Device timestamps (pkt_time) are derived from a 256 kHz hardware clock with 3.906 µs resolution.
Multiple packets often share identical pkt_time values (~99% of packets are in duplicate groups).

Timestamp generation per message:
  1. Sort packets by (pkt_time, pkt_index, subpkt_index)
     - pkt_index: Packet sequence counter (0-255), ensures correct ordering of duplicates
     - Analysis: pkt_index provides 100% correct temporal ORDERING within duplicate groups,
       though indices may have gaps (not strictly consecutive, as some packets arrive with
       different timestamps). The arrival order always matches pkt_index order.
  2. Use first packet's pkt_time as anchor
  3. Generate uniform timestamps: anchor + (sample_index / sampling_rate)

Hardware Timing Artifacts:
  - ~3-4% of pkt_time values have timing inversions (timestamps go backwards)
  - pkt_index provides correct ordering (100% accurate for packet sequencing)
  - Inversions likely due to async sensor buffering and clock jitter
  - Final monotonicity ensured by stream.py buffering/sorting before LSL output

"""

import json
import struct
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union
from datetime import datetime, timezone
import numpy as np
import pandas as pd


# Protocol constants
PACKET_HEADER_SIZE = 14  # Size of packet header in bytes
SUBPACKET_HEADER_SIZE = 5  # Size of subpacket header (TAG + 4 bytes)
DEVICE_CLOCK_HZ = 256000.0  # Device clock frequency: 256 kHz

# Sensor configuration mapping
# Maps TAG byte to sensor type, channels, samples, sampling rate, and data length
SENSORS = {
    0x11: {
        "type": "EEG",
        "n_channels": 4,
        "n_samples": 4,
        "rate": 256.0,
        "data_len": 28,
    },
    0x12: {
        "type": "EEG",
        "n_channels": 8,
        "n_samples": 2,
        "rate": 256.0,
        "data_len": 28,
    },
    0x34: {
        "type": "OPTICS",
        "n_channels": 4,
        "n_samples": 3,
        "rate": 64.0,
        "data_len": 30,
    },
    0x35: {
        "type": "OPTICS",
        "n_channels": 8,
        "n_samples": 2,
        "rate": 64.0,
        "data_len": 40,
    },
    0x36: {
        "type": "OPTICS",
        "n_channels": 16,
        "n_samples": 1,
        "rate": 64.0,
        "data_len": 40,
    },
    0x47: {
        "type": "ACCGYRO",
        "n_channels": 6,
        "n_samples": 3,
        "rate": 52.0,
        "data_len": 36,
    },
    0x53: {
        "type": "Unknown",
        "n_channels": 0,
        "n_samples": 0,
        "rate": 0.0,
        "data_len": 24,
    },
    # 0x88: New firmware battery/status packet
    # Contains battery percentage in first 2 bytes, plus additional unknown data
    # Battery is extracted from this packet for new firmware (replaces 0x98)
    # NOTE: data_len is set to minimum observed (196 bytes) but actual packets
    # vary from 188-230 bytes. The decoder only reads first 2 bytes for battery.
    0x88: {
        "type": "BATTERY",
        "n_channels": 1,
        "n_samples": 1,
        "rate": 0.2,  # ~1 packet every 5-6 seconds (variable)
        "data_len": 188,  # Minimum observed; actual packets vary 188-230 bytes
    },
    0x98: {
        "type": "BATTERY",
        "n_channels": 1,
        "n_samples": 1,
        "rate": 1.0,
        "data_len": 20,
    },
}

# Scaling constants
ACC_SCALE = 0.0000610352
GYRO_SCALE = -0.0074768
OPTICS_SCALE = 1.0 / 32768.0
EEG_SCALE = 1450.0 / 16383.0

# Standard channel label definitions shared across modules
EEG_CHANNELS = (
    "EEG_TP9",
    "EEG_AF7",
    "EEG_AF8",
    "EEG_TP10",
    "AUX_1",
    "AUX_2",
    "AUX_3",
    "AUX_4",
)

ACCGYRO_CHANNELS = (
    "ACC_X",
    "ACC_Y",
    "ACC_Z",
    "GYRO_X",
    "GYRO_Y",
    "GYRO_Z",
)

# Optics16 (TAG 0x36) channel order.
#
# The device transmits WAVELENGTH-MAJOR, not location-major (see issue #24).
# The previous location-major labelling put a name on every channel that
# belonged to a different sensor, which makes any name-based channel selection
# read the wrong photodiode and makes pulse oximetry (which needs a specific
# RED/IR pair) impossible to get right.
#
# Evidence, reproduced by tests/test_optics_mapping.py on this repo's own
# committed captures (data_p1041.txt, data_p1042.txt, test_optics16.txt) and
# independently on a full overnight recording (firmware 3.1.23, Athena_RevE,
# preset p1041):
#
#   Channel indices 4-7 always form a tight, high, near-constant quartet that
#   is clearly separated from the other twelve -- the signature of an ambient
#   reference (LED off), not a measurement:
#
#     capture            idx 4-7 means            next highest channel
#     data_p1041.txt     11.55 11.37 13.59 12.73  10.33
#     test_optics16.txt  12.33 12.18 15.24 15.10   5.63
#     overnight (3.1.23) 10.274 10.261 10.269 10.257 (std 0.0005)  3.65
#
#   On the overnight capture -- subject asleep, still, dark room, so the
#   cardiac test is clean -- four channels labelled AMB under the old order
#   carried a strong heartbeat (cardiac SNR 59-722), which is physically
#   impossible for an ambient-light channel, while indices 4-7 carried none.
#   Scoring both labellings against "AMB must have no pulse, LED channels must
#   not be dead": location-major separated AMB from LED by 1.4x (12/16
#   channels consistent); wavelength-major by 44x (16/16).
#
# CAVEAT: confirmed on firmware 3.1.23 (issue #24 reported 3.1.19). If the
# order turns out to be firmware-dependent it must be keyed on the version
# reported by "v6" rather than assumed globally.
OPTICS16_CHANNELS = (
    "OPTICS_LI_RED",
    "OPTICS_RI_RED",
    "OPTICS_LO_RED",
    "OPTICS_RO_RED",
    "OPTICS_LI_AMB",
    "OPTICS_RI_AMB",
    "OPTICS_LO_AMB",
    "OPTICS_RO_AMB",
    "OPTICS_LI_NIR",
    "OPTICS_RI_NIR",
    "OPTICS_LO_NIR",
    "OPTICS_RO_NIR",
    "OPTICS_LI_IR",
    "OPTICS_RI_IR",
    "OPTICS_LO_IR",
    "OPTICS_RO_IR",
)

# Optics8 (TAG 0x35) is a different packet, not a slice of Optics16. Measured
# on data_p1034.txt, its eight channels fall into two groups of four by DC
# level -- {0,1,4,5} high (~8.2-9.4) and {2,3,6,7} low (~1.8-2.1) -- which is
# consistent with the ordering below (two wavelengths across the outer pair,
# then across the inner pair). No ambient channel is present.
OPTICS8_CHANNELS = (
    "OPTICS_LO_NIR",
    "OPTICS_RO_NIR",
    "OPTICS_LO_IR",
    "OPTICS_RO_IR",
    "OPTICS_LI_NIR",
    "OPTICS_RI_NIR",
    "OPTICS_LI_IR",
    "OPTICS_RI_IR",
)

# Optics4 (TAG 0x34): inner sensors only. All four carry a clear pulse
# (cardiac SNR 67-81 on data_p1035.txt), so none of them is ambient.
OPTICS4_CHANNELS = (
    "OPTICS_LI_NIR",
    "OPTICS_RI_NIR",
    "OPTICS_LI_IR",
    "OPTICS_RI_IR",
)

# Backwards-compatible alias: the unqualified name has always meant the
# 16-channel layout.
OPTICS_CHANNELS = OPTICS16_CHANNELS

BATTERY_CHANNELS = ("BATTERY_PERCENT",)


def select_eeg_channels(count: int) -> List[str]:
    if count <= len(EEG_CHANNELS):
        return list(EEG_CHANNELS[:count])
    return [f"EEG_{i+1:02d}" for i in range(count)]


_OPTICS_LAYOUTS = {
    4: OPTICS4_CHANNELS,
    8: OPTICS8_CHANNELS,
    16: OPTICS16_CHANNELS,
}


def select_optics_channels(count: int) -> List[str]:
    layout = _OPTICS_LAYOUTS.get(count)
    if layout is not None:
        return list(layout)
    return [f"OPTICS_{i+1:02d}" for i in range(count)]


# ===================================
# Control-channel (status) replies
# ===================================
# The device answers control commands ("v6", "s", ...) on CONTROL_UUID with a
# JSON object.  Three properties of that channel, all verified on firmware
# 3.1.23 / hw 01.1 / Athena_RevE, make naive parsing fail:
#
#   1. Replies arrive as 20-byte notification fragments and must be
#      concatenated.  Each fragment is length-prefixed ([len][chars...]).
#   2. Replies are NUL-padded and separated by a checksum byte, e.g.
#      b'{...}\x00\x00\x00\x00\x00c{...}\x00\x00\x00\x00\x00\x00:'
#      -- so there is no clean '}{' to split on.  Splitting that way yields
#      invalid JSON.  Objects must be found by brace matching.
#   3. A "s" sent immediately after a fresh start_notify is frequently answered
#      with silence.  Send "v6" first and keep one persistent subscription.


# Proprietary control characteristic. Recorders may interleave its
# notifications into the raw file (see record(..., battery_interval=...)), so
# the decoder has to be able to tell them apart from sensor packets.
CONTROL_UUID = "273e0001-4c4d-454d-96be-f03bac821358"


def is_control_uuid(uuid: str) -> bool:
    """True if ``uuid`` names the Muse control characteristic."""
    if not isinstance(uuid, str):
        return False
    return uuid.strip().lower().startswith("273e0001")


def _scan_json_objects(text: str) -> Tuple[List[str], int]:
    """Find complete top-level ``{...}`` objects in ``text`` by brace matching.

    Returns ``(objects, consumed)`` where ``consumed`` is the index just past
    the last complete object, so a caller streaming fragments can keep the
    unparsed tail in its buffer.
    """
    objects: List[str] = []
    depth = 0
    start = -1
    consumed = 0
    in_string = False
    escaped = False

    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            if depth > 0:
                in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    objects.append(text[start : i + 1])
                    consumed = i + 1
                    start = -1

    if depth == 0 and start < 0:
        # No partial object pending: everything up to here can be discarded.
        consumed = len(text)
    elif start >= 0:
        consumed = start

    return objects, consumed


def _strip_length_prefix(data: bytes) -> bytes:
    """Drop the leading length byte of a control notification, when present."""
    if data and data[0] == len(data) - 1:
        return data[1:]
    return data


class ControlChannel:
    """Reassembles control-characteristic notifications into JSON replies.

    Feed raw notification payloads with :meth:`feed`; each call returns the
    JSON objects that completed within it.  The most recent value of every key
    ever seen is kept in :attr:`state`, so a single object exposes the merged
    result of the ``v6`` (firmware/hardware) and ``s`` (battery/status) replies.
    """

    MAX_BUFFER = 8192

    def __init__(self) -> None:
        self._buffer = ""
        self.state: Dict[str, Any] = {}
        self.replies: List[Dict[str, Any]] = []

    def feed(self, data: bytes) -> List[Dict[str, Any]]:
        """Consume one notification payload; return newly completed replies."""
        try:
            text = _strip_length_prefix(bytes(data)).decode("utf-8", errors="ignore")
        except Exception:
            return []
        return self.feed_text(text)

    def feed_text(self, text: str) -> List[Dict[str, Any]]:
        """Same as :meth:`feed` for already-decoded text."""
        self._buffer += text
        raw_objects, consumed = _scan_json_objects(self._buffer)
        self._buffer = self._buffer[consumed:]
        if len(self._buffer) > self.MAX_BUFFER:
            # Never let a stream of garbage grow without bound.
            self._buffer = self._buffer[-self.MAX_BUFFER :]

        new: List[Dict[str, Any]] = []
        for raw in raw_objects:
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            new.append(obj)
            self.replies.append(obj)
            self.state.update(obj)
        return new

    # -- convenience accessors -------------------------------------------
    @property
    def battery_percent(self) -> Optional[float]:
        """Battery charge in percent, as reported by the device itself."""
        value = self.state.get("bp")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def battery_millivolts(self) -> Optional[float]:
        """Pack voltage in millivolts (``ps`` in the status reply)."""
        value = self.state.get("ps")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def firmware(self) -> Optional[str]:
        value = self.state.get("fw")
        return str(value) if value is not None else None


def parse_message(message: str) -> Dict[str, List[Dict]]:
    """
    Parse a BLE message into structured data.

    This parser follows the actual message structure:
    MESSAGE → PACKET (14-byte header) → DATA SUBPACKETS (TAG + 4-byte header + data)

    Parameters:
    -----------
    message : str
        Tab-separated string: timestamp, UUID, hex-encoded payload

    Returns:
    --------
    dict : Dictionary with sensor types as keys, lists of subpacket dicts as values
        Keys: "EEG", "ACCGYRO", "OPTICS", "BATTERY", "Unknown"
        Each list contains subpacket dictionaries with decoded sensor data
    """

    # Parse message line
    try:
        ts, uuid, hexstring = message.strip().split("\t", 2)
        message_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        payload = bytes.fromhex(hexstring.strip())
    except (ValueError, AttributeError):
        # Malformed message
        return _empty_result()

    # Control-characteristic notifications are JSON status replies, not sensor
    # packets: parsing them as packets manufactures garbage subpackets.
    # decode_rawdata() reassembles them separately.
    if is_control_uuid(uuid):
        return _empty_result()

    # Initialize result dictionary for parsed subpackets
    parsed_data = _empty_result()

    # Step 1: Parse all packets in the message
    packets = _parse_packets(payload, message_time, uuid)

    # Step 2: Parse data subpackets within each packet
    for pkt in packets:
        subpackets = _parse_data_subpackets(pkt)

        # Step 3: Decode data for each subpacket
        for subpkt in subpackets:
            sensor_type = subpkt["sensor_type"]
            decoded = _decode_subpacket_data(subpkt)

            if decoded is not None:
                # Add packet-level metadata to decoded subpacket
                decoded.update(
                    {
                        "pkt_index": pkt["pkt_index"],
                        "pkt_time": pkt["pkt_time"],
                        "pkt_time_raw": pkt["pkt_time_raw"],
                        "message_time": pkt["message_time"],
                        "message_uuid": pkt["message_uuid"],
                    }
                )

                parsed_data[sensor_type].append(decoded)

    # Return raw subpackets for global timestamping
    return parsed_data


def _empty_result() -> Dict[str, List]:
    """Create empty result dictionary with all sensor types."""
    return {
        "EEG": [],
        "ACCGYRO": [],
        "OPTICS": [],
        "BATTERY": [],
        "Unknown": [],
    }


def _empty_result_arrays() -> Dict[str, np.ndarray]:
    """Create empty result dictionary with numpy arrays for all sensor types."""
    return {
        "EEG": np.empty((0, 0)),
        "ACCGYRO": np.empty((0, 0)),
        "OPTICS": np.empty((0, 0)),
        "BATTERY": np.empty((0, 0)),
        "Unknown": np.empty((0, 0)),
    }


def _parse_packets(payload: bytes, message_time: datetime, uuid: str) -> List[Dict]:
    """
    Parse all packets from payload based on declared length.

    Returns list of packet dicts with header information and data section.
    """
    packets = []
    offset = 0

    while offset < len(payload):
        # Need at least PACKET_HEADER_SIZE bytes for packet header
        if offset + PACKET_HEADER_SIZE > len(payload):
            break

        # Read declared packet length
        pkt_len = payload[offset]

        # A packet shorter than its own header cannot be parsed, and a declared
        # length of 0 would both index past the end and never advance `offset`.
        # Padding bytes at the end of a BLE message routinely look like this.
        if pkt_len < PACKET_HEADER_SIZE:
            break

        # Validate we have enough bytes
        if offset + pkt_len > len(payload):
            break

        # Extract full packet
        pkt_bytes = payload[offset : offset + pkt_len]

        # Parse packet header (PACKET_HEADER_SIZE bytes)
        pkt_index = pkt_bytes[1]
        pkt_time_raw = struct.unpack_from("<I", pkt_bytes, 2)[0]
        pkt_time = (
            pkt_time_raw / DEVICE_CLOCK_HZ
        )  # Convert device clock ticks to seconds
        pkt_unknown1 = pkt_bytes[6:9]
        pkt_id = pkt_bytes[9]
        pkt_unknown2 = pkt_bytes[10:13]
        byte_13 = pkt_bytes[13]

        # Decode pkt_id to get sensor type
        pkt_config = SENSORS.get(pkt_id)
        pkt_type = pkt_config["type"] if pkt_config else None

        # Validate packet
        # Note: byte_13 is NOT always 0 in newer firmware versions.
        # It appears to be a sequence counter or metadata field.
        # Removed byte_13 == 0 check for compatibility with new firmware.
        pkt_valid = (
            pkt_type is not None
            and pkt_len == len(pkt_bytes)
            and pkt_len >= PACKET_HEADER_SIZE
        )

        # Extract data section (everything after header)
        pkt_data = (
            pkt_bytes[PACKET_HEADER_SIZE:]
            if len(pkt_bytes) > PACKET_HEADER_SIZE
            else b""
        )

        packets.append(
            {
                "pkt_len": pkt_len,
                "pkt_index": pkt_index,
                "pkt_time": pkt_time,
                "pkt_time_raw": pkt_time_raw,
                "pkt_unknown1": pkt_unknown1,
                "pkt_id": pkt_id,
                "pkt_type": pkt_type,
                "pkt_unknown2": pkt_unknown2,
                "pkt_valid": pkt_valid,
                "pkt_data": pkt_data,
                "message_time": message_time,
                "message_uuid": uuid,
            }
        )

        offset += pkt_len

    return packets


def _parse_data_subpackets(pkt: Dict) -> List[Dict]:
    """
    Parse all data subpackets within a packet's data section.

    The packet data section contains:
    1. Primary data (sensor type matching pkt_type, NO TAG, NO 4-byte header, just raw data)
    2. Additional subpackets (each with [TAG][4-byte header][data])

    Returns list of subpacket dicts with raw bytes and metadata.
    """
    subpackets = []
    pkt_data = pkt["pkt_data"]
    offset = 0

    # Step 1: Parse first data subpacket (no TAG, no header, just raw sensor data)
    # This is always the sensor type indicated by the packet header
    if pkt["pkt_valid"] and pkt["pkt_type"] is not None:
        first_type = pkt["pkt_type"]
        # Get data length from SENSORS config using pkt_id as TAG
        first_data_len = SENSORS.get(pkt["pkt_id"], {}).get("data_len", 0)

        if first_data_len > 0 and offset + first_data_len <= len(pkt_data):
            # Extract first subpacket data bytes (no TAG, no header)
            first_data_bytes = pkt_data[offset : offset + first_data_len]

            # Add first subpacket (no index or unknown bytes available)
            subpackets.append(
                {
                    "sensor_type": first_type,
                    "tag_byte": pkt["pkt_id"],  # Use packet ID as tag for config lookup
                    "subpkt_index": None,  # First subpacket has no index in data section
                    "subpkt_unknown": None,  # First subpacket has no unknown bytes
                    "data_bytes": first_data_bytes,
                }
            )

            # Move offset past first data
            offset += first_data_len

    # Step 2: Parse all additional subpackets (each with TAG + 4-byte header + data)
    while offset < len(pkt_data):
        # Check if we have enough bytes for TAG + header
        if offset + SUBPACKET_HEADER_SIZE > len(pkt_data):
            break

        tag_byte = pkt_data[offset]

        # Validate TAG (must be a known sensor TAG)
        if tag_byte not in SENSORS:
            break

        sensor_type = SENSORS[tag_byte]["type"]

        # Parse 4-byte subpacket header
        subpkt_index = pkt_data[offset + 1]
        subpkt_unknown = pkt_data[offset + 2 : offset + SUBPACKET_HEADER_SIZE]

        # Get data length from SENSORS config
        data_len = SENSORS[tag_byte].get("data_len", 0)

        if data_len == 0:
            # Unknown data length, skip this TAG
            break

        # Check if we have enough bytes for full subpacket
        if offset + SUBPACKET_HEADER_SIZE + data_len > len(pkt_data):
            break

        # Extract data bytes
        data_bytes = pkt_data[
            offset + SUBPACKET_HEADER_SIZE : offset + SUBPACKET_HEADER_SIZE + data_len
        ]

        subpackets.append(
            {
                "sensor_type": sensor_type,
                "tag_byte": tag_byte,
                "subpkt_index": subpkt_index,
                "subpkt_unknown": subpkt_unknown,
                "data_bytes": data_bytes,
            }
        )

        # Move to next subpacket
        offset += SUBPACKET_HEADER_SIZE + data_len

    return subpackets


def _decode_subpacket_data(subpkt: Dict) -> Optional[Dict]:
    """
    Decode data bytes based on TAG byte configuration.

    Returns dict with:
        - data: np.ndarray with decoded values (no timestamps)
        - subpkt_index: index value (None for first subpacket)
        - subpkt_unknown: metadata bytes (None for first subpacket)
        - sensor_type: type string
        - n_channels: number of channels
        - n_samples: number of samples

    Returns None if sensor type is not decodable (e.g., "Unknown").
    """
    sensor_type = subpkt["sensor_type"]
    data_bytes = subpkt["data_bytes"]
    tag_byte = subpkt["tag_byte"]

    # Skip Unknown type (no decoder yet)
    if sensor_type == "Unknown":
        return None

    # Get configuration from TAG (cache lookup to avoid redundant dictionary access)
    config = SENSORS.get(tag_byte)
    if config is None:
        return None

    n_channels = config["n_channels"]
    n_samples = config["n_samples"]

    # Decode based on sensor type
    if sensor_type == "ACCGYRO":
        data = _decode_accgyro_data(data_bytes)
    elif sensor_type == "BATTERY":
        data = _decode_battery_data(data_bytes)
    elif sensor_type == "EEG":
        data = _decode_eeg_data(data_bytes, n_channels)
    elif sensor_type == "OPTICS":
        data = _decode_optics_data(data_bytes, n_channels)
    else:
        return None

    if data is None:
        return None

    return {
        "sensor_type": sensor_type,
        "tag_byte": tag_byte,  # Include tag_byte for timestamp calculation
        "subpkt_index": subpkt["subpkt_index"],
        "subpkt_unknown": subpkt["subpkt_unknown"],
        "data": data,
        "n_channels": n_channels,
        "n_samples": n_samples,
    }


def _decode_accgyro_data(data_bytes: bytes) -> Optional[np.ndarray]:
    """
    Decode ACCGYRO data (36 bytes → 3 samples × 6 channels).

    Returns: np.ndarray shape (3, 6) with columns [ACC_X, ACC_Y, ACC_Z, GYRO_X, GYRO_Y, GYRO_Z]
    """
    if len(data_bytes) < 36:
        return None

    # Parse 18 int16 values, reshape to 3 samples × 6 channels
    data = np.frombuffer(data_bytes[:36], dtype="<i2", count=18).reshape(3, 6)
    data = data.astype(np.float32)

    # Apply scaling
    data[:, 0:3] *= ACC_SCALE  # Accelerometer
    data[:, 3:6] *= GYRO_SCALE  # Gyroscope

    return data


def _decode_battery_data(data_bytes: bytes) -> Optional[np.ndarray]:
    """
    Decode Battery data (first 2 bytes = SOC, a UQ8.8 fixed-point percentage).

    Works for both:
    - 0x98 packets (old firmware): battery in first 2 bytes
    - 0x88 packets (new firmware): battery in first 2 bytes

    Out-of-range results are returned as NaN rather than as a number known to
    be wrong.  Battery TAGs are also found by scanning the tail of a packet for
    known TAG bytes, and that scan can desynchronise -- the 0x98 entry declares
    ``data_len = 20`` while real 0x98 packets carry ~196-205 bytes of payload,
    so a byte inside the payload can be mistaken for a TAG.  When it does, the
    "percentage" is an arbitrary uint16/256, i.e. above 100 about 61% of the
    time.  Values like 150.0, 228.0 and 229.0 have been observed in the wild
    this way (issue #28).  A missing reading is recoverable; a plausible-
    looking wrong one is not.

    The authoritative battery source is the device's own status reply -- see
    :func:`OpenMuse.get_status` and the ``battery_interval`` argument of
    :func:`OpenMuse.record`, which samples it periodically so a whole-night
    drain curve falls out of the normal decode.

    Returns: np.ndarray shape (1,) with battery percentage, or NaN
    """
    if len(data_bytes) < 2:
        return None

    raw_soc = struct.unpack("<H", data_bytes[0:2])[0]
    battery_percent = raw_soc / 256.0

    if not (0.0 <= battery_percent <= 100.0):
        battery_percent = float("nan")

    return np.array([battery_percent], dtype=np.float32)


def _bytes_to_bits(data_bytes: bytes, n_bytes: int) -> List[int]:
    """
    Convert bytes to bit array (LSB first).

    Parameters:
    -----------
    data_bytes : bytes
        Input bytes to convert
    n_bytes : int
        Number of bytes to process

    Returns:
    --------
    list[int] : List of bits (0 or 1), LSB first
    """
    bits = []
    for byte in data_bytes[:n_bytes]:
        for bit_pos in range(8):
            bits.append((byte >> bit_pos) & 1)
    return bits


def _extract_packed_int(bits: List[int], bit_start: int, bit_width: int) -> int:
    """
    Extract an integer from a bit array.

    Parameters:
    -----------
    bits : list[int]
        Bit array (0 or 1 values)
    bit_start : int
        Starting bit position
    bit_width : int
        Number of bits to extract

    Returns:
    --------
    int : Extracted integer value
    """
    int_value = 0
    for bit_idx in range(bit_width):
        if bits[bit_start + bit_idx]:
            int_value |= 1 << bit_idx
    return int_value


def _decode_eeg_data(data_bytes: bytes, n_channels: int) -> Optional[np.ndarray]:
    """
    Decode EEG data (14-bit packed values).

    EEG4: 4 samples × 4 channels = 28 bytes
    EEG8: 2 samples × 8 channels = 28 bytes

    Returns: np.ndarray shape (n_samples, n_channels)
    """
    if len(data_bytes) < 28:
        return None

    # Determine samples per packet
    n_samples = 4 if n_channels == 4 else 2

    # Convert bytes to bit array (LSB first)
    bits = _bytes_to_bits(data_bytes, 28)

    # Parse 14-bit values
    data = np.zeros((n_samples, n_channels), dtype=np.float32)

    for sample_idx in range(n_samples):
        for channel_idx in range(n_channels):
            bit_start = (sample_idx * n_channels + channel_idx) * 14

            # Extract 14 bits and scale to microvolts
            int_value = _extract_packed_int(bits, bit_start, 14)
            data[sample_idx, channel_idx] = int_value * EEG_SCALE

    return data


def _decode_optics_data(data_bytes: bytes, n_channels: int) -> Optional[np.ndarray]:
    """
    Decode Optics data (20-bit packed values).

    Sensor Positioning:
    -------------------
    Sensors are positioned as: Left Outer (LO) → Left Inner (LI) → Right Inner (RI) → Right Outer (RO)

    Signal Types:
    -------------
    - NIR: 730nm wavelength (near-infrared)
    - IR: 850nm wavelength (infrared)
    - RED: Red wavelength
    - AMB: Ambient light

    Channel Configurations:
    -----------------------
    Optics4:  3 samples × 4 channels = 30 bytes
              Channels: LI_NIR, RI_NIR, LI_IR, RI_IR (inner sensors only)

    Optics8:  2 samples × 8 channels = 40 bytes
              Channels: LO_NIR, RO_NIR, LO_IR, RO_IR, LI_NIR, RI_NIR, LI_IR, RI_IR

    Optics16: 1 sample × 16 channels = 40 bytes
              All 4 sensors × 4 wavelengths, transmitted **wavelength-major**:
              RED×4, AMB×4, NIR×4, IR×4, each in LI, RI, LO, RO order.
              See OPTICS16_CHANNELS for the evidence behind that order.

    Returns: np.ndarray shape (n_samples, n_channels)
    """
    # Determine samples per packet and required bytes
    if n_channels == 4:
        n_samples = 3
        bytes_needed = 30
    elif n_channels == 8:
        n_samples = 2
        bytes_needed = 40
    elif n_channels == 16:
        n_samples = 1
        bytes_needed = 40
    else:
        return None

    if len(data_bytes) < bytes_needed:
        return None

    # Convert bytes to bit array (LSB first)
    bits = _bytes_to_bits(data_bytes, bytes_needed)

    # Parse 20-bit values
    data = np.zeros((n_samples, n_channels), dtype=np.float32)

    for sample_idx in range(n_samples):
        for channel_idx in range(n_channels):
            bit_start = (sample_idx * n_channels + channel_idx) * 20

            # Extract 20 bits and scale
            int_value = _extract_packed_int(bits, bit_start, 20)
            data[sample_idx, channel_idx] = int_value * OPTICS_SCALE

    return data


# ============================================================================
# Timestamp Resampling Functions
# ============================================================================
# Convenience Functions
# ============================================================================


@dataclass
class StreamBuild:
    """Everything :func:`_build_stream` recovered from one sensor stream.

    ``times`` is the historical device-clock reconstruction (relative, uniform).
    ``sample_index`` and ``arrival`` are what an absolute, *measured* timebase
    needs: the monotonic sample number of every row, and the wall-clock instant
    at which the packet carrying it reached the host.
    """

    data: np.ndarray
    times: np.ndarray
    sample_index: np.ndarray
    arrival: np.ndarray
    n_skipped: int
    n_padded: int
    state: Tuple[float, int, int, int]


def _to_unix(value: Any) -> float:
    """Convert a parsed ISO-8601 message stamp to unix seconds."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return float("nan")


def _build_stream(
    subpackets: List[Dict],
    base_time: Optional[float] = None,
    wrap_offset: int = 0,
    last_abs_tick: int = 0,
    sample_counter: int = 0,
) -> Optional[StreamBuild]:
    """Assemble one sensor stream from its subpackets.

    Resilient by construction: a subpacket whose decoded shape does not match
    the stream's is repaired or dropped, never raised.  Previously a single
    mixed-width packet raised ``ValueError: could not broadcast input array
    from shape (4,4) into shape (4,8)`` out of :func:`decode_rawdata` and lost
    the entire file -- on one 25-minute capture (132,943 packets) that
    discarded 386,092 recoverable EEG rows over 2 bad subpackets.

    The mismatch is real, not corruption: TAG 0x11 (EEG4) and TAG 0x12 (EEG8)
    both have sensor type "EEG" and are therefore pooled into one stream, as do
    the three OPTICS layouts.  The stream adopts whichever width accounts for
    the most samples -- taking it from the first subpacket instead would let a
    handful of stray packets discard the majority of the file.

    A minority subpacket is then NaN-padded when the narrow layout is a genuine
    prefix of the wide one (EEG only: channels 0-3 are TP9/AF7/AF8/TP10 in both
    layouts, so the samples survive and only the absent AUX channels become
    NaN).  It is dropped otherwise -- notably for OPTICS, where Optics4 is a
    different sensor selection rather than the first four channels of Optics16,
    so padding would file real samples under the wrong channel names.
    """
    if len(subpackets) == 0:
        return None

    # Get sensor configuration from first subpacket
    first_tag = subpackets[0].get("tag_byte")
    config = SENSORS.get(first_tag, {}) if first_tag else {}
    sampling_rate = config.get("rate", 1.0) or 1.0
    n_samples_per_subpkt = config.get("n_samples", 1)

    # Sort all subpackets globally by device timestamp and sequence indices
    sorted_subpackets = sorted(
        subpackets,
        key=lambda s: (
            s.get("pkt_time_raw", 0),
            s["pkt_index"],
            s["subpkt_index"] if s["subpkt_index"] is not None else -1,
        ),
    )

    # Pre-calculate an upper bound on rows so the arrays can be pre-allocated;
    # skipped subpackets simply leave the tail unused and it is trimmed below.
    total_samples = sum(
        subpkt.get("n_samples", n_samples_per_subpkt) for subpkt in sorted_subpackets
    )

    # Normalise 1D arrays (Battery data is 1D) to 2D.
    for subpkt in sorted_subpackets:
        if subpkt["data"].ndim == 1:
            subpkt["data"] = subpkt["data"].reshape(-1, 1)

    # The stream's width is the layout carrying the most samples, so a few
    # stray packets of another layout cannot displace the bulk of the file.
    width_samples: Dict[int, int] = {}
    for subpkt in sorted_subpackets:
        data = subpkt["data"]
        if data.ndim != 2:
            continue
        width_samples[data.shape[1]] = width_samples.get(data.shape[1], 0) + data.shape[0]
    if not width_samples:
        return None
    n_channels = max(width_samples, key=lambda w: (width_samples[w], w))

    # Only EEG's narrow layout is a true prefix of its wide one, so only EEG
    # subpackets may be NaN-padded up to the stream width.
    prefix_compatible = config.get("type") == "EEG"

    data_array = np.empty((total_samples, n_channels), dtype=np.float32)
    times_array = np.empty(total_samples, dtype=np.float64)
    index_array = np.empty(total_samples, dtype=np.int64)
    arrival_array = np.empty(total_samples, dtype=np.float64)

    clock_mod = 1 << 32
    first_raw_tick = sorted_subpackets[0]["pkt_time_raw"]

    # Initialize rolling state for this sensor stream if not provided
    if base_time is None:
        # For streaming applications, use 0 as base_time to make timestamps
        # relative to when streaming started, not when the device booted.
        base_time = 0.0
        wrap_offset = 0
        last_abs_tick = first_raw_tick
        sample_counter = 0

    # Track base tick for precision (avoid accumulating floating point error)
    base_tick = first_raw_tick

    row_idx = 0
    n_skipped = 0
    n_padded = 0

    for subpkt in sorted_subpackets:
        data = subpkt["data"]

        # --- shape reconciliation (never raise on one bad subpacket) --------
        if data.ndim != 2:
            n_skipped += 1
            continue
        n_rows, width = data.shape
        if width != n_channels:
            if width < n_channels and prefix_compatible:
                padded = np.full((n_rows, n_channels), np.nan, dtype=np.float32)
                padded[:, :width] = data
                data = padded
                n_padded += 1
            else:
                n_skipped += 1
                continue
        if row_idx + n_rows > total_samples:
            n_skipped += 1
            continue

        raw_tick = subpkt.get("pkt_time_raw", first_raw_tick)

        # Expand 32-bit tick into monotonically increasing absolute tick
        prev_raw_tick = last_abs_tick % clock_mod

        # If raw_tick < prev_raw_tick there are two possibilities:
        #  - a true wraparound (prev_raw_tick - raw_tick is large, near clock_mod)
        #  - a small inversion / reordering (prev_raw_tick - raw_tick is small)
        if raw_tick < prev_raw_tick:
            if (prev_raw_tick - raw_tick) > (clock_mod // 2):
                # treat as true wraparound - increment offset once
                wrap_offset += clock_mod
                abs_tick = raw_tick + wrap_offset
            else:
                # small inversion / jitter - force monotonicity
                abs_tick = last_abs_tick
        else:
            abs_tick = raw_tick + wrap_offset
            # defensive check: force monotonicity
            if abs_tick < last_abs_tick:
                abs_tick = last_abs_tick

        last_abs_tick = abs_tick

        # Use high-precision arithmetic to avoid floating point accumulation
        tick_diff = abs_tick - base_tick
        time_diff = tick_diff / DEVICE_CLOCK_HZ

        relative_samples = int(round(time_diff * sampling_rate))

        # Maintain monotonic sample indices. This handles hardware timestamp
        # limitations where multiple subpackets share identical timestamps.
        if relative_samples < sample_counter:
            relative_samples = sample_counter

        offsets = np.arange(n_rows)
        indices = relative_samples + offsets

        data_array[row_idx : row_idx + n_rows] = data
        times_array[row_idx : row_idx + n_rows] = base_time + indices / sampling_rate
        index_array[row_idx : row_idx + n_rows] = indices
        arrival_array[row_idx : row_idx + n_rows] = _to_unix(subpkt.get("message_time"))

        sample_counter = relative_samples + n_rows
        row_idx += n_rows

    if n_skipped or n_padded:
        warnings.warn(
            f"decode: {n_skipped} malformed subpacket(s) dropped and "
            f"{n_padded} narrower subpacket(s) NaN-padded while assembling a "
            f"{n_channels}-channel stream ({row_idx} samples recovered).",
            RuntimeWarning,
            stacklevel=3,
        )

    return StreamBuild(
        data=data_array[:row_idx],
        times=times_array[:row_idx],
        sample_index=index_array[:row_idx],
        arrival=arrival_array[:row_idx],
        n_skipped=n_skipped,
        n_padded=n_padded,
        state=(base_time, wrap_offset, last_abs_tick, sample_counter),
    )


def make_timestamps(
    subpackets: List[Dict],
    base_time: Optional[float] = None,
    wrap_offset: int = 0,
    last_abs_tick: int = 0,
    sample_counter: int = 0,
) -> tuple[np.ndarray, float, int, int, int]:
    """
    Add timestamps to a list of subpackets for a single sensor type and return as numpy array.

    .. warning::
       The returned ``time`` column is a **reconstruction**, not a measurement.
       It is generated as ``base_time + sample_index / nominal_rate`` from the
       rate declared in :data:`SENSORS`; the hardware timestamps are used for
       ordering only.  The result is therefore a perfectly uniform ramp that
       does not correspond to when samples arrived, it restarts at 0 on every
       call, and BLE dropouts are absorbed into it instead of appearing as
       gaps.  On real hardware the nominal rate is ~0.14% fast, which is ~40 s
       of drift over an 8 h night.

       Use ``decode_rawdata(..., timebase="absolute")`` for a timebase that is
       anchored to wall-clock arrival times, measures the true sampling rate
       and represents dropouts as gaps.

    This implements global timestamping: all subpackets are sorted by device time before
    applying wraparound detection and timestamp generation. This prevents artificial gaps
    that occur when per-message timestamping triggers false wraparound detection due to
    timing inversions between messages.

    Global Timestamping Logic:
    --------------------------
    1. **Global Sorting**: Sort ALL subpackets by (pkt_time_raw, pkt_index, subpkt_index)
       - Ensures correct temporal order across message boundaries
       - Handles BLE packet reordering and timing jitter

    2. **Unified Wraparound Detection**: Apply 32-bit clock wraparound logic once across
       all subpackets, preventing false detections from cross-message inversions

    3. **Stream-Relative Timestamp Generation**: Generate timestamps relative to stream start
       (base_time = 0.0), ensuring uniform spacing at declared sampling rates while
       maintaining device timing precision through tick-based calculations

    4. **State Management**: For streaming, maintain state across calls to handle ongoing
       data streams with correct wraparound detection

    Parameters:
    -----------
    subpackets : List[Dict]
        List of subpacket dicts for a single sensor type
    base_time : float, optional
        Base time for timestamp calculation. If None, defaults to 0.0 for streaming
        applications (relative to stream start) instead of device boot time.
    wrap_offset : int
        Current wraparound offset for 32-bit clock
    last_abs_tick : int
        Last absolute tick value for wraparound detection
    sample_counter : int
        Current sample counter for monotonic timestamping

    Returns:
    --------
    tuple : (array, base_time, wrap_offset, last_abs_tick, sample_counter)
        array : np.ndarray with shape (total_samples, n_channels + 1)
            First column is timestamp, remaining columns are sensor data
        base_time, wrap_offset, last_abs_tick, sample_counter : Updated state values
    """
    build = _build_stream(
        subpackets, base_time, wrap_offset, last_abs_tick, sample_counter
    )
    if build is None or build.data.size == 0:
        if base_time is None:
            base_time = 0.0
        if build is not None:
            base_time, wrap_offset, last_abs_tick, sample_counter = build.state
        return np.empty((0, 0)), base_time, wrap_offset, last_abs_tick, sample_counter

    times_col = build.times.reshape(-1, 1).astype(np.float64)
    return (np.hstack((times_col, build.data)), *build.state)


def _absolute_timebase(
    sample_index: np.ndarray,
    arrival: np.ndarray,
    nominal_rate: float,
    gap_tolerance: float = 1.0,
) -> Tuple[np.ndarray, float, int]:
    """Build an absolute, measured timebase from packet arrival stamps.

    Every line of a raw capture begins with an absolute ISO-8601 timestamp, but
    ``make_timestamps`` throws it away and emits ``sample_index / nominal_rate``
    instead.  That output is smooth and fictional: on real hardware it runs
    0.139% fast (~40 s over an 8 h night) and BLE dropouts vanish into it.

    Here each packet's arrival stamp is instead treated as an **anchor** on the
    sample index.  The stream is split wherever the wall clock advanced by more
    than the sample count can explain (a real dropout, ``gap_tolerance``
    seconds of slack), and within each segment ``time = a + b * index`` is
    fitted by least squares.  The result is monotonic by construction, pinned
    to arrival time, and it *measures* the sampling rate as ``1 / b`` instead of
    assuming it.  Measured this way on a reference capture: EEG 255.62 Hz
    (nominal 256), OPTICS 64.15 Hz (nominal 64), ACCGYRO 52.27 Hz (nominal 52).

    Returns ``(times, fs_measured, n_segments)``.  ``fs_measured`` is NaN when
    no usable arrival information was available, in which case ``times`` falls
    back to the nominal ramp.
    """
    n = len(sample_index)
    if n == 0:
        return np.empty(0, dtype=np.float64), float("nan"), 0

    idx = sample_index.astype(np.float64)
    fallback = idx / nominal_rate

    finite = np.isfinite(arrival)
    if not finite.any():
        return fallback, float("nan"), 0

    # Anchor rows: the first sample of every distinct arrival stamp.
    change = np.empty(n, dtype=bool)
    change[0] = True
    np.not_equal(arrival[1:], arrival[:-1], out=change[1:])
    anchor_rows = np.flatnonzero(change & finite)
    if len(anchor_rows) == 0:
        return fallback, float("nan"), 0

    a_idx = idx[anchor_rows]
    a_arr = arrival[anchor_rows]

    # Split where the wall clock advanced more than the sample count allows.
    expected = np.diff(a_idx) / nominal_rate
    observed = np.diff(a_arr)
    breaks = np.flatnonzero(observed - expected > gap_tolerance) + 1
    seg_bounds = [0, *breaks.tolist(), len(anchor_rows)]

    times = np.empty(n, dtype=np.float64)
    slope_lo, slope_hi = 0.5 / nominal_rate, 2.0 / nominal_rate
    total_weight = 0.0
    weighted_slope = 0.0
    prev_end = -np.inf
    n_segments = 0

    for s, e in zip(seg_bounds[:-1], seg_bounds[1:]):
        if e <= s:
            continue
        n_segments += 1
        row_lo = anchor_rows[s]
        row_hi = anchor_rows[e] if e < len(anchor_rows) else n

        seg_idx = a_idx[s:e]
        seg_arr = a_arr[s:e]

        slope = 1.0 / nominal_rate
        if len(seg_idx) >= 2 and seg_idx[-1] > seg_idx[0]:
            try:
                fit_slope, fit_intercept = np.polyfit(seg_idx, seg_arr, 1)
            except (np.linalg.LinAlgError, ValueError):
                fit_slope = float("nan")
                fit_intercept = float("nan")
            if np.isfinite(fit_slope) and slope_lo <= fit_slope <= slope_hi:
                slope = float(fit_slope)
                intercept = float(fit_intercept)
            else:
                intercept = float(seg_arr.mean() - slope * seg_idx.mean())
        else:
            intercept = float(seg_arr[0] - slope * seg_idx[0])

        seg_times = intercept + slope * idx[row_lo:row_hi]

        # Segments are ordered, but never let a fit push one back over its
        # predecessor: the timebase must stay monotonic across the whole file.
        if seg_times.size and seg_times[0] <= prev_end:
            seg_times = seg_times + (prev_end - seg_times[0] + slope)
        times[row_lo:row_hi] = seg_times
        if seg_times.size:
            prev_end = float(seg_times[-1])

        weight = float(row_hi - row_lo)
        weighted_slope += slope * weight
        total_weight += weight

    fs_measured = total_weight / weighted_slope if weighted_slope > 0 else float("nan")
    return times, fs_measured, n_segments


def _status_battery_rows(messages: List[str]) -> List[Dict[str, Any]]:
    """Reassemble control-channel status replies into battery readings.

    Returns one dict per ``s`` reply that carried a battery field, with keys
    ``time`` (unix seconds, from the raw line's own ISO stamp), ``bp``
    (percent) and ``ps`` (pack millivolts, or NaN).
    """
    control = ControlChannel()
    rows: List[Dict[str, Any]] = []

    for message in messages:
        try:
            ts, uuid, hexstring = message.strip().split("\t", 2)
        except (ValueError, AttributeError):
            continue
        if not is_control_uuid(uuid):
            continue
        try:
            payload = bytes.fromhex(hexstring.strip())
            when = _to_unix(datetime.fromisoformat(ts.replace("Z", "+00:00")))
        except ValueError:
            continue

        for reply in control.feed(payload):
            if "bp" not in reply:
                continue
            try:
                percent = float(reply["bp"])
            except (TypeError, ValueError):
                continue
            if not (0.0 <= percent <= 100.0):
                continue
            millivolts = reply.get("ps")
            rows.append(
                {
                    "time": when,
                    "bp": percent,
                    "ps": float(millivolts)
                    if isinstance(millivolts, (int, float))
                    else float("nan"),
                }
            )

    return rows


def decode_rawdata(
    messages: List[str],
    timebase: str = "relative",
    return_info: bool = False,
) -> Union[Dict[str, pd.DataFrame], Tuple[Dict[str, pd.DataFrame], Dict[str, Any]]]:
    """
    Parse multiple messages and return organized data as Pandas DataFrames.

    This is the high-level convenience function that:
    1. Parses all messages (with timestamps)
    2. Combines data from all messages
    3. Converts to Pandas DataFrames

    Parameters:
    -----------
    messages : List[str]
        List of BLE message strings (one per line)
    timebase : {"relative", "absolute"}
        How the ``time`` column is produced.

        ``"relative"`` (default, unchanged behaviour) reconstructs it as
        ``sample_index / nominal_rate`` starting at 0.  **It restarts at 0 on
        every call**, so decoding one file in chunks stacks every chunk on top
        of the last unless the caller re-derives absolute time itself; it uses
        the nominal rate, which is ~0.14% fast on real hardware (~40 s of drift
        over an 8 h night); and BLE dropouts are absorbed into it rather than
        appearing as gaps.

        ``"absolute"`` anchors the timebase to the ISO-8601 arrival stamp that
        begins every line of the raw file, splits the stream at real dropouts,
        and least-squares fits ``time = a + b * index`` per segment.  The
        result is unix seconds, is comparable across chunks and files, and
        *measures* the sampling rate instead of assuming it.  Read
        ``info["streams"][name]["fs_measured"]``; never assume the nominal rate.
    return_info : bool
        Also return a dict describing what the decode actually did: measured
        sampling rate, number of continuous segments (>1 means dropouts),
        subpackets skipped or NaN-padded, and the first/last timestamp.

    Returns:
    --------
    Dict[str, pd.DataFrame] : Dictionary mapping sensor type to DataFrame
        Keys: "EEG", "ACCGYRO", "OPTICS", "BATTERY", "Unknown"
        Each DataFrame has columns: [time, channel_1, channel_2, ...]

        If the capture contains control-channel status replies (recorded by
        ``record(..., battery_interval=...)``), the BATTERY frame additionally
        carries ``battery_millivolts`` and a ``source`` column marking each row
        as ``"packet"`` or the authoritative ``"status"``.

    If ``return_info`` is true, returns ``(frames, info)`` instead.

    Example:
    --------
    >>> messages = load_messages("data.txt")
    >>> data = decode_rawdata(messages)
    >>> print(data["EEG"].shape)
    (1440, 5)  # 1440 samples x (time + 4 channels)
    >>> print(data["EEG"].columns)
    ['time', 'EEG_TP9', 'EEG_AF7', 'EEG_AF8', 'EEG_TP10']
    >>> print(data["ACCGYRO"].columns)
    ['time', 'ACC_X', 'ACC_Y', 'ACC_Z', 'GYRO_X', 'GYRO_Y', 'GYRO_Z']
    >>> print(data["OPTICS"].columns)  # For Optics4
    ['time', 'OPTICS_LI_NIR', 'OPTICS_RI_NIR', 'OPTICS_LI_IR', 'OPTICS_RI_IR']

    >>> frames, info = decode_rawdata(messages, timebase="absolute", return_info=True)
    >>> info["streams"]["EEG"]["fs_measured"]
    255.62...
    """
    if timebase not in ("relative", "absolute"):
        raise ValueError(
            f"timebase must be 'relative' or 'absolute', got {timebase!r}"
        )

    # Collect all subpackets from all messages
    all_subpackets = {
        key: [] for key in ["EEG", "ACCGYRO", "OPTICS", "BATTERY", "Unknown"]
    }

    for message in messages:
        parsed = parse_message(message)
        for sensor_type, subpacket_list in parsed.items():
            all_subpackets[sensor_type].extend(subpacket_list)

    status_rows = _status_battery_rows(messages)

    info: Dict[str, Any] = {
        "timebase": timebase,
        "n_messages": len(messages),
        "n_status_replies": len(status_rows),
        "streams": {},
    }

    result: Dict[str, pd.DataFrame] = {}

    for sensor_type, subpackets in all_subpackets.items():
        build = _build_stream(subpackets)
        if build is None or build.data.size == 0:
            result[sensor_type] = pd.DataFrame()
            continue

        first_tag = subpackets[0].get("tag_byte")
        nominal_rate = SENSORS.get(first_tag, {}).get("rate", 1.0) or 1.0

        times, fs_measured, n_segments = _absolute_timebase(
            build.sample_index, build.arrival, nominal_rate
        )
        if timebase == "relative":
            times = build.times

        info["streams"][sensor_type] = {
            "n_samples": int(build.data.shape[0]),
            "n_channels": int(build.data.shape[1]),
            "fs_nominal": float(nominal_rate),
            "fs_measured": float(fs_measured),
            "n_segments": int(n_segments),
            "n_skipped": int(build.n_skipped),
            "n_padded": int(build.n_padded),
            "t_start": float(times[0]) if len(times) else float("nan"),
            "t_end": float(times[-1]) if len(times) else float("nan"),
        }

        n_channels = build.data.shape[1]
        if sensor_type == "EEG":
            columns = select_eeg_channels(n_channels)
        elif sensor_type == "ACCGYRO":
            columns = list(ACCGYRO_CHANNELS[:n_channels])
        elif sensor_type == "OPTICS":
            columns = select_optics_channels(n_channels)
        elif sensor_type == "BATTERY":
            columns = ["battery_percent"]
        else:
            columns = [f"ch_{i+1}" for i in range(n_channels)]

        frame = pd.DataFrame(build.data, columns=columns)
        frame.insert(0, "time", times)
        result[sensor_type] = frame

    if status_rows:
        result["BATTERY"] = _merge_status_battery(
            result.get("BATTERY"), status_rows, timebase
        )

    if return_info:
        return result, info
    return result


def _merge_status_battery(
    packet_frame: Optional[pd.DataFrame],
    status_rows: List[Dict[str, Any]],
    timebase: str,
) -> pd.DataFrame:
    """Fold device-reported battery readings into the BATTERY frame.

    The device answers the ``s`` command with its own state of charge (``bp``)
    and pack voltage (``ps``); that is authoritative, unlike the packet field,
    which is a guess at undocumented bytes (see :func:`_decode_battery_data`).
    Rows are tagged so a caller can prefer one source over the other, and the
    two extra columns only appear when status replies were actually present, so
    captures made without them decode exactly as before.

    With ``timebase="relative"`` both sources are expressed as seconds from
    their own first reading, which only lines the two up approximately.  Use
    ``timebase="absolute"`` when the drain curve has to be aligned with the
    signal.
    """
    status = pd.DataFrame(status_rows).rename(
        columns={"bp": "battery_percent", "ps": "battery_millivolts"}
    )
    status["source"] = "status"

    if timebase == "relative":
        status["time"] = status["time"] - status["time"].min()

    if packet_frame is None or len(packet_frame) == 0:
        merged = status
    else:
        packet_frame = packet_frame.copy()
        packet_frame["battery_millivolts"] = np.nan
        packet_frame["source"] = "packet"
        merged = pd.concat([packet_frame, status], ignore_index=True)

    merged = merged.sort_values("time", kind="stable").reset_index(drop=True)
    return merged[["time", "battery_percent", "battery_millivolts", "source"]]
