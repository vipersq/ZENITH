"""Self-drawn world map widget (no browser engine): land, borders, graticule,
day/night terminator, a scanning beam and the event markers."""
from __future__ import annotations

import gzip
import json
import math
import time
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QPointF, QRectF, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
                           QBrush, QLinearGradient, QRadialGradient)
from PySide6.QtWidgets import QWidget, QToolTip

from .core import SOURCES, Event, resource_dir

BG = QColor("#04080b")
LAND = QColor("#0b1f1a")
LAND_EDGE = QColor("#1f6b52")
BORDER = QColor(57, 255, 176, 40)
GRID = QColor(57, 255, 176, 22)
NIGHT = QColor(0, 0, 0, 105)
TEXT = QColor("#7dffcf")


def subsolar_point(t: float) -> tuple[float, float]:
    """Approximate sub-solar latitude/longitude (degrees) for unix time t."""
    dt = datetime.fromtimestamp(t, tz=timezone.utc)
    doy = dt.timetuple().tm_yday
    hours = dt.hour + dt.minute / 60 + dt.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hours - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    lon = -15 * (hours - 12 + eqt / 60)
    return math.degrees(decl), ((lon + 180) % 360) - 180


class MapView(QWidget):
    eventClicked = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(500, 300)
        self.setFocusPolicy(Qt.StrongFocus)
        with gzip.open(resource_dir() / "world.json.gz", "rt") as fh:
            world = json.load(fh)
        self.land = QPainterPath()
        self.land.setFillRule(Qt.WindingFill)
        for ring in world["land"]:
            self.land.addPolygon(QPolygonF([QPointF(x, -y) for x, y in ring]))
            self.land.closeSubpath()
        self.borders = QPainterPath()
        for ring in world["borders"]:
            self.borders.addPolygon(QPolygonF([QPointF(x, -y) for x, y in ring]))
        self.center_lon, self.center_lat = 0.0, 20.0
        self.scale = None          # pixels per degree; None => fit
        self.events: list[Event] = []
        self.fresh: dict[str, float] = {}
        self.selected: Event | None = None
        self.hover: Event | None = None
        self._bg: QPixmap | None = None
        self._bg_key = None
        self._drag = None
        self.sweep_enabled = True
        self._sweep_x = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)
        self.font_small = QFont("Consolas", 8)

    # ------------------------------------------------------------------ geometry
    def fit_scale(self) -> float:
        return max(self.width() / 360.0, self.height() / 170.0)

    def _scale(self) -> float:
        return self.scale or self.fit_scale()

    def to_screen(self, lon, lat, wrap=0.0) -> QPointF:
        s = self._scale()
        return QPointF(self.width() / 2 + (lon + wrap - self.center_lon) * s,
                       self.height() / 2 - (lat - self.center_lat) * s)

    def to_geo(self, x, y):
        s = self._scale()
        return (self.center_lon + (x - self.width() / 2) / s,
                self.center_lat - (y - self.height() / 2) / s)

    def _clamp(self):
        s = self._scale()
        half_h = self.height() / 2 / s
        lim = 85 - half_h
        self.center_lat = max(-lim, min(lim, self.center_lat)) if lim > 0 else 0
        self.center_lon = ((self.center_lon + 180) % 360) - 180

    # ------------------------------------------------------------------ data
    def set_events(self, events: list[Event]):
        self.events = [e for e in events if e.has_pos]
        self.update()

    def mark_fresh(self, events: list[Event]):
        now = time.time()
        for e in events:
            self.fresh[e.id] = now

    def select(self, e: Event | None, center=False):
        self.selected = e
        if e and e.has_pos and center:
            self.center_lon, self.center_lat = e.lon, e.lat
            if (self.scale or 0) < self.fit_scale() * 3:
                self.scale = self.fit_scale() * 3
            self._clamp()
            self._bg = None
        self.update()

    # ------------------------------------------------------------------ painting
    def _background(self) -> QPixmap:
        key = (self.width(), self.height(), round(self.center_lon, 4), round(self.center_lat, 4),
               round(self._scale(), 4), int(time.time() // 60))
        if self._bg is not None and self._bg_key == key:
            return self._bg
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(BG)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        s = self._scale()
        # vignette glow
        g = QRadialGradient(self.width() / 2, self.height() / 2, max(self.width(), self.height()) * 0.7)
        g.setColorAt(0, QColor(10, 40, 32))
        g.setColorAt(1, BG)
        p.fillRect(self.rect(), g)
        for wrap in (-360, 0, 360):
            p.save()
            p.translate(self.width() / 2 + (wrap - self.center_lon) * s, self.height() / 2 + self.center_lat * s)
            p.scale(s, s)
            p.setPen(QPen(LAND_EDGE, 1.2 / s))
            p.setBrush(LAND)
            p.drawPath(self.land)
            p.setPen(QPen(BORDER, 0.8 / s))
            p.setBrush(Qt.NoBrush)
            p.drawPath(self.borders)
            p.restore()
        # graticule
        p.setPen(QPen(GRID, 1, Qt.DotLine))
        step = 30 if s < 6 else (10 if s < 25 else 5)
        lon0, _ = self.to_geo(0, 0)
        lon1, _ = self.to_geo(self.width(), 0)
        for lon in range(int(math.floor(lon0 / step) * step), int(lon1) + step, step):
            x = self.to_screen(lon, 0).x()
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        for lat in range(-90, 91, step):
            y = self.to_screen(0, lat).y()
            p.drawLine(QPointF(0, y), QPointF(self.width(), y))
        p.setPen(QPen(QColor(57, 255, 176, 70), 1))
        y = self.to_screen(0, 0).y()
        p.drawLine(QPointF(0, y), QPointF(self.width(), y))
        self._draw_night(p)
        p.end()
        self._bg, self._bg_key = pm, key
        return pm

    def _draw_night(self, p: QPainter):
        dec, slon = subsolar_point(time.time())
        dec = dec if abs(dec) > 0.1 else 0.1
        lon0, _ = self.to_geo(0, 0)
        lon1, _ = self.to_geo(self.width(), 0)
        pts = []
        step = max(0.5, (lon1 - lon0) / 300)
        lon = lon0
        while lon <= lon1 + step:
            ha = math.radians(lon - slon)
            lat = math.degrees(math.atan(-math.cos(ha) / math.tan(math.radians(dec))))
            pts.append(self.to_screen(lon, lat))
            lon += step
        edge = self.to_screen(0, -90 if dec > 0 else 90).y()
        poly = QPolygonF(pts + [QPointF(pts[-1].x(), edge), QPointF(pts[0].x(), edge)])
        p.setPen(Qt.NoPen)
        p.setBrush(NIGHT)
        p.drawPolygon(poly)
        p.setPen(QPen(QColor(255, 200, 80, 60), 1, Qt.DashLine))
        p.drawPolyline(QPolygonF(pts))
        sun = self.to_screen(slon, dec)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 210, 90, 140))
        p.drawEllipse(sun, 4, 4)

    def _marker_radius(self, e: Event) -> float:
        if e.source == "fireball":
            return 3 + min(8.0, math.log10(1 + e.magnitude * 10) * 3)
        if e.source in ("pursue", "tna"):
            return 4.5
        return 3.5

    def _iter_screen(self):
        w = self.width()
        for e in self.events:
            for wrap in (-360, 0, 360):
                pt = self.to_screen(e.lon, e.lat, wrap)
                if -20 <= pt.x() <= w + 20 and -20 <= pt.y() <= self.height() + 20:
                    yield e, pt

    def paintEvent(self, _):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._background())
        p.setRenderHint(QPainter.Antialiasing)
        now = time.time()
        sweep = self._sweep_x
        for e, pt in self._iter_screen():
            col = QColor(SOURCES[e.source][1])
            r = self._marker_radius(e)
            # brighten markers the beam has just passed
            dist = (sweep - pt.x()) % max(1, self.width())
            glow = max(0.0, 1 - dist / 140) if self.sweep_enabled else 0.0
            alpha = 150 if e.approx else 220
            col.setAlpha(min(255, int(alpha + 35 * glow)))
            if e.source == "ufo":
                p.setPen(QPen(col, 1.3))
                p.setBrush(QColor(col.red(), col.green(), col.blue(), 50 + int(80 * glow)))
                p.drawEllipse(pt, r + 0.5, r + 0.5)
                p.drawLine(QPointF(pt.x() - r - 3, pt.y()), QPointF(pt.x() + r + 3, pt.y()))
            elif e.source == "fireball":
                p.setPen(QPen(col, 1.2))
                p.setBrush(col)
                star = QPolygonF([QPointF(pt.x() + math.cos(a) * (r if i % 2 == 0 else r / 2.4),
                                          pt.y() + math.sin(a) * (r if i % 2 == 0 else r / 2.4))
                                  for i, a in enumerate(math.pi / 4 * k - math.pi / 2 for k in range(8))])
                p.drawPolygon(star)
            elif e.source == "stalker":
                p.setPen(QPen(col, 1.4))
                p.setBrush(QColor(col.red(), col.green(), col.blue(), 50 + int(80 * glow)))
                p.drawPolygon(QPolygonF([QPointF(pt.x(), pt.y() - r - 1.5), QPointF(pt.x() + r + 1.5, pt.y()),
                                         QPointF(pt.x(), pt.y() + r + 1.5), QPointF(pt.x() - r - 1.5, pt.y())]))
            elif e.source in ("pursue", "tna"):
                p.setPen(QPen(col, 1.2))
                p.setBrush(QColor(col.red(), col.green(), col.blue(), 70))
                p.drawRect(QRectF(pt.x() - r, pt.y() - r - 1, 2 * r, 2 * r + 2))
                p.drawLine(QPointF(pt.x() - r + 1.5, pt.y() - 1), QPointF(pt.x() + r - 1.5, pt.y() - 1))
                p.drawLine(QPointF(pt.x() - r + 1.5, pt.y() + 1.5), QPointF(pt.x() + r - 1.5, pt.y() + 1.5))
            else:
                p.setPen(QPen(col, 1.2, Qt.DashLine if e.approx else Qt.SolidLine))
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(pt.x() - r, pt.y() - r, 2 * r, 2 * r))
            born = self.fresh.get(e.id)
            if born and now - born < 120:
                phase = (now - born) % 2.0 / 2.0
                ring = QColor(col)
                ring.setAlpha(int(255 * (1 - phase)))
                p.setPen(QPen(ring, 2))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(pt, r + 4 + phase * 22, r + 4 + phase * 22)
        if self.sweep_enabled:
            grad = QLinearGradient(sweep - 140, 0, sweep, 0)
            grad.setColorAt(0, QColor(57, 255, 176, 0))
            grad.setColorAt(1, QColor(57, 255, 176, 38))
            p.fillRect(QRectF(sweep - 140, 0, 140, self.height()), grad)
            p.setPen(QPen(QColor(57, 255, 176, 120), 1))
            p.drawLine(QPointF(sweep, 0), QPointF(sweep, self.height()))
        for e, tag in ((self.hover, False), (self.selected, True)):
            if e is None or not e.has_pos:
                continue
            for wrap in (-360, 0, 360):
                pt = self.to_screen(e.lon, e.lat, wrap)
                if not (0 <= pt.x() <= self.width()):
                    continue
                c = QColor("#ffffff") if tag else QColor(255, 255, 255, 140)
                p.setPen(QPen(c, 1))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(pt, 13, 13)
                if tag:
                    p.drawLine(QPointF(pt.x() - 22, pt.y()), QPointF(pt.x() - 14, pt.y()))
                    p.drawLine(QPointF(pt.x() + 14, pt.y()), QPointF(pt.x() + 22, pt.y()))
                    p.drawLine(QPointF(pt.x(), pt.y() - 22), QPointF(pt.x(), pt.y() - 14))
                    p.drawLine(QPointF(pt.x(), pt.y() + 14), QPointF(pt.x(), pt.y() + 22))
        # HUD
        p.setFont(self.font_small)
        p.setPen(TEXT)
        lon, lat = self.to_geo(self.width() / 2, self.height() / 2)
        zoom = self._scale() / self.fit_scale()
        p.drawText(10, self.height() - 10,
                   f"CENTER {lat:+07.2f} {lon:+08.2f}   ZOOM ×{zoom:.1f}   "
                   f"MARKERS {len(self.events)}   [wheel: zoom • drag: pan • double-click: reset]")
        p.end()

    def _tick(self):
        if self.sweep_enabled and self.isVisible():
            self._sweep_x = (self._sweep_x + max(2.0, self.width() / 220)) % max(1, self.width() + 140)
            self.update()
        elif self.fresh:
            self.update()

    # ------------------------------------------------------------------ interaction
    def _hit(self, pos) -> Event | None:
        best, bd = None, 12.0
        for e, pt in self._iter_screen():
            d = math.hypot(pt.x() - pos.x(), pt.y() - pos.y())
            if d < bd:
                best, bd = e, d
        return best

    def wheelEvent(self, ev):
        factor = 1.25 if ev.angleDelta().y() > 0 else 0.8
        pos = ev.position()
        lon, lat = self.to_geo(pos.x(), pos.y())
        new = max(self.fit_scale(), min(self.fit_scale() * 60, self._scale() * factor))
        self.scale = new
        # keep the point under the cursor fixed
        self.center_lon = lon - (pos.x() - self.width() / 2) / new
        self.center_lat = lat + (pos.y() - self.height() / 2) / new
        self._clamp()
        self.update()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag = (ev.position(), self.center_lon, self.center_lat, False)

    def mouseMoveEvent(self, ev):
        if self._drag and ev.buttons() & Qt.LeftButton:
            start, lon, lat, _ = self._drag
            d = ev.position() - start
            if abs(d.x()) + abs(d.y()) > 3:
                s = self._scale()
                self.center_lon = lon - d.x() / s
                self.center_lat = lat + d.y() / s
                self._clamp()
                self._drag = (start, lon, lat, True)
                self.setCursor(Qt.ClosedHandCursor)
                self.update()
            return
        h = self._hit(ev.position())
        if h is not self.hover:
            self.hover = h
            self.setCursor(Qt.PointingHandCursor if h else Qt.ArrowCursor)
            if h:
                QToolTip.showText(ev.globalPosition().toPoint(),
                                  f"<b>{h.title}</b><br>{h.place}<br>{h.when_str()}"
                                  + ("<br><i>approximate location</i>" if h.approx else ""), self)
            else:
                QToolTip.hideText()
            self.update()

    def mouseReleaseEvent(self, ev):
        dragged = self._drag[3] if self._drag else False
        self._drag = None
        self.setCursor(Qt.ArrowCursor)
        if ev.button() == Qt.LeftButton and not dragged:
            h = self._hit(ev.position())
            if h:
                self.selected = h
                self.eventClicked.emit(h)
                self.update()

    def mouseDoubleClickEvent(self, ev):
        self.scale = None
        self.center_lon, self.center_lat = 0.0, 20.0
        self._clamp()
        self.update()

    def resizeEvent(self, ev):
        self._clamp()
        super().resizeEvent(ev)
