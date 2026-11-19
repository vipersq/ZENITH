"""ZENITH core: event model, local storage, paths and settings."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP_NAME = "ZENITH"
APP_VERSION = "1.0.0"
USER_AGENT = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) ZENITH-AnomalyWatch/{APP_VERSION}"


def resource_dir() -> Path:
    """Folder with bundled data (works inside a PyInstaller exe too)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "zenith" / "data"
    return Path(__file__).resolve().parent / "data"


def user_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), ".local", "share")
    p = Path(root) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- events

SOURCES = {
    # key: (label, colour)
    "ufo": ("UFO / UAP (NUFORC)", "#39ffb0"),
    "stalker": ("UFO Stalker (MUFON)", "#5ce1ff"),
    "reddit": ("Reddit UFO / HighStrangeness", "#c77dff"),
    "fireball": ("Fireballs (NASA/JPL)", "#ffab40"),
    "pursue": ("Declassified: Pentagon PURSUE", "#ffd166"),
    "tna": ("Declassified: UK National Archives", "#e9c46a"),
}
# declassified archives: not limited by the time window, listed in their own tab
ARCHIVE_SOURCES = {"pursue", "tna"}
# sources that can be toggled as map layers
MAP_LAYERS = [k for k in SOURCES if k != "tna"]


@dataclass
class Event:
    id: str
    source: str               # key in SOURCES
    title: str
    when: float               # unix seconds (UTC) of the event itself
    lat: float | None = None
    lon: float | None = None
    place: str = ""
    summary: str = ""
    url: str = ""
    approx: bool = False      # location is only approximate (state/country centroid)
    magnitude: float = 0.0    # used for symbol size (fireball kt, ...)
    posted: float = 0.0       # when the report was published
    extra: dict = field(default_factory=dict)

    @property
    def has_pos(self) -> bool:
        return self.lat is not None and self.lon is not None

    def when_str(self) -> str:
        if not self.when:
            return "?"
        return fmt_ts(self.when, "%Y-%m-%d %H:%M")

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Event":
        return Event(**d)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def ts_to_dt(ts: float, local: bool = True) -> datetime:
    """Unix time -> datetime that also works for dates before 1970 on Windows
    (datetime.fromtimestamp raises OSError there for negative values)."""
    try:
        if ts >= 86400:
            return datetime.fromtimestamp(ts) if local else datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        pass
    try:
        return _EPOCH + timedelta(seconds=float(ts))
    except (OverflowError, ValueError):
        return _EPOCH


def fmt_ts(ts: float, fmt: str = "%Y-%m-%d", local: bool = True) -> str:
    try:
        return ts_to_dt(ts, local).strftime(fmt)
    except ValueError:
        return "?"


def local_ts(dt: datetime) -> float:
    """naive local datetime -> unix time, safe for old dates on Windows"""
    try:
        return dt.timestamp()
    except (OSError, OverflowError, ValueError):
        return utc_ts(dt)


def utc_ts(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _EPOCH).total_seconds()


# --------------------------------------------------------------------------- storage

class Store:
    """Keeps every event we have seen, persisted to a JSON file between runs."""

    def __init__(self, path: Path | None = None):
        self.path = path or (user_dir() / "events.json")
        self.lock = threading.Lock()
        self.events: dict[str, Event] = {}
        self.load()

    def load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for d in raw:
                try:
                    e = Event.from_dict(d)
                    if e.source in SOURCES:      # drops layers that no longer exist (e.g. old quakes)
                        self.events[e.id] = e
                except TypeError:
                    pass
        except (OSError, ValueError):
            pass

    def save(self):
        with self.lock:
            data = [e.to_dict() for e in self.events.values()]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def merge(self, events: list[Event]) -> list[Event]:
        """Add events; return the ones that are new."""
        new = []
        with self.lock:
            for e in events:
                old = self.events.get(e.id)
                if old is None:
                    new.append(e)
                elif e.extra.get("full_text") is None and old.extra.get("full_text"):
                    e.extra["full_text"] = old.extra["full_text"]
                self.events[e.id] = e
        return new

    def prune(self, max_age_days: int = 120):
        cutoff = time.time() - max_age_days * 86400
        with self.lock:
            for k in [k for k, e in self.events.items()
                      if (e.source not in ARCHIVE_SOURCES and e.when and e.when < cutoff)
                      or e.extra.get("removed")]:
                del self.events[k]

    def all(self) -> list[Event]:
        with self.lock:
            return list(self.events.values())


class Settings:
    DEFAULTS = {
        "window_days": 30,
        "layers": {k: True for k in SOURCES},
        "notify": True,
        "online_geocoding": True,
        "sweep": True,
        "reddit_sightings_only": True,
        "geometry": None,
    }

    def __init__(self):
        self.path = user_dir() / "settings.json"
        self.data = json.loads(json.dumps(self.DEFAULTS))
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.update({k: v for k, v in saved.items() if k in self.DEFAULTS})
            for k in SOURCES:
                self.data["layers"].setdefault(k, True)
        except (OSError, ValueError):
            pass

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def save(self):
        try:
            self.path.write_text(json.dumps(self.data), encoding="utf-8")
        except OSError:
            pass
