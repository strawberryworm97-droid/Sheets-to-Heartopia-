"""
Convert digital sheet music (MusicXML, MIDI, MuseData, etc.) to letter-style note names
similar to beginner sites like NoobNotes: C D E with optional octave and accidentals.

Does not read scanned PDFs or images — use MuseScore or similar to export MusicXML/MIDI first.
"""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from collections import defaultdict
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Iterable

from music21 import chord, converter, interval, note, stream

from heartopia_maps import pitch_to_heartopia_key

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


def _quarter_beats(el) -> int:
    ql = float(el.duration.quarterLength)
    if ql <= 0:
        ql = 0.25
    b = int(round(ql))
    return max(1, b)


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
    """Return (list of (pitch, beats), rest_beats)."""
    pitch_dur: list[tuple] = []
    rest_beats = 0
    for el in grp:
        b = _quarter_beats(el)
        if isinstance(el, note.Rest):
            rest_beats = max(rest_beats, b)
        elif isinstance(el, chord.Chord):
            for p in el.pitches:
                pitch_dur.append((p, b))
        elif isinstance(el, note.Note):
            pitch_dur.append((el.pitch, b))
    return pitch_dur, rest_beats


def _heartopia_cluster_token(
    grp: list,
    layout: str,
    transpose_semitones: int,
    compress_double_hold: bool,
    *,
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
            max_b = max(b for _, b in pitch_dur)
            holds = max(0, max_b - 1)
            hold_str = _hold_suffix(holds, compress_double_hold)

        uniq_p: list = []
        seen: set[int] = set()
        for p, _ in sorted(pitch_dur, key=lambda x: x[0].midi):
            if p.midi not in seen:
                seen.add(p.midi)
                uniq_p.append(p)

        keys = [pitch_to_heartopia_key(p, layout, transpose_semitones) for p in uniq_p]
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
        )

    lines: list[str] = []
    for label, meas in _iter_measure_streams(s):
        if grouping_mode == "sheet_faithful":
            groups = _group_by_same_onset(meas)
            include_holds = True
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
) -> str:
    """Single line Heartopia output."""
    if grouping_mode == "sheet_faithful":
        groups = _group_by_same_onset(s)
        include_holds = True
    elif grouping_mode == "playable":
        groups = _group_by_active_beats(s)
        include_holds = False
    else:
        raise ValueError(f"Unknown grouping_mode: {grouping_mode}")

    tokens: list[str] = []
    for grp in groups:
        t = _heartopia_cluster_token(
            grp,
            layout,
            transpose_semitones,
            compress_double_hold,
            rest_as_token=True,
            rest_one_beat_space=rest_one_beat_space,
            include_holds=include_holds,
        )
        if t:
            tokens.append(t)

    return _join_heartopia_tokens(tokens)


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

        row1 = ttk.Frame(main)
        row1.pack(fill=tk.X, pady=(0, 8))

        ttk.Button(row1, text="Open file…", command=self._open_file, style="Pink.TButton").pack(
            side=tk.LEFT
        )
        self.path_var = tk.StringVar(value="No file loaded")
        ttk.Label(row1, textvariable=self.path_var, wraplength=480).pack(
            side=tk.LEFT, padx=(12, 0)
        )

        opts = ttk.Frame(main)
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

        self.grouping_label_var = tk.StringVar(value="Playable")
        self.grouping_mode_map = {
            "Sheet faithful": "sheet_faithful",
            "Playable": "playable",
        }

        ttk.Label(opts, text="Grouping:").pack(side=tk.LEFT, padx=(16, 0))
        ttk.Combobox(
            opts,
            textvariable=self.grouping_label_var,
            values=("Sheet faithful", "Playable"),
            state="readonly",
            width=14,
        ).pack(side=tk.LEFT, padx=(6, 0))
        
        btn_row = ttk.Frame(main)
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
            main,
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

        ttk.Label(
            main,
            text=(
                "Heartopia 22 = full in-game keyboard. Same attack time = one cluster (e.g. 3r); double spaces "
                "between clusters. Sequential notes stay separate. > holds."
            ),
            font=("Segoe UI", 8),
            foreground="#9ca3af",
        ).pack(anchor=tk.W, pady=(8, 0))

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
            grouping_mode = self.grouping_mode_map[self.grouping_label_var.get()]

            print("DEBUG grouping:", grouping_mode)
            print("DEBUG xml correction:", self.xml_octave_var.get())
            print("DEBUG format:", self.format_var.get())

            text = convert_file(
                self._path,
                output_mode=self.format_var.get(),
                transpose_semitones=int(self.transpose_var.get()),
                xml_octave_correction=int(self.xml_octave_var.get()),
                heartopia_rest_space=True,
                heartopia_compress_hold=False,
                heartopia_sheet_measures=True,
                grouping_mode=grouping_mode,
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
