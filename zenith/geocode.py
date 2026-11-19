"""Offline-first geocoder: GeoNames towns > state centroid > country centroid,
with an optional, throttled and cached OpenStreetMap (Nominatim) fallback."""
from __future__ import annotations

import gzip
import json
import threading
import time
import unicodedata

import requests

from .core import resource_dir, user_dir, USER_AGENT

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
US_CODES = set(US_STATES.values())
CA_CODES = {"AB", "BC", "MB", "NB", "NL", "NS", "ON", "PE", "QC", "SK", "YT", "NT", "NU"}
# regions / seas that appear in government UAP files (approximate centres)
REGIONS = {
    "persian gulf": (26.9, 51.5), "arabian gulf": (26.9, 51.5), "gulf of oman": (24.5, 58.5),
    "strait of hormuz": (26.6, 56.4), "arabian sea": (16.0, 63.0), "gulf of aden": (12.5, 47.5),
    "red sea": (20.0, 38.5), "mediterranean sea": (35.0, 18.0), "aegean sea": (38.5, 25.0),
    "black sea": (43.4, 34.3), "baltic sea": (57.5, 19.5), "north sea": (56.0, 3.0),
    "east china sea": (29.0, 125.0), "south china sea": (12.0, 113.0), "sea of japan": (40.0, 135.0),
    "philippine sea": (20.0, 130.0), "pacific ocean": (15.0, -150.0), "atlantic ocean": (30.0, -40.0),
    "indian ocean": (-10.0, 75.0), "gulf of mexico": (25.0, -90.0), "caribbean sea": (15.0, -75.0),
    "western united states": (40.0, -115.0), "southern united states": (32.5, -90.0),
    "eastern united states": (38.0, -78.0), "middle east": (29.0, 45.0), "north america": (45.0, -100.0),
    "indo pacom": (5.0, 130.0), "east coast": (38.0, -75.0), "west coast": (37.0, -122.0),
}
GB_PARTS = {"england": "ENG", "scotland": "SCT", "wales": "WLS", "northern ireland": "NIR"}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = "".join(ch if ch.isalnum() or ch == " " else " " for ch in s)
    return " ".join(s.split())


