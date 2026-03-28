"""
Convert digital sheet music (MusicXML, MIDI, MuseData, etc.) to letter-style note names
similar to beginner sites like NoobNotes: C D E with optional octave and accidentals.

Does not read scanned PDFs or images — use MuseScore or similar to export MusicXML/MIDI first.
"""

from __future__ import annotations

import argparse
import json
import sys
import tkinter as tk
from collections import defaultdict
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Iterable

from music21 import chord, converter, interval, note, stream
 
from heartopia_maps import (
    pitch_to_heartopia_key_custom,
    build_default_keymaps,
    DEFAULT_MIDI_TOP,
    DEFAULT_MIDI_MIDDLE_37,
    DEFAULT_MIDI_BOTTOM_37,
    DEFAULT_MIDI_15,
)

# Visual gap between clusters (easier to scan while playing).
CLUSTER_GAP = "  "


def _onset_key(offset: float) -> float:
    """Stable key for same attack time; rounds float noise so one chord stays one cluster."""
    return round(float(offset), 5)


def pitch_to_letters(p, *, show_octave: bool, octave_style: str) -> str:
    """Readable note name: Eb not E-, optional octave as number or apostrophe (vs middle C)."""
    name = p.name.replace("-", "b")
    if not show_octave:
        return name
    o = p.implicitOctave
    if octave_style == "number":
        return f"{name}{o}"
    # apostrophe: middle C = C, each octave up adds ', down adds ,
    base = 4
    diff = o - base
    if diff == 0:
        return name
    if diff > 0:
        return name + "'" * diff
    return name + "," * (-diff)


def element_to_string(
    el,
    *,
    show_octave: bool,
    octave_style: str,
    rest_symbol: str,
) -> str | None:
    if isinstance(el, note.Rest):
        return rest_symbol if rest_symbol else None
    if isinstance(el, note.Note):
        return pitch_to_letters(el.pitch, show_octave=show_octave, octave_style=octave_style)
    if isinstance(el, chord.Chord):
        parts = [
            pitch_to_letters(p, show_octave=show_octave, octave_style=octave_style)
            for p in el.pitches
        ]
        return "+".join(parts)
    return None


def get_parts(score: stream.Score) -> list[stream.Stream]:
    """Return each staff/part separately when present; otherwise one flattened stream."""
    if getattr(score, "parts", None) and len(score.parts) > 0:
        return list(score.parts)
    return [score]


def stream_to_letter_line(
    s: stream.Stream,
    *,
    show_octave: bool,
    octave_style: str,
    rest_symbol: str,
    separator: str,
) -> str:
    tokens: list[str] = []
    for el in s.flatten().notesAndRests:
        t = element_to_string(
            el,
            show_octave=show_octave,
            octave_style=octave_style,
            rest_symbol=rest_symbol,
        )
        if t is not None:
            tokens.append(t)
    return separator.join(tokens)


def _duration_units(el, units_per_quarter: int = 4) -> int:
    """
    Convert note/rest duration into grid units.
    Default grid: 4 units per quarter note
    - quarter = 4
    - eighth = 2
    - 16th = 1
    """
    ql = float(el.duration.quarterLength)
    if ql <= 0:
        ql = 0.25
    u = int(round(ql * units_per_quarter))
    return max(1, u)


def _hold_suffix(hold_count: int, compress_double: bool) -> str:
    """hold_count = extra beats beyond the first (number of > to print)."""
    if hold_count <= 0:
        return ""
    if not compress_double:
        return ">" * hold_count
    parts: list[str] = []
    i = 0
    while i < hold_count:
        if i + 1 < hold_count:
            parts.append(">>")
            i += 2
        else:
            parts.append(">")
            i += 1
    return "".join(parts)

def _gap_from_onset_delta(delta: float) -> str:
    EPS = 1e-6
    delta = float(delta)

    if delta <= 0.25 + EPS:
        return " "     # 16th = no extra visible gap beyond token separation
    elif delta <= 0.5 + EPS:
        return "  "    # 8th
    else:
        return "   "   # quarter+


