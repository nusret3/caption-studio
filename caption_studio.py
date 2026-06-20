"""Caption Studio -- load a video + its .srt, preview captions burned over the
(9:16) video, tune the line-wrap width live, edit text/timing, and scrub on a
waveform timeline. Built for portrait social-video captions.

Layout:
  - menu bar  : File / Caption / Help
  - wide LEFT : subtitle table + text/timing editor
  - narrow RIGHT: portrait video preview (fills an aspect-locked box) + transport
  - bottom    : audio waveform + caption blocks + playhead (click/drag to seek)
  - Caption > Style…: pick system font, caption size, and line width (live).

Run:
    .\\venv\\Scripts\\python.exe caption_studio.py ["video.mp4"]

Open-source-friendly: pure PySide6 + numpy + ffmpeg (bundled via imageio-ffmpeg).
"""
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

from PySide6.QtCore import Qt, QUrl, QSizeF, QRectF, QThread, Signal, QSettings
from PySide6.QtGui import QFont, QPen, QBrush, QColor, QPainter, QAction, QImage, QPixmap, QPalette
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFileDialog, QSlider, QPushButton,
    QLabel, QHBoxLayout, QVBoxLayout, QFormLayout, QTableWidget, QTableWidgetItem,
    QSplitter, QGraphicsScene, QGraphicsView, QGraphicsSimpleTextItem, QStyle,
    QHeaderView, QAbstractItemView, QPlainTextEdit, QLineEdit, QFontComboBox,
    QGroupBox, QDialog, QMessageBox, QCheckBox, QColorDialog, QScrollArea,
)

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = "ffmpeg"


# ---------- wrapping (same logic as wrap_srt.py) ----------
def greedy_pack(words, width):
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
        return ""
    joined = " ".join(words)
    if len(joined) <= max_chars:
        return joined
    n = len(greedy_pack(words, max_chars))
    if n <= 1:
        return joined
    lo, hi, best = max(len(w) for w in words), len(joined), len(joined)
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(greedy_pack(words, mid)) <= n:
            best, hi = mid, mid - 1
        else:
            lo = mid + 1
    return "\n".join(greedy_pack(words, best))


def render_caption(text, max_chars):
    """Honor manual line breaks if present; otherwise auto-wrap to width."""
    return text if "\n" in text else wrap_text(text, max_chars)


# ---------- srt parse / write ----------
def ms_to_ts(ms):
    ms = max(0, int(ms))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def ts_to_ms(ts):
    ts = ts.strip().replace(".", ",")
    hms, mil = ts.split(",")
    h, m, s = hms.split(":")
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(mil)


def parse_srt(path):
    raw = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    cues = []
    for block in raw.split("\n\n"):
        lines = block.split("\n")
        ai = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ai is None:
            continue
        start, end = [p.strip().split()[0] for p in lines[ai].split("-->")]
        text = " ".join(l for l in lines[ai + 1:] if l.strip())
        cues.append({"start": ts_to_ms(start), "end": ts_to_ms(end), "text": text})
    cues.sort(key=lambda c: c["start"])
    return cues


def write_srt(path, cues, max_chars=None):
    out = []
    for i, c in enumerate(cues, 1):
        t = c["text"]
        if "\n" in t:                       # manual line breaks: keep verbatim
            text = t
        elif max_chars:
            text = wrap_text(t, max_chars)
        else:
            text = t
        out.append(f"{i}\n{ms_to_ts(c['start'])} --> {ms_to_ts(c['end'])}\n{text}\n")
    Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")


def probe_display_size(path):
    """Return (w, h) as DISPLAYED (rotation-corrected) via ffmpeg, or None.
    Phone videos are often stored landscape with a 90/270 rotation flag."""
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                             capture_output=True, text=True, errors="ignore").stderr
    except Exception:
        return None
    vid = next((l for l in out.splitlines() if "Video:" in l), None)
    if not vid:
        return None
    m = re.search(r"(\d{2,5})x(\d{2,5})", vid)
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    rot = 0
    rm = re.search(r"rotate\s*:\s*(-?\d+)", out)
    if rm:
        rot = int(rm.group(1))
    dm = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", out)
    if dm:
        rot = int(float(dm.group(1)))
    if abs(rot) % 180 == 90:
        w, h = h, w
    return w, h


