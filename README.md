# OpenMuse

*The Open Python Muse S Athena EEG Decoder*

[![CI](https://github.com/DominiqueMakowski/OpenMuse/actions/workflows/ci.yml/badge.svg)](https://github.com/DominiqueMakowski/OpenMuse/actions/workflows/ci.yml)
[![Muse S Athena](media/device.webp)](https://eu.choosemuse.com/products/muse-s-athena)


This software allows recording, streaming via LSL and visualizing signals from the **Muse S Athena** headband.

> [!WARNING]  
> OpenMuse is NOT an official product of InteraXon Inc and is not affiliated with or endorsed by the company. It does not come with any warranty and should be considered an experimental software developed for research purposes only.

## Roadmap

- [x] Record raw data to a file
- [x] Decode raw data into a pandas DataFrame
- [x] Stream data over LSL
- [x] Visualise live LSL streams
- [x] Stream in battery signal and display battery level in visualizer
- [ ] Consider adding a visualization of the PSD of each signal
- [ ] Validate timestamping accuracy experimentally: confirm we get time-locked ERPs
- [ ] Validate the best PPG signal extraction method
- [ ] Validate fNIRS signal extraction method

## Installation

Install from GitHub by opening a terminal and running:

```powershell
pip install https://github.com/DominiqueMakowski/OpenMuse/zipball/main
```

The core — `find`, `record`, `decode_rawdata` — needs only `bleak`, `numpy` and `pandas`, and runs on **Python 3.10+**. `stream` and `view` need heavier optional dependencies, installed as extras:

```powershell
pip install "OpenMuse[stream]"   # adds mne-lsl, for LSL output
pip install "OpenMuse[view]"     # adds vispy, for the live viewer
pip install "OpenMuse[all]"      # both
```

They are imported lazily, so a missing extra degrades that one entry point instead of breaking `import OpenMuse`. This matters on platforms where those wheels do not exist — a 32-bit armhf Raspberry Pi, for instance, has no `mne-lsl` and no Python 3.12, but records and decodes fine on 3.11.

## Usage

After installing the package, open a terminal and use the following commands:

### Find Muse devices

Power up the Muse S Athena headband (a blue light should appear on the front) and run:

```powershell
OpenMuse find
```

This will print the MAC addresses of nearby Muse devices. Note the address of your device for the next steps.


### Record data to a file

```powershell
OpenMuse record --address <your-muse-address> --duration 60 --outfile data.txt
```

> [!TIP]
> By default, recording and streaming use the `--preset p1041`, which enables all channels. You can change the preset using the `--preset` argument, or ask for sensors with `--sensors` and let OpenMuse pick (see below for the list of documented presets).

Options that matter for long, unattended recordings:

| Option | Default | What it does |
|---|---|---|
| `--stall-timeout` | `120` | Abort with a non-zero exit status if no BLE notification arrives for this long. A dropped link does **not** otherwise stop a recording: the process keeps awaiting notifications that never come, writes nothing, and reports success when the duration elapses — so a supervisor never restarts it. `0` disables. |
| `--battery-interval` | `300` | Ask the device for its own status this often. Each reply is written into the raw file and decodes into an authoritative `BATTERY` row, so a drain curve for the whole night falls out of the normal decode. `0` disables. |
| `--low-latency` | off | Send `L1`, requesting a short BLE connection interval. Recording does not need it — nothing cares whether a sample arrives 15 ms or 200 ms late — and the radio duty cycle it forces costs battery. `stream` enables it automatically. |

`record()` returns a `RecordingResult` per device (`notifications`, `bytes_written`) so a caller can detect a zero-yield run, and raises `RecordingStalled` when the link dies.

#### Loading Recorded Data

Once your file is recorded, you can load it in Python using:

```python
import pandas as pd
import OpenMuse 

with open("data.txt", "r", encoding="utf-8") as f:
    messages = f.readlines()
data = OpenMuse.decode_rawdata(messages)

# Plot Movement Data
data["ACCGYRO"].plot(
    x="time",
    y=["ACC_X", "ACC_Y", "ACC_Z", "GYRO_X", "GYRO_Y", "GYRO_Z"],
    subplots=True
)
```

![](./media/example_accgyro.png)

#### The `time` column

> [!IMPORTANT]
> By default `time` is a **reconstruction**, not a measurement. It is generated as `sample_index / nominal_rate` starting at 0, so it **restarts at 0 on every call** (decoding one file in chunks stacks the chunks on top of each other), it uses the nominal rate — ~0.14% fast on real hardware, about 40 s of drift over an 8 h night — and BLE dropouts are absorbed into it instead of appearing as gaps.

For a timebase anchored to the ISO-8601 arrival stamp that begins every line of the raw file:

```python
frames, info = OpenMuse.decode_rawdata(messages, timebase="absolute", return_info=True)

frames["EEG"]["time"]                        # unix seconds, comparable across files
info["streams"]["EEG"]["fs_measured"]        # e.g. 255.62, not the nominal 256
info["streams"]["EEG"]["n_segments"]         # > 1 means the link dropped
info["streams"]["EEG"]["n_skipped"]          # subpackets that could not be used
```

The stream is split wherever the wall clock advanced by more than the sample count can explain, and `time = a + b·index` is least-squares fitted per segment: monotonic by construction, pinned to arrival time, and it *measures* the rate rather than assuming it. **Read `fs_measured`; do not assume the nominal rate.**

### Lab Streaming Layer (LSL)

> [!NOTE]  
> OpenMuse uses [MNE-LSL](https://mne.tools/mne-lsl/), an improved python-binding for the Lab Streaming Layer C++ library, `mne_lsl.lsl`, replacing `pylsl`.

To stream data over LSL, use:

```powershell
OpenMuse stream --address <your-muse-address>
```

This creates **separate LSL streams** for groups of channels (`Muse_EEG`,  `Muse_ACCGYRO`, etc.).

The following options are available:
- `--address`: The MAC address of your Muse device (required)
- `--duration`: Duration of the recording in seconds (if not specified, streaming will continue until you press Ctrl+C in the terminal)
- `--preset`: Preset configuration (default: `p1041` for all channels)
- `--record`: Record raw BLE packets to a file (optional)

### Visualize Live Streams

To visualize live LSL streams, open a new terminal (while the streaming is running) and run:

```powershell
OpenMuse view
```

### Hyperscanning (Multiple Devices Streaming)

To record from multiple Muse devices simultaneously (e.g., for hyperscanning studies):

1. **Discover devices**: Power on all Muse headbands and run `OpenMuse find` to note down each device's MAC address.

2. **Start streaming**: You can stream from multiple devices simultaneously by passing multiple MAC addresses:

```powershell
OpenMuse stream --address 00:55:DA:B9:FA:20 00:55:DA:BB:CD:CD
```

This will manage all connections in parallel within a single process. LSL streams will be named with their respective device IDs (e.g., `Muse-EEG (MAC1)`), allowing you to distinguish them on the receiving end.

3. **Visualize (optional)**: When multiple devices are streaming, the viewer will automatically detect and display streams from only the first device by default to avoid confusion. To view a specific device, use the `--address` filter:

```powershell
# View first device
OpenMuse view --address 00:55:DA:B9:FA:20

# View second device (open in a separate terminal)
OpenMuse view --address 00:55:DA:BB:CD:CD
```

> [!NOTE]
> If you run `OpenMuse view` without the `--address` argument when multiple devices are streaming, it will automatically select the first detected device and display a warning message listing all available devices.

4. **Record with LabRecorder**: Use [LabRecorder](https://github.com/labstreaminglayer/App-LabRecorder) to record all streams to an XDF file. Select all the streams from your devices (e.g., `Muse-EEG (00:55:DA:B9:FA:20)`, `Muse-EEG (00:55:DA:BB:CD:CD)`, etc.).

#### Multi-Device Raw Recording (Alternative to LSL)

You can also do raw BLE packet recordings from multiple Muse devices simultaneously by passing multiple MAC addresses:

```powershell
OpenMuse record --address 00:55:DA:B9:FA:20 00:55:DA:BB:CD:CD --duration 60 --outfile data.txt
```

When recording from multiple devices, the device address will be automatically appended to the filename to avoid collisions (e.g., `data_0055DAB9FA20.txt`, `data_0055DABBCDCD.txt`).

## Technical Details

### Specs

Muse S Athena specs (From the [Muse website](https://eu.choosemuse.com/products/muse-s-athena)):
- Wireless Connection: BLE 5.3, 2.4 GHz
- EEG Channels: 4 EEG channels (TP9, AF7, AF8, TP10) + 4 amplified Aux channels
  - Sample Rate: 256 Hz
  - Sample Resolution: 14 bits / sample
- Accelerometer: Three-axis at 52Hz, 16-bit resolution, range +/- 2G
- Gyroscope: Three-axis at 52Hz, 16-bit resolution, range +/- 250dps
- PPG Sensor: Triple wavelength: IR (850nm), Near-IR (730nm), Red (660nm), 64 Hz sample rate, 20-bit resolution
- fNIRS Sensor: 5-optode bilateral frontal cortex hemodynamics, 64 Hz sample rate, 20-bit resolution

### Optics channel order

Optics16 is transmitted **wavelength-major**, not location-major (issue #24): RED×4, AMB×4, NIR×4, IR×4, each in `LI, RI, LO, RO` order. Selecting a channel by name under the old, location-major labelling read a different photodiode every time — silently, with plausible-looking numbers — and made pulse oximetry, which needs a specific RED/IR pair, impossible to get right.

The evidence is physiological. On an overnight capture (firmware 3.1.23, `Athena_RevE`, `p1041`; subject asleep, still, dark room) four channels labelled `AMB` under the old order carried a strong heartbeat, cardiac SNR 59–722 — impossible for a channel whose LED is off:

| mapping | AMB median SNR | LED median SNR | separation | channels consistent |
|---|---|---|---|---|
| location-major (old) | 134.0 | 187.4 | 1.4× | 12/16 |
| **wavelength-major** | **8.7** | **382.8** | **44×** | **16/16** |

`tests/test_optics_mapping.py` pins the same conclusion on this repo's own captures: indices 4–7 are always a tight, high, near-constant quartet standing clear of the other twelve — the signature of an ambient reference — and under the corrected order those are exactly the `_AMB` channels.

Optics8 and Optics4 are separate packet types (TAGs `0x35`, `0x34`), not slices of Optics16, and have their own layouts (`OPTICS8_CHANNELS`, `OPTICS4_CHANNELS`).

> [!NOTE]
> Confirmed on firmware 3.1.23 only (issue #24 reported 3.1.19). If the order turns out to be firmware-dependent it will have to be keyed on the version reported by `v7`.

### Battery

The device reports its own state of charge in the JSON reply to the `s` control command:

```json
{"hn":"MuseS-1E2B","sn":"7011-AH29-1E2B","bp":89.11,"ps":4161,"rc":0}
```

`bp` is percent and `ps` is pack millivolts. This is authoritative and is what `record(battery_interval=...)` samples; the resulting `BATTERY` rows carry `source == "status"` plus a `battery_millivolts` column.

The battery field in the data packets is a guess at undocumented bytes and can be wrong: the TAG scan that finds it declares `data_len = 20` for `0x98` while real `0x98` packets carry ~200 bytes of payload, so a byte inside the payload can be mistaken for a TAG and the "percentage" is then an arbitrary `uint16 / 256` — above 100 about 61% of the time (150.0, 228.0 and 229.0 have all been observed). Out-of-range values are now returned as `NaN` rather than as a number known to be wrong.

There is no standard BLE Battery Service on this device — GATT enumeration shows only the proprietary `0000fe8d` service — so the control channel is the only reliable source. To read it outside a recording:

```python
OpenMuse.get_status("<your-muse-address>")   # {'fw': '3.1.23', 'bp': 89.11, 'ps': 4161, ...}
```

> [!WARNING]
> Only one BLE client may own a Muse at a time. Do not call `get_status()` while `record()` or `stream()` holds the device — connecting alongside them can leave the headset in a transitional state and kill the recording. Use `--battery-interval` instead.

### Presets

| Presets                                 | EEG   | Optics   | ACC/GYRO | Battery | Red LED |
|-----------------------------------------|:-----:|:--------:|:--------:|:-------:|:--------|
| p20, p21, p50, p51, p60, p61            | EEG4  |   —      |    X     |    X    |   off   |
| p1034                                   | EEG4  | Optics8  |    X     |    X    | bright  |
| p1035                                   | EEG4  | Optics4  |    X     |    X    |  dim    |
| p1043                                   | EEG8  | Optics8  |    X     |    X    | bright  |
| p1044                                   | EEG8  | Optics8  |    X     |    X    |  dim    |
| p1041, p1042                            | EEG8  | Optics16 |    X     |    X    | bright  |
| p1045                                   | EEG8  | Optics4  |    X     |    X    |  dim    |
| p1046                                   | EEG8  | Optics4  |    X     |    X    |   —     |
| p4129                                   | EEG8  | Optics4  |    X     |    X    |  dim    |

*Table derived from the signature of the data packets present in the data. More presets could exist.*

The EEG/Optics columns are now also available in code as `OpenMuse.PRESETS`, printable with `OpenMuse presets`, and re-derived from `tests/test_data/data_p*.txt` by `tests/test_presets.py` so they cannot silently drift. (`p1034` is EEG**4**, not EEG8: its packets carry TAG `0x11`.)

Rather than remembering the numbers, you can ask for sensors and let the preset be chosen for you:

```powershell
OpenMuse record --address <addr> --sensors EEG ACCGYRO   # -> p21
```

```python
OpenMuse.preset_for_sensors(["EEG", "ACCGYRO"])            # 'p21'
OpenMuse.describe_preset("p1041")                          # {'EEG': 8, 'OPTICS': 16, ...}
```

> [!TIP]
> **What the manufacturer's own app uses: `p1035`.** Captured from an Android
> HCI snoop log during a sleep session (firmware 3.1.23), the vendor app runs a
> brief `p21` fit check, then halts and settles on **`p1035`** — EEG4 +
> Optics4 + ACC/GYRO — for the session itself. Four optics channels instead of
> sixteen keeps PPG available at a fraction of the LED cost. It also sends
> `L1` *before* `dc001` and then explicitly stands low latency down with `L0`
> once streaming is established, which is what `record()` now does.

#### Presets and battery life

The preset is the **only** control over which sensors the headset actually runs, so it is the only setting that saves power at the source — `stream --sensors` filters after reception and saves nothing.

Optics is ~30% of the payload *and* drives sixteen NIR/IR/RED LEDs, which is typically a dominant draw in a wearable. If you do not need PPG/fNIRS, an optics-free preset (`p21`) is by far the cheapest useful configuration, and the lower data rate in turn allows a longer BLE connection interval. Note that EEG channels 4–7 in the 8-channel presets are the Aux channels, not scalp electrodes — `p21` still gives you TP9/AF7/AF8/TP10.

Measured payload for `p1041` (all sensors) over a 2.06 h capture:

| stream  | rate     | channels | values/s | share |
|---------|----------|----------|----------|-------|
| EEG     | 253.4 Hz | 8        | 2027     | 60.4% |
| OPTICS  | 63.6 Hz  | 16       | 1018     | 30.3% |
| ACCGYRO | 51.7 Hz  | 6        | 310      | 9.3%  |
| **total** |        |          | **3355** | ~6.7 KB/s |

### Acknowledgements

This project would not have been possible without the breakthroughs of [amused-Py](https://github.com/Amused-EEG/amused-py) that identified the communication protocol and [AbosaSzakal's parser](https://github.com/AbosaSzakal/MuseAthenaDataformatParser) who documented the data structure.