def _join_timed_tokens(timed_tokens: list[tuple[float, str]]) -> str:
    """
    Join (offset, token) pairs using rhythm-aware spacing.
    """
    if not timed_tokens:
        return ""

    out = [timed_tokens[0][1]]
    prev_offset = timed_tokens[0][0]

    for offset, token in timed_tokens[1:]:
        out.append(_gap_from_onset_delta(offset - prev_offset))
        out.append(token)
        prev_offset = offset

    return "".join(out)


def _wrap_timed_tokens(
    timed_tokens: list[tuple[float, str]],
    width: int = 80,
    break_threshold: float = 0.5,
) -> str:
    """
    Wrap rhythm-aware output without splitting close rhythmic groups.

    break_threshold:
    - gaps <= this stay in the same unbreakable chunk
    - gaps > this are allowed wrap points

    Default:
    - 0.5 quarterLength = eighth-note threshold
    So 16ths and 8ths stay grouped together.
    """
    if not timed_tokens:
        return ""

    # Step 1: build larger chunks that should stay together
    chunks: list[str] = []
    current_chunk = timed_tokens[0][1]
    prev_offset = timed_tokens[0][0]

    for offset, token in timed_tokens[1:]:
        delta = float(offset) - float(prev_offset)
        gap = _gap_from_onset_delta(delta)

        if delta <= break_threshold:
            # Keep this token glued to the current chunk
            current_chunk += gap + token
        else:
            # Start a new chunk
            chunks.append(current_chunk)
            current_chunk = token

        prev_offset = offset

    chunks.append(current_chunk)

    # Step 2: wrap only between chunks
    lines: list[str] = []
    current_line = chunks[0]

    for chunk in chunks[1:]:
        piece = " " + chunk   # small separator between larger wrap chunks

        if len(current_line) + len(piece) <= width:
            current_line += piece
        else:
            lines.append(current_line)
            current_line = chunk

    lines.append(current_line)
    return "\n".join(lines)

def _join_heartopia_tokens(tokens: list[str], gap: str = CLUSTER_GAP) -> str:
    """Join cluster tokens; __RESTn__ expands to n spaces (no asterisks)."""
    out: list[str] = []
    for t in tokens:
        if t.startswith("__REST") and t.endswith("__"):
            n = int(t[6:-2])
            if n > 0:
                if out:
                    out.append(gap)
                out.append(" " * n)
            continue
        if out:
            out.append(gap)
        out.append(t)
    return "".join(out)


def _group_by_same_onset(m: stream.Stream) -> list[list]:
    """
    One cluster per attack time only (notes that truly sound together in the score).
    Sequential eighths/sixteenths stay separate so output matches how you press keys in time.
    """
    g: defaultdict[float, list] = defaultdict(list)
    for el in m.flatten().notesAndRests:
        g[_onset_key(el.offset)].append(el)
    return [g[k] for k in sorted(g.keys())]

def _element_end_offset(el) -> float:
    return float(el.offset) + float(el.duration.quarterLength)


def _beat_slots_for_stream(s: stream.Stream) -> list[tuple[float, float]]:
    """
    Return quarter-note time windows across the stream.
    Each slot is [start, end), one beat wide.
    """
    flat = list(s.flatten().notesAndRests)
    if not flat:
        return []

    start = min(float(el.offset) for el in flat)
    end = max(_element_end_offset(el) for el in flat)

    slots: list[tuple[float, float]] = []
    t = float(int(start))
    while t < end:
        slots.append((t, t + 1.0))
        t += 1.0
    return slots


def _active_group_for_slot(s: stream.Stream, slot_start: float, slot_end: float) -> list:
    """
    Collect notes/chords/rests active during a beat slot.
    """
    active: list = []
    for el in s.flatten().notesAndRests:
        el_start = float(el.offset)
        el_end = _element_end_offset(el)
        if el_start < slot_end and el_end > slot_start:
            active.append(el)
    return active


def _group_by_active_beats(s: stream.Stream) -> list[list]:
    """
    One group per beat, using notes sounding during that beat.
    """
    groups: list[list] = []
    for slot_start, slot_end in _beat_slots_for_stream(s):
        grp = _active_group_for_slot(s, slot_start, slot_end)
        if grp:
            groups.append(grp)
    return groups