# ---------- waveform extraction (background thread) ----------
class WaveformLoader(QThread):
    ready = Signal(object)

    def __init__(self, path, buckets=2_000_000):
        super().__init__()
        self.path = path
        self.buckets = buckets  # upper cap; actual ~1 bucket/ms

    def run(self):
        try:
            cmd = [FFMPEG, "-v", "error", "-i", self.path, "-ac", "1", "-ar", "8000", "-f", "s16le", "-"]
            data = subprocess.run(cmd, capture_output=True).stdout
            samples = np.frombuffer(data, np.int16).astype(np.float32) / 32768.0
            if samples.size == 0:
                self.ready.emit(np.zeros(0)); return
            # ~1 ms resolution (8000 Hz / 8 = 1000 buckets/sec), capped
            b = min(self.buckets, max(2000, samples.size // 8))
            idx = (np.arange(samples.size) * b // samples.size)
            peaks = np.zeros(b, dtype=np.float32)
            np.maximum.at(peaks, idx, np.abs(samples))
            mx = float(peaks.max()) or 1.0
            self.ready.emit(peaks / mx)
        except Exception:
            self.ready.emit(np.zeros(0))


class ThumbLoader(QThread):
    """Extract a filmstrip of small frames via ffmpeg (one call). Emits
    [(ms, QImage)] -- QImage is thread-safe; convert to QPixmap on the GUI side."""
    ready = Signal(object)

    def __init__(self, path, aspect, height=56):
        super().__init__()
        self.path = path
        self.aspect = aspect or (9 / 16)
        self.h = height

    def run(self):
        try:
            info = subprocess.run([FFMPEG, "-hide_banner", "-i", self.path],
                                  capture_output=True, text=True, errors="ignore").stderr
            dm = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", info)
            dur = (int(dm.group(1)) * 3600 + int(dm.group(2)) * 60 + float(dm.group(3))) if dm else 0.0
            interval = max(0.5, dur / 250) if dur > 0 else 1.0
            H = self.h
            W = max(2, int(round(H * self.aspect)))
            W += W % 2
            cmd = [FFMPEG, "-v", "error", "-i", self.path, "-vf",
                   f"fps=1/{interval},scale={W}:{H}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
            data = subprocess.run(cmd, capture_output=True).stdout
            frame = W * H * 3
            thumbs = []
            for i in range(len(data) // frame):
                buf = bytes(data[i * frame:(i + 1) * frame])
                img = QImage(buf, W, H, W * 3, QImage.Format_RGB888).copy()
                thumbs.append((int(i * interval * 1000), img))
            self.ready.emit(thumbs)
        except Exception:
            self.ready.emit([])


class TimelineContent(QWidget):
    """The scrollable strip: thumbnails (top), waveform (middle), caption blocks
    + playhead (bottom). Width = duration * pixels-per-second (zoom)."""
    seekRequested = Signal(int)
    zoomRequested = Signal(float)  # factor (panel anchors on the playhead)

    H = 150
    TH = 56

    def __init__(self):
        super().__init__()
        self.setFixedHeight(self.H)
        self.peaks = np.zeros(0)
        self.duration = 0
        self.position = 0
        self.cues = []
        self.thumbs = []     # [(ms, QPixmap)]
        self.pps = 50.0      # pixels per second

    def ms_to_x(self, ms): return ms / 1000.0 * self.pps
    def x_to_ms(self, x): return int(max(0.0, x) / max(1e-6, self.pps) * 1000)

    def set_peaks(self, p): self.peaks = p; self.update()
    def set_cues(self, c): self.cues = c; self.update()
    def set_thumbs(self, t): self.thumbs = t; self.update()
    def set_position(self, ms): self.position = ms; self.update()

    def mousePressEvent(self, e):
        if self.duration > 0:
            self.seekRequested.emit(self.x_to_ms(e.position().x()))

    def mouseMoveEvent(self, e):
        if self.duration > 0 and (e.buttons() & Qt.LeftButton):
            self.seekRequested.emit(self.x_to_ms(e.position().x()))

    def wheelEvent(self, e):
        if self.duration > 0:
            factor = 1.25 if e.angleDelta().y() > 0 else 0.8
            self.zoomRequested.emit(factor)
            e.accept()
        else:
            e.ignore()

    def paintEvent(self, e):
        p = QPainter(self)
        r = e.rect()
        x0, x1 = r.left(), r.right()
        p.fillRect(r, QColor(28, 28, 32))
        w = self.width()
        for ms, pix in self.thumbs:
            x = self.ms_to_x(ms)
            if x > x1 or x + pix.width() < x0:
                continue
            p.drawPixmap(int(x), 0, pix)
        wf_top, wf_h = self.TH + 4, self.H - self.TH - 24
        mid = wf_top + wf_h / 2
        n = len(self.peaks)
        if n and self.duration > 0:
            p.setPen(QColor(90, 170, 255))
            for x in range(max(0, x0), min(w, x1 + 1)):
                # take the peak over this pixel's time-range so detail survives zoom
                i0 = int((x / self.pps * 1000) / self.duration * n)
                i1 = int(((x + 1) / self.pps * 1000) / self.duration * n)
                i0 = min(n - 1, max(0, i0))
                i1 = min(n - 1, max(i0, i1))
                amp = float(self.peaks[i0:i1 + 1].max()) * (wf_h * 0.5)
                p.drawLine(x, int(mid - amp), x, int(mid + amp))
        if self.duration > 0:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 200, 60, 130))
            for c in self.cues:
                cx0, cx1 = self.ms_to_x(c["start"]), self.ms_to_x(c["end"])
                if cx1 < x0 or cx0 > x1:
                    continue
                p.drawRect(QRectF(cx0, self.H - 16, max(1.0, cx1 - cx0), 13))
            px = int(self.ms_to_x(self.position))
            p.setPen(QPen(QColor(255, 70, 70), 2))
            p.drawLine(px, 0, px, self.H)


class TimelinePanel(QWidget):
    """Zoom/scroll wrapper around TimelineContent with ± / Fit buttons."""
    seekRequested = Signal(int)
    MAX_PPS = 2000.0

    def __init__(self):
        super().__init__()
        self.content = TimelineContent()
        self.content.seekRequested.connect(self.seekRequested)
        self.content.zoomRequested.connect(self._zoom)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.content)
        self.scroll.setWidgetResizable(False)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setFixedHeight(TimelineContent.H + 18)
        self._fit_mode = True

        b_out = QPushButton("Zoom −"); b_out.clicked.connect(lambda: self._zoom(0.8))
        b_in = QPushButton("Zoom +"); b_in.clicked.connect(lambda: self._zoom(1.25))
        b_fit = QPushButton("Fit"); b_fit.clicked.connect(self.fit)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Timeline  (scroll wheel to zoom)")); bar.addStretch(1)
        bar.addWidget(b_out); bar.addWidget(b_in); bar.addWidget(b_fit)
        lay = QVBoxLayout(self); lay.setContentsMargins(4, 0, 4, 4)
        lay.addLayout(bar); lay.addWidget(self.scroll)

    def set_peaks(self, p): self.content.set_peaks(p)
    def set_cues(self, c): self.content.set_cues(c)
    def set_thumbs(self, t): self.content.set_thumbs(t)

    def set_duration(self, d):
        self.content.duration = d
        self.fit() if self._fit_mode else self._relayout()

    def set_position(self, ms):
        self.content.set_position(ms)
        self.scroll.ensureVisible(int(self.content.ms_to_x(ms)), 0, 80, 0)

    def _vp_w(self):
        return max(50, self.scroll.viewport().width())

    def _fit_pps(self):
        return self._vp_w() / max(0.001, self.content.duration / 1000.0)

    def _relayout(self):
        w = max(self._vp_w(), int(self.content.duration / 1000.0 * self.content.pps))
        self.content.setFixedWidth(w)
        self.content.update()

    def fit(self):
        if self.content.duration <= 0:
            return
        self._fit_mode = True
        self.content.pps = self._fit_pps()
        self._relayout()

    def _zoom(self, factor):
        if self.content.duration <= 0:
            return
        self._fit_mode = False
        self.content.pps = max(self._fit_pps(), min(self.MAX_PPS, self.content.pps * factor))
        self._relayout()
        self._center_playhead()

    def _center_playhead(self):
        x = self.content.ms_to_x(self.content.position)
        self.scroll.horizontalScrollBar().setValue(max(0, int(x - self._vp_w() / 2)))

    def resizeEvent(self, e):
        self.fit() if self._fit_mode else self._relayout()


# ---------- video view + aspect-locked container ----------
class VideoView(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        self.setBackgroundBrush(QBrush(Qt.transparent))
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # repaint the whole viewport each frame -- without this, the moving video
        # + repositioning caption leave stale rectangles ("ghost boxes").
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.video_item = QGraphicsVideoItem()
        self._scene.addItem(self.video_item)
        self.caption = QGraphicsSimpleTextItem()
        self.caption.setBrush(QBrush(Qt.white))
        self.caption.setZValue(10)
        self._scene.addItem(self.caption)

        self._native = QSizeF(1080, 1920)
        self._aspect = 1080 / 1920
        self._font_pct = 6.0
        self._family = "Arial"
        self._bold = True
        self._italic = False
        self._fill = QColor(255, 255, 255)
        self._box = None
        self._locked = False  # set once we have an authoritative (probed) size
        self.video_item.nativeSizeChanged.connect(self._on_native)
        self._apply_layout()

    def set_native(self, w, h):
        """Authoritative display size (from ffmpeg probe); overrides nativeSize."""
        self._locked = True
        self._native = QSizeF(w, h)
        self._aspect = w / h
        self._apply_layout()
        if self._box:
            self._box.relayout()

    def _on_native(self, size: QSizeF):
        if self._locked:
            return
        if size.isValid() and size.width() > 0:
            self._native = size
            self._aspect = size.width() / size.height()
            self._apply_layout()
            if self._box:
                self._box.relayout()

    def _apply_layout(self):
        w, h = self._native.width(), self._native.height()
        self.video_item.setSize(self._native)
        self._scene.setSceneRect(0, 0, w, h)
        self._restyle_caption()
        self.fitInView(0, 0, w, h, Qt.KeepAspectRatio)

    def _restyle_caption(self):
        px = max(8, int(self._native.height() * self._font_pct / 100.0))
        f = QFont(self._family)
        f.setBold(self._bold)
        f.setItalic(self._italic)
        f.setPixelSize(px)
        self.caption.setFont(f)
        self.caption.setBrush(QBrush(self._fill))
        self.caption.setPen(QPen(QColor(0, 0, 0), max(1.0, px * 0.07), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        self._reposition()

    def _reposition(self):
        r = self.caption.boundingRect()
        w, h = self._native.width(), self._native.height()
        self.caption.setPos((w - r.width()) / 2, h * 0.84 - r.height())

    def set_caption(self, text):
        self.caption.setText(text or "")
        self._reposition()

    def set_font_pct(self, pct):
        self._font_pct = pct
        self._restyle_caption()

    def set_font_family(self, name):
        self._family = name
        self._restyle_caption()

    def set_bold(self, b):
        self._bold = b
        self._restyle_caption()

    def set_italic(self, b):
        self._italic = b
        self._restyle_caption()

    def set_fill(self, color):
        self._fill = color
        self._restyle_caption()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fitInView(0, 0, self._native.width(), self._native.height(), Qt.KeepAspectRatio)


class AspectBox(QWidget):
    """Hosts the VideoView and gives it an exact rectangle matching the video
    aspect, centered -- so the frame fills its box instead of floating tiny."""

    def __init__(self, view: VideoView):
        super().__init__()
        self.setMinimumSize(200, 240)
        self._view = view
        view.setParent(self)
        view._box = self

    def relayout(self):
        a = self._view._aspect or (9 / 16)
        W, H = self.width(), self.height()
        if W <= 0 or H <= 0:
            return
        if W / H > a:
            h = H; w = int(h * a)
        else:
            w = W; h = int(w / a)
        self._view.setGeometry((W - w) // 2, (H - h) // 2, w, h)

    def resizeEvent(self, e):
        self.relayout()


# ---------- style dialog (font / size / width) ----------
class StyleDialog(QDialog):
    def __init__(self, studio):
        super().__init__(studio)
        self.setWindowTitle("Caption style")
        self.setModal(False)
        self.studio = studio

        self.font_combo = QFontComboBox()
        self.font_combo.setCurrentFont(QFont(studio.font_family))
        self.font_combo.currentFontChanged.connect(lambda f: studio.apply_font_family(f.family()))
        self.size_slider = QSlider(Qt.Horizontal); self.size_slider.setRange(2, 14); self.size_slider.setValue(int(studio.font_pct))
        self.size_lbl = QLabel(f"{int(studio.font_pct)}%")
        self.size_slider.valueChanged.connect(self._on_size)
        self.width_slider = QSlider(Qt.Horizontal); self.width_slider.setRange(8, 40); self.width_slider.setValue(studio.max_chars)
        self.width_lbl = QLabel(str(studio.max_chars))
        self.width_slider.valueChanged.connect(self._on_width)

        self.cb_bold = QCheckBox("Bold"); self.cb_bold.setChecked(studio.bold)
        self.cb_bold.toggled.connect(studio.apply_bold)
        self.cb_italic = QCheckBox("Italic"); self.cb_italic.setChecked(studio.italic)
        self.cb_italic.toggled.connect(studio.apply_italic)
        style_row = QHBoxLayout(); style_row.addWidget(self.cb_bold); style_row.addWidget(self.cb_italic); style_row.addStretch(1)
        self.color_btn = QPushButton("Text color…")
        self.color_btn.clicked.connect(self._pick_color)

        form = QFormLayout(self)
        form.addRow("Font", self.font_combo)
        form.addRow("Size", _with_label(self.size_slider, self.size_lbl))
        form.addRow("Line width", _with_label(self.width_slider, self.width_lbl))
        form.addRow("Style", _wrap(style_row))
        form.addRow("Color", self.color_btn)

    def _on_size(self, v):
        self.size_lbl.setText(f"{v}%")
        self.studio.apply_font_size(v)

    def _on_width(self, v):
        self.width_lbl.setText(str(v))
        self.studio.apply_width(v)

    def _pick_color(self):
        c = QColorDialog.getColor(self.studio.fill_color, self, "Caption text color")
        if c.isValid():
            self.studio.apply_fill(c)


class Studio(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Caption Studio")
        self.resize(1320, 880)
        self.cues = []
        self.srt_path = None
        self._seeking = False
        self._sync = False
        self._wave = None
        self._style_dlg = None
        self._hl_row = -1
        self._dirty = False        # unsaved caption edits?
        self._title_name = None    # current .srt name shown in the title

        # ---- persisted settings ----
        self.settings = QSettings("CaptionStudio", "CaptionStudio")
        s = self.settings
        self.font_family = s.value("style/family", "Arial")
        self.font_pct = float(s.value("style/size", 6.0))
        self.max_chars = int(s.value("style/width", 18))
        self.bold = s.value("style/bold", True, type=bool)
        self.italic = s.value("style/italic", False, type=bool)
        self.fill_color = QColor(s.value("style/fill", "#ffffff"))
        self.wrap_default = s.value("io/wrap", True, type=bool)
        self.last_dir = s.value("io/dir", "")
        self.recent = self._load_recent()      # most-recent-first list of video paths
        self.volume = int(s.value("audio/volume", 80))

        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self.audio.setVolume(self.volume / 100.0)
        self.player.setAudioOutput(self.audio)
        self.view = VideoView()
        self.view.set_font_family(self.font_family)
        self.view.set_font_pct(self.font_pct)
        self.view.set_bold(self.bold)
        self.view.set_italic(self.italic)
        self.view.set_fill(self.fill_color)
        self.player.setVideoOutput(self.view.video_item)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)

        self._build_menu()
        self.setCentralWidget(self._build_body())
        geo = self.settings.value("win/geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    # ---------- menu ----------
    def _build_menu(self):
        mb = self.menuBar()
        m_file = mb.addMenu("&File")
        m_file.addAction(_act(self, "Open &Video…", self.open_video, "Ctrl+O"))
        self.m_recent = m_file.addMenu("Open &Recent")
        self._rebuild_recent_menu()
        m_file.addAction(_act(self, "Open &SRT…", self.open_srt))
        m_file.addAction(_act(self, "&Save SRT…", self.save_srt, "Ctrl+S"))
        m_file.addSeparator()
        m_file.addAction(_act(self, "&Quit", self.close, "Ctrl+Q"))

        m_cap = mb.addMenu("&Caption")
        m_cap.addAction(_act(self, "&Style…", self.show_style))
        self.act_wrap = QAction("&Wrap on save", self, checkable=True)
        self.act_wrap.setChecked(self.wrap_default)
        m_cap.addAction(self.act_wrap)

        m_help = mb.addMenu("&Help")
        m_help.addAction(_act(self, "&About", self.show_about))

    # ---------- recent files ----------
    def _load_recent(self):
        rec = self.settings.value("io/recent", [])
        if isinstance(rec, str):          # QSettings collapses a 1-item list to a str
            rec = [rec] if rec else []
        return [p for p in rec if p]

    def _push_recent(self, path):
        path = str(path)
        self.recent = [path] + [p for p in self.recent if p != path]
        del self.recent[10:]              # keep the 10 most recent
        self.settings.setValue("io/recent", self.recent)
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        self.m_recent.clear()
        if not self.recent:
            self.m_recent.addAction("(none)").setEnabled(False)
            return
        for p in self.recent:
            act = QAction(p, self)
            act.triggered.connect(lambda _=False, path=p: self._open_recent(path))
            self.m_recent.addAction(act)
        self.m_recent.addSeparator()
        self.m_recent.addAction(_act(self, "Clear Recent", self._clear_recent))

    def _open_recent(self, path):
        if not Path(path).exists():
            QMessageBox.warning(self, "File not found",
                                f"{path}\n\nThis file no longer exists; removing it from Recent.")
            self.recent = [p for p in self.recent if p != path]
            self.settings.setValue("io/recent", self.recent)
            self._rebuild_recent_menu()
            return
        self.open_video(path)

    def _clear_recent(self):
        self.recent = []
        self.settings.setValue("io/recent", self.recent)
        self._rebuild_recent_menu()

    # ---------- body ----------
    def _build_body(self):
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Start", "End", "Dur", "Text"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 110); self.table.setColumnWidth(1, 110); self.table.setColumnWidth(2, 64)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_select)

        self.ed_start = QLineEdit(); self.ed_start.editingFinished.connect(self._apply_timing)
        self.ed_end = QLineEdit(); self.ed_end.editingFinished.connect(self._apply_timing)
        self.ed_text = QPlainTextEdit(); self.ed_text.setMaximumHeight(120)
        self.ed_text.setPlaceholderText("Caption text — press Enter to set your own line break; otherwise it auto-wraps")
        self.ed_text.textChanged.connect(self._on_text_edit)
        trow = QHBoxLayout(); trow.addWidget(self.ed_start); trow.addWidget(QLabel("→")); trow.addWidget(self.ed_end)
        form = QFormLayout(); form.addRow("Show → Hide", _wrap(trow))
        editor = QVBoxLayout(); editor.addLayout(form); editor.addWidget(QLabel("Text")); editor.addWidget(self.ed_text)
        ed_box = QGroupBox("Edit caption"); ed_box.setLayout(editor)

        left_split = QSplitter(Qt.Vertical)
        left_split.addWidget(self.table)
        left_split.addWidget(ed_box)
        left_split.setSizes([580, 200])

        # RIGHT: aspect-locked video + transport
        self.videobox = AspectBox(self.view)
        self.play_btn = QPushButton(); self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_btn.clicked.connect(self._toggle_play)
        self.pos_slider = QSlider(Qt.Horizontal)
        self.pos_slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.pos_slider.sliderReleased.connect(self._seek_release)
        self.pos_slider.sliderMoved.connect(self.player.setPosition)
        self.time_lbl = QLabel("00:00 / 00:00")
        self.mute_btn = QPushButton(); self.mute_btn.setCheckable(True)
        self.mute_btn.setToolTip("Mute")
        self.mute_btn.toggled.connect(self._on_mute)
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100); self.vol_slider.setValue(self.volume)
        self.vol_slider.setFixedWidth(110); self.vol_slider.setToolTip("Volume")
        self.vol_slider.valueChanged.connect(self._on_volume)
        self._update_volume_icon()
        transport = QHBoxLayout()
        transport.addWidget(self.play_btn); transport.addWidget(self.pos_slider, 1); transport.addWidget(self.time_lbl)
        transport.addWidget(self.mute_btn); transport.addWidget(self.vol_slider)
        right = QVBoxLayout()
        right.addWidget(self.videobox, 1)
        right.addLayout(transport)
        right_w = QWidget(); right_w.setLayout(right)

        split = QSplitter()
        split.addWidget(_wrap_widget(left_split))
        split.addWidget(right_w)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 1)
        split.setSizes([860, 440])

        self.timeline = TimelinePanel()
        self.timeline.seekRequested.connect(self.player.setPosition)

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(split, 1)
        lay.addWidget(self.timeline)
        return body

    # ---------- dialogs ----------
    def show_style(self):
        if self._style_dlg is None:
            self._style_dlg = StyleDialog(self)
        self._style_dlg.show()
        self._style_dlg.raise_()
        self._style_dlg.activateWindow()

    def show_about(self):
        QMessageBox.about(self, "About Caption Studio",
            "<b>Caption Studio</b><br>"
            "Preview &amp; fine-tune SRT captions over portrait video.<br><br>"
            "Built with PySide6 + numpy + ffmpeg. Free for amateurs to use and share.")

    # ---------- unsaved-changes tracking ----------
    def _update_title(self):
        base = f"Caption Studio — {self._title_name}" if self._title_name else "Caption Studio"
        self.setWindowTitle(base + (" •" if self._dirty else ""))

    def _set_dirty(self, flag):
        if self._dirty == flag:
            return
        self._dirty = flag
        self._update_title()

    def _confirm_discard(self):
        """Ask to save when there are unsaved edits. Return True if it's OK to
        proceed (nothing unsaved, saved, or discarded); False to cancel."""
        if not self._dirty or not self.cues:
            return True
        btn = QMessageBox.question(
            self, "Unsaved changes",
            "You have unsaved caption changes.\n\nSave them before continuing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save)
        if btn == QMessageBox.Cancel:
            return False
        if btn == QMessageBox.Save:
            self.save_srt()
            return not self._dirty       # save dialog cancelled -> still dirty -> abort
        return True                      # Discard

    # ---------- files ----------
    def open_video(self, path=None):
        if not self._confirm_discard():
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "Open video", self.last_dir, "Video (*.mp4 *.mkv *.mov *.webm *.avi);;All files (*)")
        if not path:
            return
        self.last_dir = str(Path(path).parent)
        self._push_recent(path)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.pause()
        sz = probe_display_size(path)
        if sz:
            self.view.set_native(*sz)
        self.timeline.set_peaks(np.zeros(0))
        self.timeline.set_thumbs([])
        self._wave = WaveformLoader(path)
        self._wave.ready.connect(self.timeline.set_peaks)
        self._wave.start()
        self._thumb = ThumbLoader(path, (sz[0] / sz[1]) if sz else (9 / 16))
        self._thumb.ready.connect(self._on_thumbs)
        self._thumb.start()
        stem = Path(path).with_suffix("")
        for cand in (Path(str(stem) + ".short.srt"), Path(str(stem) + ".srt")):
            if cand.exists():
                self._load_srt(cand)
                break

    def _on_thumbs(self, thumbs):
        self.timeline.set_thumbs([(ms, QPixmap.fromImage(img)) for ms, img in thumbs])

    def open_srt(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open SRT", self.last_dir, "Subtitles (*.srt *.vtt);;All files (*)")
        if path:
            self.last_dir = str(Path(path).parent)
            self._load_srt(Path(path))

    def _load_srt(self, path):
        self.cues = parse_srt(path)
        self.srt_path = str(path)
        self._title_name = path.name
        self._dirty = False
        self._update_title()
        self._fill_table()
        self.timeline.set_cues(self.cues)

    def _default_save_name(self):
        if self.srt_path:
            p = Path(self.srt_path)
            # when wrapping, default to a *.wrapped.srt so we don't clobber the source
            if self.act_wrap.isChecked():
                return str(p.with_name(p.stem + ".wrapped" + p.suffix))
            return str(p)
        base = Path(self.last_dir) if self.last_dir else Path.cwd()
        return str(base / "captions.srt")

    def save_srt(self):
        if not self.cues:
            return
        # getSaveFileName is a Save-As; the native dialog prompts before overwriting.
        path, _ = QFileDialog.getSaveFileName(self, "Save SRT as…", self._default_save_name(), "Subtitles (*.srt)")
        if not path:
            return
        if not path.lower().endswith(".srt"):
            path += ".srt"
        write_srt(path, self.cues, self.max_chars if self.act_wrap.isChecked() else None)
        self.srt_path = path
        self.last_dir = str(Path(path).parent)
        self._title_name = Path(path).name
        self._dirty = False
        self._update_title()

    # ---------- table / editor ----------
    def _fill_table(self):
        self._sync = True
        self._hl_row = -1
        self.table.setRowCount(len(self.cues))
        self.table.setVerticalHeaderLabels([str(i + 1) for i in range(len(self.cues))])
        for r, c in enumerate(self.cues):
            self._set_row(r, c)
        self._sync = False

    def _set_row(self, r, c):
        self.table.setItem(r, 0, QTableWidgetItem(ms_to_ts(c["start"])))
        self.table.setItem(r, 1, QTableWidgetItem(ms_to_ts(c["end"])))
        self.table.setItem(r, 2, QTableWidgetItem(f"{(c['end'] - c['start']) / 1000:.2f}"))
        self.table.setItem(r, 3, QTableWidgetItem(c["text"].replace("\n", " ")))

    def _cur_row(self):
        idx = self.table.currentRow()
        return idx if 0 <= idx < len(self.cues) else -1

    def _on_select(self):
        if self._sync:
            return
        r = self._cur_row()
        if r < 0:
            return
        c = self.cues[r]
        self._sync = True
        self.ed_start.setText(ms_to_ts(c["start"]))
        self.ed_end.setText(ms_to_ts(c["end"]))
        self.ed_text.setPlainText(c["text"])
        self._sync = False
        self.player.setPosition(c["start"])
        self.view.set_caption(render_caption(c["text"], self.max_chars))

    def _on_text_edit(self):
        if self._sync:
            return
        r = self._cur_row()
        if r < 0:
            return
        # preserve manual line breaks; normalize spaces within each line
        lines = [" ".join(l.split()) for l in self.ed_text.toPlainText().split("\n")]
        text = "\n".join(l for l in lines if l)
        self.cues[r]["text"] = text
        self._sync = True
        self.table.item(r, 3).setText(text.replace("\n", " "))
        self._sync = False
        self.view.set_caption(render_caption(text, self.max_chars))
        self._set_dirty(True)

    def _apply_timing(self):
        if self._sync:
            return
        r = self._cur_row()
        if r < 0:
            return
        try:
            start, end = ts_to_ms(self.ed_start.text()), ts_to_ms(self.ed_end.text())
        except Exception:
            return
        if end < start:
            end = start
        self.cues[r]["start"], self.cues[r]["end"] = start, end
        self._sync = True
        self._set_row(r, self.cues[r])
        self._sync = False
        self.timeline.set_cues(self.cues)
        self._set_dirty(True)

    # ---------- playback ----------
    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_state(self, state):
        playing = state == QMediaPlayer.PlayingState
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause if playing else QStyle.SP_MediaPlay))

    def _on_volume(self, v):
        self.volume = v
        self.audio.setVolume(v / 100.0)
        if v > 0 and self.mute_btn.isChecked():   # dragging up un-mutes
            self.mute_btn.setChecked(False)
        self._update_volume_icon()

    def _on_mute(self, muted):
        self.audio.setMuted(muted)
        self.mute_btn.setToolTip("Unmute" if muted else "Mute")
        self._update_volume_icon()

    def _update_volume_icon(self):
        silent = self.mute_btn.isChecked() or self.vol_slider.value() == 0
        icon = QStyle.SP_MediaVolumeMuted if silent else QStyle.SP_MediaVolume
        self.mute_btn.setIcon(self.style().standardIcon(icon))

    def _on_duration(self, d):
        self.pos_slider.setMaximum(max(0, d))
        self.timeline.set_duration(d)

    def _seek_release(self):
        self._seeking = False
        self.player.setPosition(self.pos_slider.value())

    def _on_position(self, ms):
        if not self._seeking:
            self.pos_slider.setValue(ms)
        self.timeline.set_position(ms)
        self.time_lbl.setText(f"{_clock(ms)} / {_clock(self.player.duration())}")
        idx = next((i for i, c in enumerate(self.cues) if c["start"] <= ms < c["end"]), -1)
        if idx < 0:  # fall back to inclusive end (last frame of last caption)
            idx = next((i for i, c in enumerate(self.cues) if c["start"] <= ms <= c["end"]), -1)
        self.view.set_caption(render_caption(self.cues[idx]["text"], self.max_chars) if idx >= 0 else "")
        self._highlight_row(idx)

    def _highlight_row(self, idx):
        if idx == self._hl_row:
            return
        self._set_row_bg(self._hl_row, None)
        self._set_row_bg(idx, QColor(46, 96, 62))
        self._hl_row = idx
        if idx >= 0 and self.table.item(idx, 0):
            self.table.scrollToItem(self.table.item(idx, 0), QAbstractItemView.EnsureVisible)

    def _set_row_bg(self, r, color):
        if 0 <= r < self.table.rowCount():
            brush = QBrush(color) if color else QBrush()
            for c in range(self.table.columnCount()):
                it = self.table.item(r, c)
                if it:
                    it.setBackground(brush)

    # ---------- style apply (called by StyleDialog) ----------
    def apply_width(self, v):
        self.max_chars = v
        r = self._cur_row()
        if r >= 0:
            self.view.set_caption(render_caption(self.cues[r]["text"], self.max_chars))
        else:
            self._on_position(self.player.position())

    def apply_font_size(self, v):
        self.font_pct = v
        self.view.set_font_pct(v)

    def apply_font_family(self, name):
        self.font_family = name
        self.view.set_font_family(name)

    def apply_bold(self, b):
        self.bold = b
        self.view.set_bold(b)

    def apply_italic(self, b):
        self.italic = b
        self.view.set_italic(b)

    def apply_fill(self, color):
        self.fill_color = color
        self.view.set_fill(color)

    # ---------- persistence ----------
    def closeEvent(self, e):
        if not self._confirm_discard():
            e.ignore()
            return
        s = self.settings
        s.setValue("style/family", self.font_family)
        s.setValue("style/size", self.font_pct)
        s.setValue("style/width", self.max_chars)
        s.setValue("style/bold", self.bold)
        s.setValue("style/italic", self.italic)
        s.setValue("style/fill", self.fill_color.name())
        s.setValue("io/wrap", self.act_wrap.isChecked())
        s.setValue("io/dir", self.last_dir)
        s.setValue("audio/volume", self.volume)
        s.setValue("win/geometry", self.saveGeometry())
        super().closeEvent(e)


# ---------- helpers ----------
def _act(parent, text, fn, shortcut=None):
    a = QAction(text, parent)
    a.triggered.connect(fn)
    if shortcut:
        a.setShortcut(shortcut)
    return a


def _wrap(layout):
    w = QWidget(); w.setLayout(layout); return w


def _wrap_widget(w):
    box = QWidget(); lay = QVBoxLayout(box); lay.setContentsMargins(0, 0, 0, 0); lay.addWidget(w); return box


def _with_label(slider, label):
    row = QHBoxLayout(); row.addWidget(slider, 1); row.addWidget(label)
    return _wrap(row)


def _clock(ms):
    s = int(ms) // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def apply_dark(app):
    app.setStyle("Fusion")
    text = QColor(220, 220, 220)
    p = QPalette()
    p.setColor(QPalette.Window, QColor(37, 37, 38))
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, QColor(30, 30, 30))
    p.setColor(QPalette.AlternateBase, QColor(45, 45, 48))
    p.setColor(QPalette.ToolTipBase, QColor(45, 45, 48))
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, QColor(45, 45, 48))
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, QColor(255, 80, 80))
    p.setColor(QPalette.Link, QColor(90, 160, 255))
    p.setColor(QPalette.Highlight, QColor(38, 100, 160))
    p.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.PlaceholderText, QColor(140, 140, 140))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(120, 120, 120))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(120, 120, 120))
    p.setColor(QPalette.Disabled, QPalette.WindowText, QColor(120, 120, 120))
    app.setPalette(p)


def main():
    app = QApplication(sys.argv)
    apply_dark(app)
    w = Studio()
    w.show()
    if len(sys.argv) > 1:
        w.open_video(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
