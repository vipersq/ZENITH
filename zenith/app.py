"""ZENITH main window."""
from __future__ import annotations

import html
import sys
import time
import traceback
from datetime import datetime, timezone

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, QUrl, Signal, QByteArray
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QPushButton,
                               QSplitter, QSystemTrayIcon, QTabWidget, QTextBrowser, QVBoxLayout,
                               QWidget, QMenu)

from . import sources
from .core import APP_NAME, APP_VERSION, ARCHIVE_SOURCES, fmt_ts, MAP_LAYERS, SOURCES, Event, Settings, Store, resource_dir
from .geocode import Geocoder
from .mapview import MapView

# refresh intervals (seconds)
SCHEDULE = {"ufo": 60 * 60, "stalker": 10 * 60, "reddit": 10 * 60,
            "fireball": 6 * 3600, "kp": 15 * 60, "pursue": 24 * 3600, "tna": 24 * 3600}
SOURCE_NAMES = {"ufo": "NUFORC", "stalker": "UFO Stalker", "reddit": "Reddit",
                "fireball": "NASA", "kp": "NOAA Kp", "pursue": "PURSUE", "tna": "UK Archives"}
ICONS = {"ufo": "◯", "stalker": "◇", "reddit": "□", "fireball": "✦", "pursue": "▣", "tna": "▤"}

STYLE = """
* { font-family: Consolas, 'Cascadia Mono', 'DejaVu Sans Mono', monospace; font-size: 12px; color: #b8ffe4; }
QMainWindow, QWidget#root { background: #04080b; }
QFrame#panel { background: #071114; border: 1px solid #12392e; border-radius: 4px; }
QLabel#title { font-size: 20px; font-weight: bold; color: #39ffb0; letter-spacing: 4px; }
QLabel#sub { color: #3fa883; letter-spacing: 2px; }
QLabel#hdr { color: #39ffb0; font-weight: bold; letter-spacing: 2px; padding-top: 4px; }
QLabel#clock { color: #7dffcf; font-size: 13px; }
QLineEdit, QComboBox { background: #030607; border: 1px solid #1f6b52; padding: 4px; border-radius: 3px; }
QComboBox QAbstractItemView { background: #071114; selection-background-color: #12392e; }
QPushButton { background: #0b2a22; border: 1px solid #1f6b52; padding: 5px 10px; border-radius: 3px; color: #7dffcf; }
QPushButton:hover { background: #12392e; border-color: #39ffb0; }
QPushButton:disabled { color: #2e5c4d; border-color: #12392e; }
QListWidget { background: #030607; border: 1px solid #12392e; outline: none; }
QListWidget::item { padding: 5px 4px; border-bottom: 1px solid #0b1f1a; }
QListWidget::item:selected { background: #12392e; color: #ffffff; }
QTextBrowser { background: #030607; border: 1px solid #12392e; padding: 6px; }
QTabWidget::pane { border: 1px solid #12392e; top: -1px; }
QTabBar::tab { background: #030607; border: 1px solid #12392e; padding: 5px 12px; color: #3fa883; letter-spacing: 1px; }
QTabBar::tab:selected { background: #0b2a22; color: #39ffb0; border-bottom-color: #0b2a22; }
QCheckBox { spacing: 6px; }
QCheckBox::indicator { width: 12px; height: 12px; border: 1px solid #1f6b52; background: #030607; }
QCheckBox::indicator:checked { background: #39ffb0; }
QSplitter::handle { background: #0b1f1a; }
QStatusBar { background: #030607; color: #3fa883; border-top: 1px solid #12392e; }
QScrollBar:vertical { background: #030607; width: 9px; }
QScrollBar::handle:vertical { background: #1f6b52; min-height: 20px; border-radius: 3px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QToolTip { background: #071114; color: #b8ffe4; border: 1px solid #39ffb0; }
"""

WINDOWS = [("24 hours", 1), ("3 days", 3), ("7 days", 7), ("30 days", 30), ("90 days", 90), ("1 year", 365)]


def make_icon() -> QIcon:
    path = resource_dir() / "zenith.png"
    if path.exists():
        return QIcon(str(path))
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor("#39ffb0"), 3))
    p.drawEllipse(6, 6, 52, 52)
    p.drawEllipse(22, 22, 20, 20)
    p.drawLine(32, 2, 32, 14)
    p.end()
    return QIcon(pm)