def _iter_measure_streams(part: stream.Stream) -> list[tuple[str, stream.Stream]]:
    measures = list(part.getElementsByClass(stream.Measure))
    if measures:
        return [(str(i + 1), meas) for i, meas in enumerate(measures)]
    return [("1", part)]


def _collect_pitches_and_rest(grp: list) -> tuple[list, int]:
    """Return (list of (pitch, units), rest_units)."""
    pitch_dur: list[tuple] = []
    rest_units = 0
    for el in grp:
        u = _duration_units(el)
        if isinstance(el, note.Rest):
            rest_units = max(rest_units, u)
        elif isinstance(el, chord.Chord):
            for p in el.pitches:
                pitch_dur.append((p, u))
        elif isinstance(el, note.Note):
            pitch_dur.append((el.pitch, u))
    return pitch_dur, rest_units


def _heartopia_cluster_token(
    grp: list,
    layout: str,
    transpose_semitones: int,
    compress_double_hold: bool,
    *,
    keymaps: dict[str, dict[int, str]] | None = None,
    rest_as_token: bool,
    rest_one_beat_space: bool = True,
    include_holds: bool = True,
) -> str:
    """
    One cluster: keys concatenated low-to-high.
    In sheet_faithful mode, hold suffixes may be shown.
    In playable mode, include_holds=False so each beat is just a playable snapshot.
    """
    pitch_dur, rest_beats = _collect_pitches_and_rest(grp)

    if pitch_dur:
        hold_str = ""
        if include_holds:
            max_u = max(u for _, u in pitch_dur)
            holds = max(0, max_u - 1)
            hold_str = _hold_suffix(holds, compress_double_hold)

        uniq_p: list = []
        seen: set[int] = set()
        for p, _ in sorted(pitch_dur, key=lambda x: x[0].midi):
            if p.midi not in seen:
                seen.add(p.midi)
                uniq_p.append(p)
        if keymaps is None:
            raise ValueError("Custom keymaps are required for Heartopia conversion.")

        keys = [
            pitch_to_heartopia_key_custom(p, layout, keymaps, transpose_semitones)
            for p in uniq_p
        ]
        return "".join(keys) + hold_str

    if rest_beats:
        if rest_as_token:
            if rest_beats == 1 and rest_one_beat_space:
                return "__REST1__"
            return f"__REST{rest_beats}__"
        return " " * rest_beats

    return ""


def stream_to_heartopia_line(
    s: stream.Stream,
    *,
    layout: str,
    transpose_semitones: int = 0,
    rest_one_beat_space: bool = True,
    compress_double_hold: bool = False,
    sheet_measures: bool = True,
    grouping_mode: str = "sheet_faithful",
    keymaps: dict[str, dict[int, str]] | None = None,
) -> str:
    """Heartopia keyboard codes in either sheet-faithful or playable grouping."""
    if not sheet_measures:
        return stream_to_heartopia_flat(
            s,
            layout=layout,
            transpose_semitones=transpose_semitones,
            rest_one_beat_space=rest_one_beat_space,
            compress_double_hold=compress_double_hold,
            grouping_mode=grouping_mode,
            keymaps=keymaps,
        )

    lines: list[str] = []
    for label, meas in _iter_measure_streams(s):
        if grouping_mode == "sheet_faithful":
            groups = _group_by_same_onset(meas)
            include_holds = False
        elif grouping_mode == "playable":
            groups = _group_by_active_beats(meas)
            include_holds = False
        else:
            raise ValueError(f"Unknown grouping_mode: {grouping_mode}")

        clusters: list[str] = []
        for grp in groups:
            t = _heartopia_cluster_token(
                grp,
                layout,
                transpose_semitones,
                compress_double_hold,
                keymaps=keymaps,
                rest_as_token=False,
                rest_one_beat_space=rest_one_beat_space,
                include_holds=include_holds,
            )
            if t:
                clusters.append(t)

        if not clusters:
            continue

        inner = CLUSTER_GAP.join(clusters)
        lines.append(f"m.{label:>3} | {inner} |")

    return "\n".join(lines)