class Geocoder:
    def __init__(self, online: bool = True):
        self.online = online
        d = resource_dir()
        with gzip.open(d / "cities.json.gz", "rt", encoding="utf-8") as fh:
            rows = json.load(fh)
        self.by_name: dict[str, list] = {}
        for name, cc, a1, lat, lon, pop in rows:
            self.by_name.setdefault(name, []).append((cc, a1, lat, lon, pop))
        # "saint x" vs "st x" both indexed
        for name in list(self.by_name):
            if name.startswith("saint "):
                self.by_name.setdefault("st " + name[6:], self.by_name[name])
        idx = json.loads((d / "geo_index.json").read_text(encoding="utf-8"))
        self.countries = idx["countries"]
        self.admin1 = idx["admin1"]
        self.cache_path = user_dir() / "geocache.json"
        try:
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}
        self._lock = threading.Lock()
        self._last_online = 0.0
        self._dirty = 0

    # -- helpers ------------------------------------------------------------
    def country_code(self, country: str, state: str = "") -> str | None:
        c = norm(country)
        if not c:
            st = (state or "").strip().upper()
            if st in US_CODES:
                return "US"
            if st in CA_CODES:
                return "CA"
            return None
        hit = self.countries.get(c)
        return hit[0] if hit else None

    def _state_code(self, cc: str | None, state: str) -> str:
        s = (state or "").strip()
        if cc == "US" and norm(s) in US_STATES:
            return US_STATES[norm(s)]
        return s.upper()

    @staticmethod
    def _variants(n: str):
        yield n
        swaps = {"st": "saint", "saint": "st", "ft": "fort", "fort": "ft", "mt": "mount", "mount": "mt"}
        head, _, rest = n.partition(" ")
        if rest and head in swaps:
            yield f"{swaps[head]} {rest}"

    # -- main entry ---------------------------------------------------------
    def locate(self, city: str, state: str = "", country: str = "", allow_online: bool = True):
        """Return (lat, lon, approx) or None."""
        cc = self.country_code(country, state)
        st = self._state_code(cc, state)
        key = f"{norm(city)}|{st}|{cc or norm(country)}"
        if key in self.cache:
            v = self.cache[key]
            return tuple(v) if v else None

        cands = []
        for k in self._variants(norm(city)):
            cands = self.by_name.get(k, [])
            if cc:
                cands = [c for c in cands if c[0] == cc]
            if st and cc in ("US", "CA", "AU"):
                cands = [c for c in cands if c[1] == st]
            if cands:
                break
        if cands:
            best = max(cands, key=lambda c: c[4])
            return best[2], best[3], False

        if allow_online and self.online and city:
            hit = self._nominatim(city, state, country)
            if hit:
                self._remember(key, [hit[0], hit[1], False])
                return hit[0], hit[1], False

        if cc and st and f"{cc}|{st}" in self.admin1:
            lat, lon = self.admin1[f"{cc}|{st}"]
            return lat, lon, True
        c = self.countries.get(norm(country)) if country else None
        if c:
            return c[1], c[2], True
        return None

    def find_in_text(self, text: str):
        """Very small NER: look for 'City, ST' or 'over/in/near City' in a title."""
        import re
        t = text or ""
        m = re.search(r"\b([A-Z][a-zA-Z.\- ]{2,30}),\s*([A-Z]{2})\b", t)
        if m:
            city, st = m.group(1).strip(), m.group(2)
            city = re.split(r"\b(?:over|in|near|above|from|at)\s+", city)[-1].strip()
            cc = "US" if st in US_CODES else ("CA" if st in CA_CODES else None)
            if cc:
                r = self.locate(city, st, cc, allow_online=False)
                if r:
                    return r, f"{city}, {st}"
        for m in re.finditer(r"\b(?:over|in|near|above|from)\s+([A-Z][a-zA-Z\-]+(?:\s[A-Z][a-zA-Z\-]+){0,2})", t):
            name = m.group(1)
            n = norm(name)
            if n in self.countries and len(n) > 3:
                c = self.countries[n]
                return (c[1], c[2], True), name
            if n in US_STATES:
                lat, lon = self.admin1.get(f"US|{US_STATES[n]}", (None, None))
                if lat is not None:
                    return (lat, lon, True), name
            cands = self.by_name.get(n)
            if cands:
                best = max(cands, key=lambda c: c[4])
                if best[4] >= 150_000:   # only big, unambiguous cities from free text
                    return (best[2], best[3], False), name
        return None

    def locate_text(self, text: str):
        """Free-form place like 'Detroit, MI', 'Germany', 'Tokyo, Japan'. Offline only."""
        t = (text or "").strip()
        if not t or t.upper() in ("N/A", "NA", "UNKNOWN"):
            return None
        parts = [p.strip() for p in t.split(",") if p.strip()]
        if len(parts) >= 2:
            last = parts[-1]
            if last.upper() in US_CODES or norm(last) in US_STATES:
                return self.locate(parts[0], last, "US", allow_online=False)
            r = self.locate(parts[0], "", last, allow_online=False)
            if r:
                return r
        n = norm(t)
        if n in REGIONS:
            return REGIONS[n][0], REGIONS[n][1], True
        if n in self.countries:
            c = self.countries[n]
            return c[1], c[2], True
        if n in US_STATES:
            lat, lon = self.admin1.get(f"US|{US_STATES[n]}", (None, None))
            if lat is not None:
                return lat, lon, True
        cands = self.by_name.get(n)
        if cands:
            best = max(cands, key=lambda c: c[4])
            if best[4] >= 50_000:
                return best[2], best[3], False
        for p in parts:   # e.g. "Near Kabul" or "Baghdad Iraq"
            for word in (p, p.split()[-1]):
                c = self.countries.get(norm(word))
                if c:
                    return c[1], c[2], True
        return None

    # -- online fallback ------------------------------------------------------
    def _nominatim(self, city, state, country):
        with self._lock:
            wait = 1.1 - (time.time() - self._last_online)
            if wait > 0:
                time.sleep(wait)
            self._last_online = time.time()
        q = ", ".join(x for x in (city, state, country) if x)
        try:
            r = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": q, "format": "json", "limit": 1},
                headers={"User-Agent": USER_AGENT}, timeout=10,
            )
            if r.status_code == 200 and r.json():
                j = r.json()[0]
                return float(j["lat"]), float(j["lon"])
        except (requests.RequestException, ValueError, KeyError):
            pass
        return None

    def _remember(self, key, value):
        with self._lock:
            self.cache[key] = value
            self._dirty += 1
            if self._dirty >= 10:
                self.flush()

    def flush(self):
        try:
            self.cache_path.write_text(json.dumps(self.cache), encoding="utf-8")
            self._dirty = 0
        except OSError:
            pass
