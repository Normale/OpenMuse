# Muse S Athena protocol — what we still do not decode

Everything this package receives but does not interpret, with whatever evidence
we have for each. Written so someone can pick an item up and work on it without
re-deriving the ground truth.

**Measurement base for all counts below** unless stated otherwise: one capture
from a Muse S Athena, firmware **3.1.23**, hw **01.1**, `Athena_RevE`, preset
**p1035**, recorded over BLE by a Raspberry Pi — first 40,000 lines, giving
**39,828 packets** and **70,415 subpackets**. Cross-checks come from the
captures committed in `tests/test_data/`.

---

## 1. TAG `0x53` — an entire undecoded stream

**Status:** `SENSORS[0x53] = {"type": "Unknown", "n_channels": 0,
"n_samples": 0, "rate": 0.0, "data_len": 24}`. Parsed, then discarded by
`_decode_subpacket_data` because the type is `"Unknown"`.

This is not rare. In the sample it is the **fourth most common subpacket**:

| TAG | subpackets | meaning |
| --- | --- | --- |
| `0x11` | 65,327 | EEG4 |
| `0x34` | 21,778 | OPTICS4 |
| `0x47` | 17,766 | ACCGYRO |
| **`0x53`** | **5,079** | **unknown** |
| `0x98` | 2 | battery |

It also appears as a *primary* packet type (1,367 packets), so it is a stream in
its own right, not a trailer.

**Payload is always exactly 24 bytes.** Sample payloads:

```
ff1f6a20fd1f6920fd1f6920fd1f6a20fd1f6920fd1f6920
fd1f6a20022069200120692001206a2001206920ff1f6a20
01206a2001206920fe1f6920ff1f6920fe1f6920fc1f6920
ff1f6920ff1f6a20fd1f6a20fe1f6a2000206a20ff1f6920
```

**Read as 12 × uint16 little-endian, it resolves cleanly into two interleaved
channels:**

| | values seen | as uint16 |
| --- | --- | --- |
| channel A | `fd1f`, `fe1f`, `ff1f`, `0020`, `0120` | 8189–8193 |
| channel B | `6820`, `6920`, `6a20` | 8296–8298 |

Both sit within a few counts of **8192 = 2^13**, the midpoint of the 14-bit ADC
used for EEG. Two quiet channels parked at mid-scale with ±3 counts of noise is
the signature of **reference / bias electrodes (REF and DRL)**, which the Muse
has in addition to the four EEG channels — though this is inference from the
numbers, not confirmed.

The strict alternation between two distinct levels ~107 counts apart rules out a
single channel sampled at double rate.

**Implied rate.** In the same file, `0x11` contributed 65,327 × 4 = 261,308 EEG
samples, i.e. 1,021 s at 256 Hz. Over that span `0x53` contributed 5,079 × 6 =
30,474 samples per channel → **≈ 30 Hz**.

**Why it matters.** There is no contact-quality signal at all on the BLE path —
`horseshoe` is populated only on the Mind Monitor OSC path, and is NaN in every
BLE session we have recorded since 2026-07-23. If `0x53` carries REF/DRL levels,
it is the natural place to look for electrode-contact information, which is
currently missing entirely from overnight recordings.

**To do:** confirm the channel identity (record with one electrode deliberately
lifted and see which value moves), fix the sample count and rate, and decide
whether it belongs in its own DataFrame or in `quality`.

---

## 2. `pkt_unknown1` — packet header bytes 6:9

**Constant.** One distinct value, `8c0100`, across all 39,828 packets.

Not a counter, not a timestamp, not data. Most likely a protocol or format
version, or a fixed device-class marker. Note `0x018c = 396` if read as a LE
uint16 followed by a zero byte.

**To do:** check whether it differs across firmware versions, presets, or after
`v7` (see §7). If it tracks the protocol version, it is worth surfacing.

---

## 3. `pkt_unknown2` (header bytes 10:13) and `subpkt_unknown` (subpacket header bytes 2:5)

**Both are near-unique per packet**, which is the interesting part:

| field | distinct values | occurrences |
| --- | --- | --- |
| `pkt_unknown2` | 39,729 | 39,828 |
| `subpkt_unknown` | 66,553 | 70,415 |

A 3-byte field that takes a different value on essentially every packet is not
metadata — that is the profile of a **checksum or CRC**. The handful of repeats
is what you would expect from collisions in a 24-bit space at these counts.

**To do:** test the CRC hypothesis directly (CRC-24, or a truncated CRC-32, over
the packet body). If it validates, the decoder gains real integrity checking
instead of inferring corruption from shape mismatches — which is currently the
only way `_build_stream` notices a bad subpacket at all.

---

## 4. `byte_13` — packet header byte 13

Parsed into a local and then dropped. The existing comment records that it is
**not** always 0 on newer firmware, and that a validity check against 0 was
removed for compatibility. Its actual meaning is unrecorded.

**To do:** tabulate it against `pkt_id` and `subpkt_index`.

---

## 5. `subpkt_index` — understood, but thrown away

This one is *decoded*, documented as a per-sensor-type sequence counter, used for
ordering, and then discarded. It is more useful than that: the step distribution
between consecutive subpackets of the same TAG shows real gaps.