def stream_to_heartopia_flat(
    s: stream.Stream,
    *,
    layout: str,
    transpose_semitones: int = 0,
    rest_one_beat_space: bool = True,
    compress_double_hold: bool = False,
    grouping_mode: str = "sheet_faithful",
    keymaps: dict[str, dict[int, str]] | None = None,
) -> str:
    """
    Single line Heartopia output.

    Rules:
    - same onset / chord -> fused token (e.g. h3)
    - 16th apart -> 1 space
    - 8th apart -> 2 spaces
    - quarter or more -> 3 spaces
    """
    timed_tokens: list[tuple[float, str]] = []

    # Flat mode should be onset-based for readable rhythmic spacing.
    groups = _group_by_same_onset(s)

    for grp in groups:
        offset = min(float(el.offset) for el in grp)

        token = _heartopia_cluster_token(
            grp,
            layout,
            transpose_semitones,
            compress_double_hold,
            keymaps=keymaps,
            rest_as_token=False,   # cleaner flat output; rests affect spacing via onset gaps
            rest_one_beat_space=rest_one_beat_space,
            include_holds=False,
        )

        if token:
            timed_tokens.append((offset, token))

    return _wrap_timed_tokens(timed_tokens, width=80, break_threshold=0.5)

def convert_file(
    path: Path | str,
    *,
    part_index: int | None = None,
    show_octave: bool = False,
    octave_style: str = "number",
    rest_symbol: str = "—",
    separator: str = " ",
    output_mode: str = "letters",
    transpose_semitones: int = 0,
    xml_octave_correction: int = 0,
    heartopia_rest_space: bool = True,
    heartopia_compress_hold: bool = False,
    heartopia_sheet_measures: bool = True,
    grouping_mode: str = "sheet_faithful",
    keymaps: dict[str, dict[int, str]] | None = None,
) -> str:
    """Parse a file and return letter notes or Heartopia key codes."""
    if output_mode == "heartopia_37":
        output_mode = "heartopia_22"

    p = Path(path)
    score = converter.parse(str(p))
    if not isinstance(score, stream.Score):
        single = stream.Score()
        single.insert(0, score)
        score = single

    if transpose_semitones != 0 and output_mode == "letters":
        score = score.transpose(interval.ChromaticInterval(transpose_semitones))

    parts = get_parts(score)
    lines: list[str] = []

    if part_index is not None:
        if part_index < 0 or part_index >= len(parts):
            raise IndexError(f"Part index {part_index} out of range (0–{len(parts) - 1})")
        target_parts = [parts[part_index]]
    else:
        target_parts = parts

    effective_transpose = transpose_semitones
    if (
        p.suffix.lower() in {".xml", ".musicxml", ".mxl"}
        and output_mode in ("heartopia_15", "heartopia_22")
    ):
        effective_transpose += xml_octave_correction

    for i, part in enumerate(target_parts):
        if output_mode == "letters":
            line = stream_to_letter_line(
                part,
                show_octave=show_octave,
                octave_style=octave_style,
                rest_symbol=rest_symbol,
                separator=separator,
            )
        elif output_mode in ("heartopia_15", "heartopia_22"):
            line = stream_to_heartopia_line(
                part,
                layout=output_mode,
                transpose_semitones=effective_transpose,
                rest_one_beat_space=heartopia_rest_space,
                compress_double_hold=heartopia_compress_hold,
                sheet_measures=heartopia_sheet_measures,
                grouping_mode=grouping_mode,
                keymaps=keymaps,
            )
        else:
            raise ValueError(f"Unknown output_mode: {output_mode}")

        if len(target_parts) > 1:
            label = part.partName or part.id or f"Part {i + 1}"
            lines.append(f"## {label}\n{line}")
        else:
            lines.append(line)

    return "\n\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Convert sheet music files to letter notes.")
    ap.add_argument("file", type=Path, help="MusicXML, MIDI, or other music21-supported file")
    ap.add_argument(
        "--part",
        type=int,
        default=None,
        help="0-based part index (omit for all parts)",
    )
    ap.add_argument(
        "--format",
        choices=("letters", "heartopia-15", "heartopia-22"),
        default="heartopia-22",
        help="Output: letter names or Heartopia PC key codes",
    )
    ap.add_argument(
        "--transpose",
        type=int,
        default=0,
        help="Semitones to shift pitch (+ up, - down). Heartopia: remaps keys; letters: transposes names.",
    )
    ap.add_argument(
        "--flat-line",
        action="store_true",
        help="Heartopia: one line of text instead of measure rows",
    )
    ap.add_argument("--octave", action="store_true", help="Include octave in each note")
    ap.add_argument(
        "--octave-style",
        choices=("number", "apostrophe"),
        default="number",
        help="How to show octave (default: number like C4)",
    )
    ap.add_argument("--no-rest", action="store_true", help="Skip rests in output")
    ap.add_argument(
        "--separator",
        default=" ",
        help="Character between notes (default: space)",
    )
    ap.add_argument(
        "--heartopia-star-rest",
        action="store_true",
        help="Use * for 1-beat rests too (Heartopia modes only; default: space for 1 beat)",
    )
    ap.add_argument(
        "--heartopia-compress-hold",
        action="store_true",
        help="Use >> for pairs of hold beats (Heartopia modes only)",     
    )
    return ap