# ----------------------------------------------------------------------------- workers

class _Signals(QObject):
    done = Signal(str, object)
    failed = Signal(str, str, int)


class _HumanBridge(QObject):
    """Lets the browser worker thread tell the UI that the bot check wants a click."""
    changed = Signal(bool)

    def emit_changed(self, v: bool):
        self.changed.emit(bool(v))


class Job(QRunnable):
    def __init__(self, key, fn, *args):
        super().__init__()
        self.key, self.fn, self.args = key, fn, args
        self.signals = _Signals()

    def run(self):
        try:
            res = self.fn(*self.args)
            self.signals.done.emit(self.key, res)
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            self.signals.failed.emit(self.key, msg[:300], int(getattr(e, "retry_after", 0) or 0))


# ----------------------------------------------------------------------------- window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} // Anomaly Watch  v{APP_VERSION}")
        self.setWindowIcon(make_icon())
        self.settings = Settings()
        self.store = Store()
        self.store.prune()
        self.geocoder = Geocoder(online=self.settings["online_geocoding"])
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(4)
        self.status: dict[str, tuple[str, float, str]] = {}   # key -> (state, ts, msg)
        self.running: set[str] = set()
        self.next_due = {k: 0.0 for k in SCHEDULE}
        self.first_load = {k: True for k in SCHEDULE}
        self.kp = None
        self._jobs = []
        self.current: Event | None = None
        self.human = _HumanBridge()
        self.human.changed.connect(self._web_human)
        self._build()
        geo = self.settings["geometry"]
        if geo:
            self.restoreGeometry(QByteArray.fromHex(geo.encode()))
        else:
            self.resize(1500, 900)
        self._ensure_on_screen()
        self._setup_tray()
        self.refresh_view()
        self.tick = QTimer(self)
        self.tick.timeout.connect(self._on_tick)
        self.tick.start(1000)
        QTimer.singleShot(300, self._on_tick)

    # ------------------------------------------------------------------ UI
    def _build(self):
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(8, 8, 8, 4)
        lay.setSpacing(6)

        top = QHBoxLayout()
        t = QLabel("ZENITH", objectName="title")
        s = QLabel("// UFO / UAP SIGHTINGS & ANOMALY MONITOR", objectName="sub")
        top.addWidget(t)
        top.addWidget(s)
        top.addStretch(1)
        self.kp_label = QLabel("Kp —", objectName="clock")
        self.kp_label.setToolTip("NOAA planetary geomagnetic index (0–9). Kp ≥ 5 = storm, aurora possible.")
        top.addWidget(self.kp_label)
        top.addSpacing(20)
        self.clock = QLabel(objectName="clock")
        top.addWidget(self.clock)
        top.addSpacing(12)
        self.btn_refresh = QPushButton("⟳ REFRESH")
        self.btn_refresh.clicked.connect(self.refresh_all)
        top.addWidget(self.btn_refresh)
        lay.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        lay.addWidget(split, 1)

        # map panel
        map_frame = QFrame(objectName="panel")
        ml = QVBoxLayout(map_frame)
        ml.setContentsMargins(2, 2, 2, 2)
        self.map = MapView()
        self.map.sweep_enabled = self.settings["sweep"]
        self.map.eventClicked.connect(lambda e: self.show_event(e, from_map=True))
        ml.addWidget(self.map)
        split.addWidget(map_frame)

        # side panel
        side = QFrame(objectName="panel")
        side.setMinimumWidth(360)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(8, 8, 8, 8)
        sl.setSpacing(5)

        sl.addWidget(QLabel("LAYERS", objectName="hdr"))
        self.layer_boxes = {}
        for key in MAP_LAYERS:
            label, color = SOURCES[key]
            cb = QCheckBox(label)
            cb.setChecked(self.settings["layers"].get(key, True))
            cb.setStyleSheet(f"QCheckBox {{ color: {color}; }} QCheckBox::indicator:checked {{ background: {color}; }}")
            cb.toggled.connect(self._layers_changed)
            self.layer_boxes[key] = cb
            sl.addWidget(cb)

        row = QHBoxLayout()
        row.addWidget(QLabel("TIME WINDOW"))
        self.window_combo = QComboBox()
        for label, days in WINDOWS:
            self.window_combo.addItem(label, days)
        idx = next((i for i, (_, d) in enumerate(WINDOWS) if d == self.settings["window_days"]), 3)
        self.window_combo.setCurrentIndex(idx)
        self.window_combo.currentIndexChanged.connect(self._window_changed)
        row.addWidget(self.window_combo, 1)
        sl.addLayout(row)

        row2 = QHBoxLayout()
        self.cb_sweep = QCheckBox("Scanner")
        self.cb_sweep.setChecked(self.settings["sweep"])
        self.cb_sweep.toggled.connect(self._sweep_changed)
        self.cb_notify = QCheckBox("Notifications")
        self.cb_notify.setChecked(self.settings["notify"])
        self.cb_notify.toggled.connect(lambda v: self._set("notify", v))
        self.cb_unexplained = QCheckBox("Unexplained only")
        self.cb_unexplained.setToolTip("Hide NUFORC reports that have been given an explanation (Starlink, aircraft, etc.)")
        self.cb_unexplained.toggled.connect(self.refresh_view)
        row2.addWidget(self.cb_sweep)
        row2.addWidget(self.cb_notify)
        row2.addWidget(self.cb_unexplained)
        sl.addLayout(row2)
        self.cb_reddit_sight = QCheckBox("Reddit: sightings only (no discussions or questions)")
        self.cb_reddit_sight.setToolTip("Keeps posts flaired Sighting/UFO/Anomalies, posts with a photo/video "
                                        "or with a recognised location. Posts removed by moderators are always hidden.")
        self.cb_reddit_sight.setChecked(self.settings["reddit_sightings_only"])
        self.cb_reddit_sight.toggled.connect(lambda v: (self._set("reddit_sightings_only", v), self.refresh_view()))
        sl.addWidget(self.cb_reddit_sight)

        vsplit = QSplitter(Qt.Vertical)
        self.tabs = QTabWidget()
        # --- live reports
        live = QWidget()
        ll = QVBoxLayout(live)
        ll.setContentsMargins(4, 6, 4, 4)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search: shape, place, word… (e.g. triangle, orb, Texas)")
        self.search.textChanged.connect(self.refresh_view)
        ll.addWidget(self.search)
        self.count_label = QLabel(objectName="sub")
        ll.addWidget(self.count_label)
        self.feed = QListWidget()
        self.feed.setWordWrap(True)
        self.feed.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.feed.currentItemChanged.connect(self._feed_selected)
        ll.addWidget(self.feed, 1)
        self.tabs.addTab(live, "LIVE")
        # --- declassified files
        arch = QWidget()
        al = QVBoxLayout(arch)
        al.setContentsMargins(4, 6, 4, 4)
        ar = QHBoxLayout()
        self.arch_search = QLineEdit()
        self.arch_search.setPlaceholderText("Search files: Rendlesham, FBI, video, 1952…")
        self.arch_search.textChanged.connect(self.refresh_archive)
        self.arch_filter = QComboBox()
        self.arch_filter.addItem("All", "")
        self.arch_filter.addItem("Pentagon PURSUE (USA)", "pursue")
        self.arch_filter.addItem("National Archives (UK)", "tna")
        self.arch_filter.currentIndexChanged.connect(self.refresh_archive)
        ar.addWidget(self.arch_search, 1)
        ar.addWidget(self.arch_filter)
        al.addLayout(ar)
        self.arch_count = QLabel(objectName="sub")
        al.addWidget(self.arch_count)
        self.arch_list = QListWidget()
        self.arch_list.setWordWrap(True)
        self.arch_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.arch_list.currentItemChanged.connect(self._feed_selected)
        al.addWidget(self.arch_list, 1)
        self.tabs.addTab(arch, "DECLASSIFIED FILES")
        vsplit.addWidget(self.tabs)

        det = QWidget()
        dl = QVBoxLayout(det)
        dl.setContentsMargins(0, 0, 0, 0)
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(False)
        self.detail.setOpenLinks(False)
        dl.addWidget(self.detail, 1)
        br = QHBoxLayout()
        self.btn_full = QPushButton("FULL REPORT")
        self.btn_full.clicked.connect(self.load_full)
        self.btn_center = QPushButton("SHOW ON MAP")
        self.btn_center.clicked.connect(lambda: self.current and self.map.select(self.current, center=True))
        self.btn_src = QPushButton("SOURCE ↗")
        self.btn_src.setToolTip("Opens the source page / document in your default browser")
        self.btn_src.clicked.connect(lambda: self.current and self.current.url and
                                     QDesktopServices.openUrl(QUrl(self.current.url)))
        for b in (self.btn_full, self.btn_center, self.btn_src):
            br.addWidget(b)
        dl.addLayout(br)
        vsplit.addWidget(det)
        vsplit.setSizes([480, 320])
        sl.addWidget(vsplit, 1)
        split.addWidget(side)
        split.setStretchFactor(0, 1)
        split.setSizes([1100, 420])

        self.statusBar().showMessage("Start…")
        self._set_detail_buttons(None)
        self.detail.setHtml(self._welcome_html())

    def _welcome_html(self):
        return ("<h3 style='color:#39ffb0'>ZENITH v%s</h3>"
                "<p>Near real-time monitor of UFO/UAP sightings, anomalies and declassified government files.</p>"
                "<p>◯ <b style='color:#39ffb0'>NUFORC</b> — civilian UFO reports (hourly, fetched via Edge in the background).<br>"
                "◇ <b style='color:#5ce1ff'>UFO Stalker</b> — live reports with exact position and details (every 10 min).<br>"
                "□ <b style='color:#c77dff'>Reddit</b> — r/UFOs, r/UFOB, r/UAP, r/HighStrangeness (every 10 min, "
                "location guessed from the title).<br>"
                "✦ <b style='color:#ffab40'>NASA/JPL</b> — fireballs detected by US government sensors.<br>"
                "▣ <b style='color:#ffd166'>Pentagon PURSUE</b> — declassified UAP files and videos (war.gov/UFO); "
                "those with an incident location are on the map.<br>"
                "▤ <b style='color:#e9c46a'>UK National Archives</b> — declassified UFO files of the British MoD "
                "(DECLASSIFIED FILES tab).</p>"
                "<p>The darker band on the map is night; the yellow dot is the subsolar point. "
                "Click a marker or a report in the list.</p>") % APP_VERSION

    def _ensure_on_screen(self):
        """If the saved position is off every connected screen (monitor unplugged, small side display,
        minimised when closed), put the window centred on the main screen."""
        if self.isMinimized():
            self.showNormal()
        frame = self.frameGeometry()
        best = 0
        for scr in QApplication.screens():
            inter = scr.availableGeometry().intersected(frame)
            best = max(best, inter.width() * inter.height())
        main = QApplication.primaryScreen().availableGeometry()
        if best < 0.5 * frame.width() * frame.height() or frame.width() < 600 or frame.height() < 400:
            w = min(1500, main.width() - 60)
            h = min(900, main.height() - 60)
            self.resize(w, h)
            self.move(main.x() + (main.width() - w) // 2, main.y() + (main.height() - h) // 2)

    def _setup_tray(self):
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(make_icon(), self)
            self.tray.setToolTip("ZENITH // Anomaly Watch")
            menu = QMenu()
            a = QAction("Show window", self)
            a.triggered.connect(lambda: (self.showNormal(), self.activateWindow()))
            menu.addAction(a)
            q = QAction("Quit", self)
            q.triggered.connect(QApplication.quit)
            menu.addAction(q)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(lambda r: r == QSystemTrayIcon.Trigger and
                                        (self.showNormal(), self.activateWindow()))
            self.tray.messageClicked.connect(lambda: (self.showNormal(), self.activateWindow()))
            self.tray.show()

    # ------------------------------------------------------------------ settings
    def _set(self, k, v):
        self.settings[k] = v
        self.settings.save()

    def _layers_changed(self):
        self.settings["layers"] = {k: cb.isChecked() for k, cb in self.layer_boxes.items()}
        self.settings.save()
        self.refresh_view()

    def _window_changed(self):
        self._set("window_days", self.window_combo.currentData())
        self.refresh_view()

    def _sweep_changed(self, v):
        self.map.sweep_enabled = v
        self._set("sweep", v)
        self.map.update()

    # ------------------------------------------------------------------ fetching
    def _on_tick(self):
        now = datetime.now()
        utc = datetime.now(timezone.utc)
        self.clock.setText(f"{now:%H:%M:%S} LOC   {utc:%H:%M:%S} UTC")
        t = time.time()
        for key, every in SCHEDULE.items():
            if t >= self.next_due[key] and key not in self.running:
                self.start_fetch(key)
                self.next_due[key] = t + every
        self._update_status()

    def refresh_all(self):
        for key in SCHEDULE:
            if key not in self.running:
                self.start_fetch(key)
                self.next_due[key] = time.time() + SCHEDULE[key]

    def start_fetch(self, key):
        fn = {
            "ufo": lambda: sources.fetch_nuforc_browser(
                self.geocoder, 3 if self.settings["window_days"] > 30 else 2, self.human.emit_changed),
            "reddit": lambda: sources.fetch_reddit(self.geocoder),
            "stalker": sources.fetch_stalker,
            "pursue": lambda: sources.fetch_pursue(self.geocoder),
            "tna": sources.fetch_tna,
            "fireball": sources.fetch_fireballs,
            "kp": sources.fetch_kp,
        }[key]
        job = Job(key, fn)
        job.signals.done.connect(self._fetched)
        job.signals.failed.connect(self._failed)
        self._jobs.append(job)
        self.running.add(key)
        self.status[key] = ("…", time.time(), "fetching")
        self.pool.start(job)
        self._update_status()

    def _web_human(self, waiting):
        if waiting:
            self.statusBar().showMessage("NUFORC asks for confirmation — tick the box in the browser window "
                                         "that just appeared. It will close by itself.")
            if self.tray and self.settings["notify"]:
                self.tray.showMessage("ZENITH", "NUFORC asks for a one-time verification click "
                                      "in the browser window.", QSystemTrayIcon.Information, 8000)

    def _fetched(self, key, result):
        self.running.discard(key)
        self._jobs = [j for j in self._jobs if j.key != key]
        if key == "kp":
            self.kp = result
            self._show_kp()
            self.status[key] = ("OK", time.time(), f"Kp {result['kp']:.1f}")
            self._update_status()
            return
        new = self.store.merge(result)
        self.status[key] = ("OK", time.time(), f"{len(result)} records, {len(new)} new")
        if not self.first_load[key] and new:
            self.map.mark_fresh(new)
            self._notify(key, new)
        self.first_load[key] = False
        try:
            self.store.save()
        except OSError:
            pass
        self.geocoder.flush()
        self.refresh_view()

    def _failed(self, key, msg, retry_after=0):
        self.running.discard(key)
        self._jobs = [j for j in self._jobs if j.key != key]
        self.status[key] = ("ERROR", time.time(), msg)
        # rate-limited: wait as long as the server asked; otherwise retry a bit sooner than usual
        delay = retry_after if retry_after else min(SCHEDULE[key], 180)
        self.next_due[key] = time.time() + delay
        self._update_status()

    def _update_status(self):
        parts = []
        for key in SCHEDULE:
            st = self.status.get(key)
            if not st:
                parts.append(f"{SOURCE_NAMES[key]}: —")
                continue
            state, ts, msg = st
            ago = int(time.time() - ts)
            dot = {"OK": "●", "ERROR": "✖", "…": "◌"}[state]
            when = f"{ago // 60}m ago" if ago >= 60 else f"{ago}s ago"
            parts.append(f"{dot} {SOURCE_NAMES[key]} {when}")
        self.statusBar().showMessage("   ".join(parts))
        tips = "\n".join(f"{SOURCE_NAMES[k]}: {v[0]} — {v[2]}" for k, v in self.status.items())
        self.statusBar().setToolTip(tips)

    def _show_kp(self):
        kp = self.kp["kp"]
        col = "#39ffb0" if kp < 4 else ("#ffd166" if kp < 5 else "#ff4d6d")
        word = "quiet" if kp < 4 else ("unsettled" if kp < 5 else "GEOMAGNETIC STORM")
        self.kp_label.setText(f"<span style='color:{col}'>Kp {kp:.1f} • {word}</span>")

    def _notify(self, key, new: list[Event]):
        if not (self.tray and self.settings["notify"]):
            return
        if key in ("ufo", "stalker"):
            recent = [e for e in new if e.when > time.time() - 30 * 86400]
            if not recent:
                return
            src = "NUFORC" if key == "ufo" else "UFO Stalker"
            title = (f"ZENITH: {len(recent)} new UFO reports ({src})" if len(recent) > 1
                     else f"ZENITH: new UFO report ({src})")
            body = "\n".join(f"{e.title} ({e.when_str()})" for e in recent[:3])
        elif key in ARCHIVE_SOURCES:
            title = f"ZENITH: {len(new)} new declassified documents"
            body = "\n".join(e.title for e in new[:3])
        elif key == "fireball":
            title, body = "ZENITH: new fireball in the NASA database", "\n".join(f"{e.title} • {e.place}" for e in new[:3])
        else:
            return
        self.tray.showMessage(title, body, QSystemTrayIcon.Information, 8000)

    # ------------------------------------------------------------------ view
    def visible_events(self) -> list[Event]:
        days = self.settings["window_days"]
        cutoff = time.time() - days * 86400
        q = self.search.text().strip().lower()
        layers = {k for k, cb in self.layer_boxes.items() if cb.isChecked()}
        only_unexpl = self.cb_unexplained.isChecked()
        reddit_sight = self.cb_reddit_sight.isChecked()
        out = []
        for e in self.store.all():
            if e.source not in layers:
                continue
            if e.source in ARCHIVE_SOURCES:
                if not e.has_pos:
                    continue          # archive docs without a place live only in their tab
            elif e.when < cutoff:
                continue
            if only_unexpl and e.source == "ufo" and e.extra.get("explanation"):
                continue
            if e.source == "reddit":
                if e.extra.get("removed"):
                    continue
                if reddit_sight and not e.extra.get("sighting", True):
                    continue
            if q and q not in (e.title + " " + e.place + " " + e.summary).lower():
                continue
            out.append(e)
        out.sort(key=lambda e: e.when, reverse=True)
        return out

    def refresh_view(self):
        evs = self.visible_events()
        self.map.set_events(evs)
        counts = {k: 0 for k in SOURCES}
        for e in evs:
            counts[e.source] += 1
        for k, cb in self.layer_boxes.items():
            cb.setText(f"{SOURCES[k][0]}  [{counts[k]}]")
        live = [e for e in evs if e.source not in ARCHIVE_SOURCES]
        on_map = sum(1 for e in live if e.has_pos)
        self.count_label.setText(f"{len(live)} reports in window • {on_map} on map")
        cur_id = self.current.id if self.current else None
        self.feed.blockSignals(True)
        self.feed.clear()
        for e in live[:800]:
            it = QListWidgetItem(f"{ICONS[e.source]} {e.title}\n   {e.when_str()}  •  {e.place[:60]}")
            it.setForeground(QColor(SOURCES[e.source][1]))
            it.setData(Qt.UserRole, e)
            if e.source == "ufo" and e.extra.get("explanation"):
                it.setToolTip(f"NUFORC explanation: {e.extra['explanation']}")
            self.feed.addItem(it)
            if e.id == cur_id:
                self.feed.setCurrentItem(it)
        self.feed.blockSignals(False)
        self.refresh_archive()

    def refresh_archive(self):
        q = self.arch_search.text().strip().lower()
        only = self.arch_filter.currentData()
        docs = [e for e in self.store.all() if e.source in ARCHIVE_SOURCES
                and (not only or e.source == only)
                and (not q or q in (e.title + " " + e.place + " " + e.summary + " "
                                    + str(e.extra.get("agency", ""))).lower())]
        docs.sort(key=lambda e: (e.when or 0), reverse=True)
        n_p = sum(1 for e in docs if e.source == "pursue")
        self.arch_count.setText(f"{len(docs)} documents • Pentagon {n_p} • UK {len(docs) - n_p}")
        cur_id = self.current.id if self.current else None
        self.arch_list.blockSignals(True)
        self.arch_list.clear()
        for e in docs:
            when = e.extra.get("covering") or (fmt_ts(e.when, "%Y-%m-%d", local=False)
                                                if e.when else "date unknown")
            where = e.place or ("UK" if e.source == "tna" else "")
            it = QListWidgetItem(f"{e.title}\n   {when}" + (f"  •  {where[:50]}" if where else ""))
            it.setForeground(QColor(SOURCES[e.source][1]))
            it.setData(Qt.UserRole, e)
            self.arch_list.addItem(it)
            if e.id == cur_id:
                self.arch_list.setCurrentItem(it)
        self.arch_list.blockSignals(False)

    def _feed_selected(self, item, _prev):
        if item:
            self.show_event(item.data(Qt.UserRole), from_map=False)

    def _set_detail_buttons(self, e: Event | None):
        self.btn_full.setEnabled(bool(e and e.source == "ufo" and "sighting" in e.url))
        self.btn_center.setEnabled(bool(e and e.has_pos))
        self.btn_src.setEnabled(bool(e and e.url))

    def show_event(self, e: Event, from_map: bool):
        self.current = e
        self._set_detail_buttons(e)
        if from_map:
            lst = self.arch_list if e.source in ARCHIVE_SOURCES else self.feed
            self.tabs.setCurrentIndex(1 if e.source in ARCHIVE_SOURCES else 0)
            for i in range(lst.count()):
                if lst.item(i).data(Qt.UserRole).id == e.id:
                    lst.blockSignals(True)
                    lst.setCurrentRow(i)
                    lst.scrollToItem(lst.item(i))
                    lst.blockSignals(False)
                    break
        else:
            self.map.select(e, center=False)
        self.map.selected = e
        self.map.update()
        self.detail.setHtml(self._event_html(e))

    def _event_html(self, e: Event) -> str:
        col = SOURCES[e.source][1]
        esc = lambda s: html.escape(str(s or "")).replace("\n", "<br>")  # noqa: E731
        rows = [("When", e.when_str() + (" (local time at the site)" if e.extra.get("local_time") else ""))]
        if e.place:
            rows.append(("Where", e.place))
        if e.has_pos:
            rows.append(("Position", f"{e.lat:.3f}, {e.lon:.3f}" + (" — approximate" if e.approx else "")))
        if e.source == "ufo":
            rows.append(("Shape", e.extra.get("shape")))
            if e.posted:
                rows.append(("Published", fmt_ts(e.posted, "%Y-%m-%d")))
            if e.extra.get("media"):
                rows.append(("Media", e.extra["media"]))
            if e.extra.get("explanation"):
                rows.append(("Explanation", f"<b style='color:#ffd166'>{esc(e.extra['explanation'])}</b>"))
        if e.source == "stalker":
            x = e.extra
            for label, key in (("Type", "type"), ("Shape", "shape"), ("Colour", "colour"), ("Sound", "sound"),
                               ("Flight path", "flight"), ("Duration", "duration"), ("Objects", "objects"),
                               ("Witnesses", "witnesses"), ("Distance", "distance"), ("Altitude", "altitude"),
                               ("Weather", "weather"), ("Context", "context"), ("Features", "features"),
                               ("Rating", "rating")):
                if x.get(key) not in (None, "", 0):
                    rows.append((label, x[key]))
            if x.get("media"):
                rows.append(("Media", f"{x['media']} file(s) — see the source"))
            if e.posted:
                rows.append(("Submitted", fmt_ts(e.posted, "%Y-%m-%d %H:%M")))
        if e.source == "pursue":
            x = e.extra
            rows = [("Agency", x.get("agency")), ("Kind", {"PDF": "PDF document", "VID": "video",
                                                            "IMG": "image"}.get(x.get("kind"), x.get("kind"))),
                    ("Incident date", x.get("incident_date") or "unknown"),
                    ("Location", e.place or "unknown"), ("Released", x.get("release"))]
            if x.get("video"):
                rows.append(("Video", x["video"]))
            if x.get("redaction"):
                rows.append(("Redacted", x["redaction"]))
            rows.append(("Data source", x.get("origin")))
        if e.source == "tna":
            x = e.extra
            rows = [("Reference", x.get("reference")), ("Covering dates", x.get("covering")),
                    ("Department", x.get("department")), ("Opened", x.get("opened")),
                    ("Held at", "The National Archives, Kew")]
        if e.source == "reddit":
            if e.extra.get("flair"):
                rows.append(("Flair", e.extra["flair"]))
            rows.append(("Score", e.extra.get("score", 0)))
            if e.extra.get("comments"):
                rows.append(("Comments", e.extra["comments"]))
            if e.extra.get("media"):
                rows.append(("Media", "photo / video in the post"))
        table = "".join(f"<tr><td style='color:#3fa883;padding-right:10px'>{k}</td><td>"
                        f"{v if k == 'Explanation' else esc(v)}</td></tr>" for k, v in rows)
        body = e.extra.get("full_text") or e.summary
        note = ""
        if e.source in ARCHIVE_SOURCES:
            note = ("<p style='color:#3fa883'><i>Click SOURCE ↗ to open the original document"
                    + (" (many DEFE 24 files can be downloaded free as PDF)." if e.source == "tna" else ".")
                    + "</i></p>")
        if e.source == "ufo" and not e.extra.get("full_text"):
            note = "<p style='color:#3fa883'><i>This is a summary. Click FULL REPORT to load the full witness account.</i></p>"
        return (f"<h3 style='color:{col};margin:0'>{esc(e.title)}</h3>"
                f"<table style='margin:6px 0'>{table}</table>"
                f"<div style='color:#d8fff0'>{esc(body) or '<i>no description</i>'}</div>{note}")

    def load_full(self):
        e = self.current
        if not e:
            return
        self.btn_full.setEnabled(False)
        self.btn_full.setText("LOADING…")
        job = Job(e.id, sources.fetch_nuforc_full_browser, e.url, self.human.emit_changed)
        job.signals.done.connect(self._full_ok)
        job.signals.failed.connect(self._full_bad)
        self._jobs.append(job)
        self.pool.start(job)

    def _full_ok(self, key, txt):
        self._jobs = [j for j in self._jobs if j.key != key]
        ev = self.store.events.get(key)
        if ev:
            ev.extra["full_text"] = txt
            self.store.save()
        self.btn_full.setText("FULL REPORT")
        if self.current and self.current.id == key:
            self.current.extra["full_text"] = txt
            self.detail.setHtml(self._event_html(self.current))
        self._set_detail_buttons(self.current)

    def _full_bad(self, key, msg, _retry=0):
        self._jobs = [j for j in self._jobs if j.key != key]
        self.btn_full.setText("FULL REPORT")
        self._set_detail_buttons(self.current)
        self.detail.append(f"<p style='color:#ff4d6d'>Could not load the report: {html.escape(msg)}</p>")

    def closeEvent(self, ev):
        if not self.isMinimized():
            self.settings["geometry"] = bytes(self.saveGeometry().toHex()).decode()
        self.settings.save()
        try:
            self.store.save()
        except OSError:
            pass
        self.geocoder.flush()
        if self.tray:
            self.tray.hide()
        super().closeEvent(ev)


def _app_dir():
    """Folder next to ZENITH.exe (frozen) or the project folder (python main.py)."""
    from pathlib import Path
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _crash_box(text: str):
    log = _app_dir() / "zenith_crash.log"
    try:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n{text}\n")
    except OSError:
        pass
    msg = ("ZENITH could not start.\n\nDetails were saved to:\n" + str(log) + "\n\n" + text[-1500:])
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "ZENITH — error", 0x10)
            return
        except Exception:  # noqa: BLE001
            pass
    try:
        sys.__stderr__.write(msg + "\n")
    except Exception:  # noqa: BLE001
        pass


def main():
    # pythonw / windowed exe: there is no console, so send prints to a log file
    if sys.stderr is None or sys.stdout is None:
        try:
            from .core import user_dir
            fh = open(user_dir() / "zenith.log", "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stdout or fh
            sys.stderr = sys.stderr or fh
        except OSError:
            import io
            sys.stdout = sys.stdout or io.StringIO()
            sys.stderr = sys.stderr or io.StringIO()
    try:
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("vipersq.zenith.anomalywatch")
            except Exception:  # noqa: BLE001
                pass
        app = QApplication(sys.argv)
        app.setApplicationName(APP_NAME)
        app.setStyleSheet(STYLE)
        app.setFont(QFont("Consolas", 9))
        w = MainWindow()
        w.show()
        w.raise_()
        w.activateWindow()
    except BaseException:  # noqa: BLE001
        _crash_box(traceback.format_exc())
        raise SystemExit(1)
    sys.exit(app.exec())
