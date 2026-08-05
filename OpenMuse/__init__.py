"""OpenMuse: Minimal utilities for Muse EEG devices.

The core of the package (recording, decoding, device discovery) depends only on
``bleak``, ``numpy`` and ``pandas``.  The ``stream`` and ``view`` entry points
additionally need ``mne-lsl`` and ``vispy``, which are *optional* extras::

    pip install OpenMuse[stream]
    pip install OpenMuse[view]

Those two names are resolved lazily (PEP 562), so ``import OpenMuse`` keeps
working on platforms where the optional wheels do not exist -- notably 32-bit
armhf Raspberry Pi, where ``record``/``decode_rawdata`` run fine on Python 3.11
but ``mne-lsl`` cannot be installed.
"""

from .decode import ControlChannel, decode_rawdata, parse_message
from .muse import (describe_preset, find_muse, get_status, HandshakeFailed, MuseS,
                   PRESETS, preset_for_sensors)
from .record import record, RecordingResult, RecordingStalled

__version__ = "0.1.9"

__all__ = [
    "ControlChannel",
    "decode_rawdata",
    "describe_preset",
    "find_muse",
    "get_status",
    "MuseS",
    "parse_message",
    "PRESETS",
    "preset_for_sensors",
    "record",
    "RecordingResult",
    "HandshakeFailed",
    "RecordingStalled",
    "stream",
    "view",
]

# Optional entry points -> (module, extra name, packages the extra installs)
_OPTIONAL_ATTRS = {
    "stream": (".stream", "stream", "mne-lsl"),
    "view": (".view", "view", "vispy"),
}


def __getattr__(name):
    """Import ``stream``/``view`` on first use (PEP 562).

    A missing optional dependency degrades that one entry point instead of
    breaking ``import OpenMuse`` outright.
    """
    try:
        module_name, extra, packages = _OPTIONAL_ATTRS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None

    from importlib import import_module

    try:
        module = import_module(module_name, __name__)
    except ImportError as exc:
        raise ImportError(
            f"OpenMuse.{name} requires the optional '{extra}' extra "
            f"({packages}), which is not installed. "
            f"Install it with: pip install 'OpenMuse[{extra}]'\n"
            f"Original error: {exc}"
        ) from exc

    value = getattr(module, name)
    globals()[name] = value  # cache so __getattr__ runs only once
    return value


def __dir__():
    return sorted(__all__)