def main_cli(argv: Iterable[str] | None = None) -> int:
    ap = build_arg_parser()
    args = ap.parse_args(list(argv) if argv is not None else None)
    rest = "" if args.no_rest else "—"
    fmt = args.format.replace("-", "_")
    try:
        text = convert_file(
            args.file,
            part_index=args.part,
            show_octave=args.octave,
            octave_style=args.octave_style,
            rest_symbol=rest,
            separator=args.separator,
            output_mode=fmt,
            transpose_semitones=args.transpose,
            heartopia_rest_space=not args.heartopia_star_rest,
            heartopia_compress_hold=args.heartopia_compress_hold,
            heartopia_sheet_measures=not args.flat_line,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(text)
    return 0


class SheetToLettersApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Sheet music → Heartopia")
        self.minsize(620, 480)
        self._path: Path | None = None
        import sys
        from pathlib import Path

        if getattr(sys, "frozen", False):
            base_dir = Path(sys.executable).resolve().parent
        else:
            base_dir = Path(__file__).resolve().parent

        self.settings_path = base_dir / "heartopia_keymaps.json"
        self.keymaps = build_default_keymaps()
        self._load_keymaps_from_disk()
        self.keybinding_vars: dict[tuple[str, int], tk.StringVar] = {}

        bg = "#ffffff"
        accent = "#ec4899"
        accent_light = "#fce7f3"
        self.configure(bg=bg)
        sty = ttk.Style()
        sty.theme_use("clam")
        sty.configure("TFrame", background=bg)
        sty.configure("TLabel", background=bg, foreground="#374151")
        sty.configure(
            "Pink.TButton",
            background=bg,
            foreground="#374151",
            bordercolor=accent,
            lightcolor=accent,
            darkcolor=accent,
            relief="solid",
            borderwidth=2,
            padding=(14, 6),
        )
        sty.map(
            "Pink.TButton",
            background=[("active", "#fef7fb"), ("pressed", "#fce7f3")],
            bordercolor=[("active", accent), ("pressed", accent)],
            lightcolor=[("active", accent), ("pressed", accent)],
            darkcolor=[("active", accent), ("pressed", accent)],
        )
        sty.configure(
            "TCombobox",
            fieldbackground=bg,
            background=bg,
            foreground="#374151",
            arrowcolor=accent,
            borderwidth=1,
            relief="flat",
)
        sty.map(
            "TCombobox",
            fieldbackground=[("readonly", bg)],
            background=[("readonly", bg)],
            foreground=[("readonly", "#374151")],
        )
        sty.configure("TSpinbox", fieldbackground=bg, lightcolor=accent_light)

        main = ttk.Frame(self, padding=12)
        main.pack(fill=tk.BOTH, expand=True)
        notebook = ttk.Notebook(main)
        notebook.pack(fill=tk.BOTH, expand=True)

        converter_tab = ttk.Frame(notebook, padding=12)
        keybindings_tab = ttk.Frame(notebook, padding=12)

        notebook.add(converter_tab, text="Converter")
        notebook.add(keybindings_tab, text="Keybindings")
        self._build_keybindings_tab(keybindings_tab)

        row1 = ttk.Frame(converter_tab)
        row1.pack(fill=tk.X, pady=(0, 8))

        ttk.Button(row1, text="Open file…", command=self._open_file, style="Pink.TButton").pack(
            side=tk.LEFT
        )
        self.path_var = tk.StringVar(value="No file loaded")
        ttk.Label(row1, textvariable=self.path_var, wraplength=480).pack(
            side=tk.LEFT, padx=(12, 0)
        )

        opts = ttk.Frame(converter_tab)
        opts.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(opts, text="Output format:").pack(side=tk.LEFT)
        self.format_var = tk.StringVar(value="heartopia_22")
        fmt = ttk.Combobox(
            opts,
            textvariable=self.format_var,
            values=("heartopia_22", "heartopia_15", "letters"),
            state="readonly",
            width=16,
        )
        fmt.pack(side=tk.LEFT, padx=(8, 16))

        ttk.Label(opts, text="Transpose:").pack(side=tk.LEFT)
        self.transpose_var = tk.IntVar(value=0)
        ttk.Spinbox(
            opts,
            from_=-24,
            to=24,
            width=5,
            textvariable=self.transpose_var,
        ).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(opts, text="XML octave correction:").pack(side=tk.LEFT, padx=(16, 0))
        self.xml_octave_var = tk.StringVar(value="24")
        ttk.Combobox(
            opts,
            textvariable=self.xml_octave_var,
            values=("0", "12", "24"),
            state="readonly",
            width=5,
        ).pack(side=tk.LEFT, padx=(6, 0))
        
        btn_row = ttk.Frame(converter_tab)
        btn_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(btn_row, text="Convert", command=self._convert, style="Pink.TButton").pack(
            side=tk.LEFT
        )
        ttk.Button(btn_row, text="Copy", command=self._copy, style="Pink.TButton").pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(btn_row, text="Save as .txt…", command=self._save, style="Pink.TButton").pack(
            side=tk.LEFT, padx=(8, 0)
        )

        self.out = scrolledtext.ScrolledText(
            converter_tab,
            height=20,
            wrap="none",
            font=("Consolas", 11),
            bg=bg,
            fg="#1f2937",
            insertbackground=accent,
            selectbackground=accent_light,
            selectforeground="#1f2937",
            highlightthickness=1,
            highlightbackground=accent_light,
            highlightcolor=accent,
        )
        self.out.pack(fill=tk.BOTH, expand=True)

    def _build_keybindings_tab(self, parent) -> None:
        outer = ttk.Frame(parent)
        outer.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(outer, bg="#ffffff", highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas)

        content.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Label(
            content,
            text="Heartopia Keybindings",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        ttk.Label(
            content,
            text="Edit the actual note output keys for each supported MIDI pitch.",
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 16))

        sections = [
            ("22-key Top Row", "top_22", sorted(DEFAULT_MIDI_TOP.keys())),
            ("22-key Middle Row", "middle_22", sorted(DEFAULT_MIDI_MIDDLE_37.keys())),
            ("22-key Bottom Row", "bottom_22", sorted(DEFAULT_MIDI_BOTTOM_37.keys())),
            ("15-key Layout", "layout_15", sorted(DEFAULT_MIDI_15.keys())),
        ]

        for title, map_name, midi_list in sections:
            section = ttk.Frame(content)
            section.pack(fill=tk.X, pady=(0, 18))

            ttk.Label(
                section,
                text=title,
                font=("Segoe UI", 13, "bold"),
            ).pack(anchor="w", pady=(0, 8))

            for midi_num in midi_list:
                row = ttk.Frame(section)
                row.pack(fill=tk.X, pady=2)

                ttk.Label(
                    row,
                    text=f"MIDI {midi_num}",
                    width=12,
                    anchor="w",
                    font=("Segoe UI", 10),
                ).pack(side=tk.LEFT, padx=(0, 10))

                var = tk.StringVar(value=self.keymaps[map_name][midi_num])
                self.keybinding_vars[(map_name, midi_num)] = var

                entry = ttk.Entry(row, textvariable=var, width=8, font=("Consolas", 11))
                entry.pack(side=tk.LEFT)

        btn_row = ttk.Frame(content)
        btn_row.pack(fill=tk.X, pady=(10, 0))

        ttk.Button(
            btn_row,
            text="Apply all",
            style="Pink.TButton",
            command=self._apply_all_keybindings,
        ).pack(side=tk.LEFT)

        ttk.Button(
            btn_row,
            text="Reset defaults",
            style="Pink.TButton",
            command=self._reset_keybindings_defaults,
        ).pack(side=tk.LEFT, padx=(8, 0))

    def _load_keymaps_from_disk(self) -> None:
                if not self.settings_path.is_file():
                    return

                try:
                    raw = json.loads(self.settings_path.read_text(encoding="utf-8"))

                    for map_name, midi_map in raw.items():
                        if map_name not in self.keymaps:
                            continue

                        for midi_num_str, value in midi_map.items():
                            midi_num = int(midi_num_str)
                            if midi_num in self.keymaps[map_name]:
                                self.keymaps[map_name][midi_num] = str(value)

                except Exception as e:
                    print(f"WARNING: failed to load keymaps: {e}")
    def _save_keymaps_to_disk(self) -> None:
        try:
            serializable = {
                map_name: {str(midi_num): value for midi_num, value in midi_map.items()}
                for map_name, midi_map in self.keymaps.items()
            }

            self.settings_path.write_text(
                json.dumps(serializable, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            messagebox.showerror("Save error", f"Could not save keybindings:\n{e}")            

    def _open_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Open sheet music",
            filetypes=[
                ("Music & MIDI", "*.xml *.musicxml *.mxl *.mid *.midi"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self._path = Path(path)
        self.path_var.set(str(self._path))

    def _convert(self) -> None:
        if not self._path or not self._path.is_file():
            messagebox.showwarning("No file", "Choose a MusicXML or MIDI file first.")
            return

        try:
            print("DEBUG xml correction:", self.xml_octave_var.get())
            print("DEBUG format:", self.format_var.get())

            text = convert_file(
                self._path,
                output_mode=self.format_var.get(),
                transpose_semitones=int(self.transpose_var.get()),
                xml_octave_correction=int(self.xml_octave_var.get()),
                heartopia_rest_space=True,
                heartopia_compress_hold=False,
                heartopia_sheet_measures=False,
                grouping_mode="sheet_faithful",
                keymaps=self.keymaps,
            )
        except Exception as e:
            messagebox.showerror("Conversion failed", str(e))
            return

        self.out.delete("1.0", tk.END)
        self.out.insert(tk.END, text)

    def _copy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.out.get("1.0", tk.END))
        messagebox.showinfo("Copied", "Output copied to clipboard.")

    def _is_valid_binding(self, value: str) -> bool:
        return bool(value.strip())


    def _apply_all_keybindings(self) -> None:
        for (map_name, midi_num), var in self.keybinding_vars.items():
            value = var.get().strip()
            if not self._is_valid_binding(value):
                messagebox.showerror(
                    "Invalid keybinding",
                    f"Binding for MIDI {midi_num} cannot be empty."
                )
                return

        for (map_name, midi_num), var in self.keybinding_vars.items():
            self.keymaps[map_name][midi_num] = var.get().strip()

        self._save_keymaps_to_disk()
        messagebox.showinfo("Keybindings", "Custom keybindings applied.")


    def _reset_keybindings_defaults(self) -> None:
        self.keymaps = build_default_keymaps()

        for (map_name, midi_num), var in self.keybinding_vars.items():
            var.set(self.keymaps[map_name][midi_num])

        self._save_keymaps_to_disk()
        messagebox.showinfo("Keybindings", "Default keybindings restored.")
    
    def _save(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
        )
        if not path:
            return
        Path(path).write_text(self.out.get("1.0", tk.END), encoding="utf-8")
        messagebox.showinfo("Saved", f"Saved to {path}")


def main() -> None:
    if len(sys.argv) > 1:
        raise SystemExit(main_cli())
    app = SheetToLettersApp()
    app.mainloop()


if __name__ == "__main__":
    main()
