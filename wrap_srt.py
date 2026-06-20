"""Post-process a subtitle file: wrap each caption's text to a character-per-line
budget, balancing the line widths and never splitting a word. Works on .srt and
.vtt, edits in place. Cheap to re-run with a different budget to tune width on
9:16 portrait video -- no need to re-run Whisper.

    python wrap_srt.py "video.short.srt" [max_chars=18]

- max_chars is a soft budget: respected when possible; a single word longer than
  the budget is left whole on its own line (overflows rather than getting cut).
- Re-running is safe: existing line breaks are flattened and re-wrapped, so you
  can sweep max_chars values freely.
"""
import sys
from pathlib import Path


def greedy_pack(words, width):
    """First-fit pack words into lines of <= `width` chars. A word longer than
    `width` takes its own line (overflow). Returns a list of line strings."""
    lines, cur = [], ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def wrap_text(text, max_chars):
    words = text.split()
    if not words:
        return text
    joined = " ".join(words)
    if len(joined) <= max_chars:
        return joined
    n = len(greedy_pack(words, max_chars))  # minimal lines under the budget
    if n <= 1:
        return joined  # one over-long word; leave it whole
    # Balance: smallest width that still packs into n lines -> even-length lines.
    lo, hi, best = max(len(w) for w in words), len(joined), len(joined)
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(greedy_pack(words, mid)) <= n:
            best, hi = mid, mid - 1
        else:
            lo = mid + 1
    return "\n".join(greedy_pack(words, best))


def process(path, max_chars):
    raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    out, changed = [], 0
    for block in raw.split("\n\n"):
        lines = block.split("\n")
        ai = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ai is None:  # e.g. the WEBVTT header block -- leave untouched
            out.append(block)
            continue
        text = " ".join(l for l in lines[ai + 1:] if l.strip())
        out.append("\n".join(lines[: ai + 1] + [wrap_text(text, max_chars)]))
        changed += 1
    path.write_text("\n\n".join(out) + "\n", encoding="utf-8")
    return changed


def main():
    if len(sys.argv) < 2:
        print("usage: wrap_srt.py <file.srt|file.vtt> [max_chars=18]", file=sys.stderr)
        return 1
    path = Path(sys.argv[1])
    max_chars = int(sys.argv[2]) if len(sys.argv) > 2 else 18
    n = process(path, max_chars)
    print(f"Wrapped {n} captions in {path.name} at max_chars={max_chars}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