| TAG | n | step +1 | +2 | +3 | +4 |
| --- | --- | --- | --- | --- | --- |
| `0x11` EEG4 | 47,564 | 30,447 | 15,971 | 1,033 | 100 |
| `0x34` OPTICS4 | 11,860 | 6,388 | 2,972 | 1,341 | 578 |
| `0x47` ACCGYRO | 7,277 | 2,632 | 1,957 | 1,206 | 703 |
| `0x53` | 3,712 | 2,667 | 752 | 200 | 59 |

Every step > 1 is subpackets the device sent and the host never received.

**To do:** expose loss per stream from the counter directly. Today
`decode_rawdata(..., timebase="absolute")` infers loss from *timing* gaps, which
cannot see a dropout shorter than one sample interval and cannot distinguish
"never sent" from "sent and lost". The sequence counter can, exactly.

---

## 6. Battery packet framing (`0x88`, `0x98`)

Both declare a `data_len` that does not match reality:

- `0x98` declares **20** bytes; observed packets carry ~**196–205** bytes of
  payload (total packet length 210–219).
- `0x88` declares **188**; the comment concedes actual packets run 188–230, and
  the decoder reads only the first 2 bytes.

Because the tail is then re-scanned for TAG bytes, a byte inside the battery
payload can be mistaken for a subpacket TAG. That is the mechanism behind the
impossible percentages in issue #28 — the value is an arbitrary `uint16 / 256`,
above 100 about 61% of the time. Values of 105.0 and 133.0 were the *only* two
battery rows recovered from two 30-minute captures on this device.

The rest of the payload — ~190 bytes on every battery packet — is entirely
undecoded. The `0x88` comment speculates "possibly processed signals".

**To do:** determine the true frame length for both TAGs so the trailing scan
starts in the right place, then characterise the remaining ~190 bytes.

---

## 7. Control-channel commands

Known: `h` (halt), `dc001` (start streaming), `p<NNNN>` (preset), `s` (status),
`v6`/`v7` (version), `L1`/`L0` (low latency on/off).

Open questions:

- **`v6` vs `v7`.** The vendor app sends `v7`; this package sends `v6`. The
  status reply reports `pv:7` and `pv:4` respectively. Whether `pv` selects a
  different packet encoding is **unknown and untested** — which is why `v7` is
  available but not the default (`initialize_device(version_command=...)`).
- **What `L1`/`L0` actually do.** Not the BLE connection interval: `btmon` shows
  the link pinned at **15 ms with no Connection Update after `L0`**, and the
  vendor app's own session sits at 15 ms too. So the documented rationale for
  low-latency mode is wrong on this firmware, and the real effect is unidentified.
- **The preset space is unmapped.** Fifteen presets are characterised in
  `OpenMuse.PRESETS` by decoding committed captures, but that table came from
  guessed strings. There is no discovery mechanism, and presets sharing a
  composition (p20/p21, p50/p51, p60/p61, p1041/p1042) differ in something not
  observable in those captures — the EEG in all of them is rail-to-rail, so no
  gain or filter difference can be read out either.

---

## 8. Status reply fields

The reply to `s`, verbatim:

```json
{"hn":"MuseS-1E2B","sn":"7011-AH29-1E2B","ma":"00-55-da-ba-1e-2b",
 "hs":"DC23-TVDT-1426","id":"0","bp":92.98,"ps":4161,"ln":"0",
 "ch":[0,0],"rc":0}
```

and to `v7`:

```json
{"fw":"3.1.23","bn":1,"tp":"consumer","hw":"01.1","pv":7,"ap":"headset",
 "sp":"Athena_RevE","hb":"Athena_RevF_NW","bl":"1.0.1","be":"1.5.1","rc":0}
```

Understood: `hn` host name, `sn` serial, `ma` MAC, `bp` battery percent, `fw`
firmware, `hw` hardware, `sp`/`hb` board revisions, `rc` return code.

Unknown: **`ch`** (always `[0,0]` here — a channel or contact status is the
obvious guess, and would be valuable given §1), **`ln`**, **`id`**, **`hs`**,
**`bn`**, **`tp`**, **`ap`**, **`bl`**, **`be`**, **`pv`**.

**`ps` is not trustworthy.** Documented as pack millivolts, and it does read
4161 mV consistently while discharging — but it also reports **33** in other
replies from the same device minutes apart. Either the unit is context-dependent
or the field is overloaded. Only `bp` should be relied on.

---

## 9. Optics channel identity below 16 channels

`OPTICS16_CHANNELS` is now wavelength-major with evidence (see README and
`tests/test_optics_mapping.py`). The narrower layouts are **not** verified:

- **Optics8** (`0x35`): measured on `data_p1034.txt`, the eight channels fall
  into two DC groups — {0,1,4,5} high (~8.2–9.4), {2,3,6,7} low (~1.8–2.1) —
  consistent with the labelling, but the wavelength assignment is assumed.
- **Optics4** (`0x34`): all four carry a clear pulse (cardiac SNR 67–81 on
  `data_p1035.txt`), so none is ambient; beyond that the identity is assumed.

**To do:** the ambient-light experiment that settled Optics16 (cover the sensors,
then illuminate them, and see which channels track room light) applied to the
4- and 8-channel presets.
