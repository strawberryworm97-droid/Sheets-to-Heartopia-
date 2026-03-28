"""
Heartopia virtual keyboard layouts (PC key → MIDI pitch).

Full in-game layout (named heartopia_22 in the app):
- Bottom row , … ] + l ; 0 - =  → MIDI 48–59 (C3–B3 chromatic)
- Middle row z … m + s d g h j → MIDI 60–71 (C4–B4 chromatic)
- Top row q … i + 2 3 5 6 7   → MIDI 72–84 (C5–C6 chromatic)

15-key: Q–I + A–J diatonic (snap black notes to nearest white).
"""

from __future__ import annotations
from copy import deepcopy

# Top row (high octave), MIDI 72–84
MIDI_TOP: dict[int, str] = {
    72: "q",
    73: "2",
    74: "w",
    75: "3",
    76: "e",
    77: "r",
    78: "5",
    79: "t",
    80: "6",
    81: "y",
    82: "7",
    83: "u",
    84: "i",
}

# Middle row (middle octave), MIDI 60–71
MIDI_MIDDLE_37: dict[int, str] = {
    60: "z",
    61: "s",
    62: "x",
    63: "d",
    64: "c",
    65: "v",
    66: "g",
    67: "b",
    68: "h",
    69: "n",
    70: "j",
    71: "m",
}

# Bottom row (low octave), MIDI 48–59
MIDI_BOTTOM_37: dict[int, str] = {
    48: ",",
    49: "l",
    50: ".",
    51: ";",
    52: "/",
    53: "o",
    54: "0",
    55: "p",
    56: "-",
    57: "[",
    58: "=",
    59: "]",
}

# 15-key: A–J base + Q–I upper (diatonic)
MIDI_15: dict[int, str] = {
    60: "a",
    62: "s",
    64: "d",
    65: "f",
    67: "g",
    69: "h",
    71: "j",
    72: "q",
    74: "w",
    76: "e",
    77: "r",
    79: "t",
    81: "y",
    83: "u",
    84: "i",
}

ALLOWED_MIDI_15 = frozenset(MIDI_15.keys())
ALLOWED_MIDI_37 = frozenset(range(48, 85))

DEFAULT_MIDI_TOP = dict(MIDI_TOP)
DEFAULT_MIDI_MIDDLE_37 = dict(MIDI_MIDDLE_37)
DEFAULT_MIDI_BOTTOM_37 = dict(MIDI_BOTTOM_37)
DEFAULT_MIDI_15 = dict(MIDI_15)


def build_default_keymaps() -> dict[str, dict[int, str]]:
    return {
        "top_22": deepcopy(DEFAULT_MIDI_TOP),
        "middle_22": deepcopy(DEFAULT_MIDI_MIDDLE_37),
        "bottom_22": deepcopy(DEFAULT_MIDI_BOTTOM_37),
        "layout_15": deepcopy(DEFAULT_MIDI_15),
    }

def nearest_midi(midi: int, allowed: frozenset[int]) -> int:
    if midi in allowed:
        return midi
    return min(allowed, key=lambda m: (abs(m - midi), m))


def midi_to_key_37(midi: int) -> str:
    m = int(round(midi))
    if 48 <= m <= 59:
        return MIDI_BOTTOM_37[m]
    if 60 <= m <= 71:
        return MIDI_MIDDLE_37[m]
    if 72 <= m <= 84:
        return MIDI_TOP[m]
    n = nearest_midi(m, ALLOWED_MIDI_37)
    if 48 <= n <= 59:
        return MIDI_BOTTOM_37[n]
    if 60 <= n <= 71:
        return MIDI_MIDDLE_37[n]
    return MIDI_TOP[n]


def midi_to_key_15(midi: int) -> str:
    m = int(round(midi))
    n = nearest_midi(m, ALLOWED_MIDI_15)
    return MIDI_15[n]


def pitch_to_heartopia_key(p, layout: str, transpose_semitones: int = 0) -> str:
    from music21.pitch import Pitch

    if isinstance(p, Pitch):
        midi = p.midi
    else:
        midi = int(p)
    midi = max(0, min(127, midi + transpose_semitones))
    if layout == "heartopia_15":
        return midi_to_key_15(midi)
    if layout == "heartopia_22":
        return midi_to_key_37(midi)
    raise ValueError(f"Unknown layout: {layout}")

def midi_to_key_37_custom(midi: int, keymaps: dict[str, dict[int, str]]) -> str:
    m = int(round(midi))

    bottom = keymaps["bottom_22"]
    middle = keymaps["middle_22"]
    top = keymaps["top_22"]

    if 48 <= m <= 59:
        return bottom[m]
    if 60 <= m <= 71:
        return middle[m]
    if 72 <= m <= 84:
        return top[m]

    n = nearest_midi(m, ALLOWED_MIDI_37)
    if 48 <= n <= 59:
        return bottom[n]
    if 60 <= n <= 71:
        return middle[n]
    return top[n]


def midi_to_key_15_custom(midi: int, keymaps: dict[str, dict[int, str]]) -> str:
    m = int(round(midi))
    n = nearest_midi(m, ALLOWED_MIDI_15)
    return keymaps["layout_15"][n]


def pitch_to_heartopia_key_custom(
    p,
    layout: str,
    keymaps: dict[str, dict[int, str]],
    transpose_semitones: int = 0,
) -> str:
    from music21.pitch import Pitch

    if isinstance(p, Pitch):
        midi = p.midi
    else:
        midi = int(p)

    midi = max(0, min(127, midi + transpose_semitones))

    if layout == "heartopia_15":
        return midi_to_key_15_custom(midi, keymaps)
    if layout == "heartopia_22":
        return midi_to_key_37_custom(midi, keymaps)

    raise ValueError(f"Unknown layout: {layout}")
