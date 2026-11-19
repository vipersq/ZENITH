"""Data sources. Each fetcher returns a list[Event] (or raises on failure)."""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import requests

from .core import Event, USER_AGENT, local_ts, utc_ts

TIMEOUT = 30
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


def _session(ua: str = USER_AGENT) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": ua, "Accept-Language": "en-GB,en;q=0.9"})
    return s


try:  # curl_cffi mimics a real Chrome TLS/HTTP2 fingerprint, which bot filters accept
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover
    cffi_requests = None


def browser_session():
    """Session that looks like desktop Chrome (needed for NUFORC and Reddit)."""
    if cffi_requests is not None:
        for target in ("chrome", "chrome131", "chrome124", "chrome120"):
            try:
                s = cffi_requests.Session(impersonate=target)
                s.headers.update({"Accept-Language": "en-GB,en;q=0.9"})
                return s
            except Exception:  # noqa: BLE001 - older curl_cffi without that target
                continue
    s = _session(BROWSER_UA)
    s.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    })
    return s


def http_mode() -> str:
    return "curl_cffi (Chrome)" if cffi_requests is not None else "requests (no curl_cffi!)"


class RateLimited(RuntimeError):
    def __init__(self, msg, retry_after=600):
        super().__init__(msg)
        self.retry_after = retry_after


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "nav", "header", "footer"):
            self.skip += 1
        if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "nav", "header", "footer") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(s: str) -> str:
    p = _Text()
    p.feed(s or "")
    txt = "".join(p.parts)
    txt = re.sub(r"[ \t\r\f\v]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n\n", txt)
    return txt.strip()


# =========================================================================== NUFORC

NUFORC_INDEX = "https://nuforc.org/subndx/?id=e{ym}"
NUFORC_AJAX = ("https://nuforc.org/wp-admin/admin-ajax.php?action=get_wdtable&table_id=1"
               "&wdt_var1=YearMonth&wdt_var2={ym}")
NUFORC_NONCE = re.compile(r'id=["\']wdtNonceFrontendServerSide_1["\'][^>]*value=["\']([a-f0-9]+)["\']')
NUFORC_ID = re.compile(r"id=(\d+)")


def _parse_us_dt(s: str) -> datetime | None:
    s = html_to_text(s).strip()
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%y %H:%M", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def fetch_nuforc(geocoder, months: int = 2) -> list[Event]:
    s = browser_session()
    try:  # land on the home page first, like a person would (sets cookies)
        s.get("https://nuforc.org/", timeout=TIMEOUT)
    except Exception:  # noqa: BLE001
        pass
    now = datetime.now()
    yms, y, m = [], now.year, now.month
    for _ in range(months):
        yms.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    out: list[Event] = []
    errors = []
    for ym in yms:
        try:
            out.extend(_nuforc_month(s, ym, geocoder))
        except Exception as e:  # noqa: BLE001 - one bad month must not kill the rest
            errors.append(f"{ym}: {e}")
    if not out and errors:
        raise RuntimeError("; ".join(errors))
    return out


def _nuforc_month(s: requests.Session, ym: str, geocoder) -> list[Event]:
    index_url = NUFORC_INDEX.format(ym=ym)
    r = s.get(index_url, timeout=TIMEOUT, headers={"Referer": "https://nuforc.org/"})
    if r.status_code == 403:
        t = re.search(r"<title>(.*?)</title>", r.text or "", re.S | re.I)
        who = (t.group(1).strip()[:60] if t else (r.headers.get("server") or "?"))
        raise RuntimeError(f"403 — NUFORC blocked the request ({http_mode()}; page: {who})")
    r.raise_for_status()
    nonce = NUFORC_NONCE.search(r.text)
    if not nonce:
        raise RuntimeError("no wdtNonce (did the NUFORC site change?)")
    post = {
        "draw": "1",
        "columns[0][data]": "0", "columns[0][searchable]": "true", "columns[0][orderable]": "false",
        "columns[1][data]": "1", "columns[1][searchable]": "true", "columns[1][orderable]": "true",
        "order[0][column]": "1", "order[0][dir]": "desc",
        "start": "0", "length": "-1",
        "search[value]": "", "search[regex]": "false",
        "wdtNonce": nonce.group(1),
    }
    r = s.post(NUFORC_AJAX.format(ym=ym), data=post, timeout=90, headers={
        "Referer": index_url, "X-Requested-With": "XMLHttpRequest", "Origin": "https://nuforc.org",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    r.raise_for_status()
    rows = r.json().get("data") or []
    return [e for e in (_nuforc_row(row, geocoder) for row in rows) if e]


NUFORC_FORM = {
    "draw": "1",
    "columns[0][data]": "0", "columns[0][searchable]": "true", "columns[0][orderable]": "false",
    "columns[1][data]": "1", "columns[1][searchable]": "true", "columns[1][orderable]": "true",
    "order[0][column]": "1", "order[0][dir]": "desc",
    "start": "0", "length": "-1",
    "search[value]": "", "search[regex]": "false",
}


def nuforc_months(n: int) -> list[str]:
    now = datetime.now()
    out, y, m = [], now.year, now.month
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def parse_nuforc_payload(texts: list[str], geocoder) -> list[Event]:
    """texts: raw JSON responses of the wpDataTables AJAX call (one per month)."""
    out, errors = [], []
    for t in texts:
        try:
            rows = json.loads(t).get("data") or []
        except ValueError:
            errors.append((t or "").strip()[:120])
            continue
        out.extend(e for e in (_nuforc_row(r, geocoder) for r in rows) if e)
    if not out and errors:
        raise RuntimeError("NUFORC returned non-JSON: " + errors[0])
    return out


def parse_reddit_text(text: str, geocoder) -> list[Event]:
    t = (text or "").strip()
    if not t.startswith("{"):
        raise RuntimeError("Reddit: " + (t[:140] or "empty response"))
    return _reddit_json(json.loads(t), geocoder)


def trim_nuforc_report(txt: str) -> str:
    txt = re.sub(r"[ \t]+", " ", txt or "")
    txt = re.sub(r"\n\s*\n+", "\n\n", txt).strip()
    start = txt.find("Occurred")
    if start == -1:
        start = txt.find("NUFORC UFO Sighting")
    if start != -1:
        txt = txt[start:]
    for stop in ("Leave a Reply", "Comments are closed", "© ", "Copyright"):
        k = txt.find(stop)
        if k > 200:
            txt = txt[:k]
    return txt.strip()[:12000]


def _nuforc_row(raw, geocoder) -> Event | None:
    if not isinstance(raw, list) or len(raw) < 8:
        return None
    link = str(raw[0] or "")
    occurred = _parse_us_dt(str(raw[1] or ""))
    city, state, country = (html_to_text(str(raw[i] or "")).strip() for i in (2, 3, 4))
    shape = html_to_text(str(raw[5] or "")).strip() or "Unknown"
    summary = html.unescape(html_to_text(str(raw[6] or ""))).strip()
    posted = _parse_us_dt(str(raw[7] or ""))
    media = html_to_text(str(raw[8] or "")).strip() if len(raw) > 8 else ""
    explanation = html_to_text(str(raw[9] or "")).strip() if len(raw) > 9 and raw[9] else ""
    if not occurred:
        return None
    mid = NUFORC_ID.search(link)
    sid = mid.group(1) if mid else hashlib.sha1(f"{occurred}{city}{summary}".encode()).hexdigest()[:10]
    place = ", ".join(x for x in (city, state, country) if x)
    pos = geocoder.locate(city, state, country) if geocoder else None
    e = Event(
        id=f"nuforc:{sid}", source="ufo",
        title=f"{shape} — {city or country or '?'}",
        # NUFORC gives local time without a zone; treat as local wall clock
        when=local_ts(occurred), posted=local_ts(posted) if posted else 0.0,
        place=place, summary=summary,
        url=f"https://nuforc.org/sighting/?id={sid}" if mid else "https://nuforc.org/",
        extra={"shape": shape, "media": media, "explanation": explanation, "local_time": True},
    )
    if pos:
        e.lat, e.lon, e.approx = pos
    return e


def fetch_nuforc_full(url: str) -> str:
    s = browser_session()
    r = s.get(url, timeout=TIMEOUT, headers={"Referer": "https://nuforc.org/"})
    r.raise_for_status()
    txt = html_to_text(r.text)
    # keep the report body: from "Occurred" to the end of the report block
    start = txt.find("Occurred")
    if start == -1:
        start = txt.find("NUFORC UFO Sighting")
    if start != -1:
        txt = txt[start:]
    for stop in ("Leave a Reply", "Comments are closed", "© ", "Copyright"):
        k = txt.find(stop)
        if k > 200:
            txt = txt[:k]
    return txt.strip()[:12000]


# =========================================================================== NASA fireballs

def fetch_fireballs(days: int = 365) -> list[Event]:
    date_min = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    r = _session().get("https://ssd-api.jpl.nasa.gov/fireball.api",
                       params={"date-min": date_min, "req-loc": "true"}, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    fields = j.get("fields", [])
    out = []
    for row in j.get("data", []):
        d = dict(zip(fields, row))
        try:
            when = datetime.strptime(d["date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            lat = float(d["lat"]) * (-1 if d.get("lat-dir") == "S" else 1)
            lon = float(d["lon"]) * (-1 if d.get("lon-dir") == "W" else 1)
        except (KeyError, TypeError, ValueError):
            continue
        kt = float(d.get("impact-e") or 0)
        energy = d.get("energy")
        alt, vel = d.get("alt"), d.get("vel")
        lines = [f"Impact energy: {kt} kt TNT", f"Radiated energy: {energy} ×10¹⁰ J"]
        if alt:
            lines.append(f"Burst altitude: {alt} km")
        if vel:
            lines.append(f"Velocity: {vel} km/s")
        lines.append("Source: US government sensors via NASA/JPL CNEOS. Data is published with a delay.")
        out.append(Event(
            id=f"fireball:{d['date']}", source="fireball",
            title=f"Fireball {kt:g} kt", when=utc_ts(when), lat=lat, lon=lon,
            place=f"{abs(lat):.1f}°{'S' if lat < 0 else 'N'} {abs(lon):.1f}°{'W' if lon < 0 else 'E'}",
            summary="\n".join(lines), magnitude=kt,
            url="https://cneos.jpl.nasa.gov/fireballs/",
        ))
    return out


# =========================================================================== NOAA Kp

def fetch_kp() -> dict:
    r = _session().get("https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
                       timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    series = []
    for row in data:
        if isinstance(row, dict):
            t, kp = row.get("time_tag"), row.get("Kp")
        elif isinstance(row, list) and row and row[0] != "time_tag":
            t, kp = row[0], row[1]
        else:
            continue
        try:
            series.append((t, float(kp)))
        except (TypeError, ValueError):
            continue
    if not series:
        raise RuntimeError("pusty indeks Kp")
    return {"kp": series[-1][1], "time": series[-1][0], "series": series[-24:]}


# =========================================================================== Reddit

REDDIT_SIGHTING_FLAIRS = {"sighting", "ufo", "uap", "anomalies", "anomaly", "video", "photo", "footage",
                          "video/photo", "image", "sighting/video", "witness", "encounter", "close encounter"}
REDDIT_TALK_FLAIRS = {"question", "community question", "disclosure", "government", "discussion", "meta",
                      "news", "podcast", "historical", "cross-post", "space launch", "opinion", "theory",
                      "consciousness", "fringe science", "speculation", "rant", "meme", "humor", "politics"}


def _reddit_removed(d: dict) -> bool:
    if d.get("removed_by_category") or d.get("banned_by"):
        return True
    if (d.get("selftext") or "").strip() in ("[removed]", "[deleted]"):
        return True
    if (d.get("author") or "") == "[deleted]":
        return True
    meta = d.get("_meta") or {}
    if isinstance(meta, dict) and (meta.get("removal_type") or "Removed by" in str(meta.get("edited_title", ""))):
        return True
    return False


def _reddit_media(d: dict) -> bool:
    url = d.get("url") or ""
    return bool(d.get("is_video") or d.get("is_gallery") or d.get("media")
                or d.get("post_hint") in ("image", "hosted:video", "rich:video")
                or any(h in url for h in ("v.redd.it", "i.redd.it", "/gallery/", "youtu", "imgur", "streamable")))


def fetch_reddit(geocoder, subs=("UFOs", "UFOB", "UAP", "HighStrangeness")) -> list[Event]:
    """Reddit posts via the public Arctic Shift archive (reddit.com itself blocks non-browser
    clients). Posts arrive there within roughly 10-30 minutes. Removed posts are returned flagged
    (extra['removed']) so the app can hide ones it stored before they were taken down."""
    s = _session()
    out, errors = [], []
    for sub in subs:
        try:
            r = s.get("https://arctic-shift.photon-reddit.com/api/posts/search",
                      params={"subreddit": sub, "limit": 100, "sort": "desc"}, timeout=TIMEOUT)
            if r.status_code == 429:
                raise RateLimited("Arctic Shift 429 — rate limited", _retry_after(r))
            r.raise_for_status()
            j = r.json()
            if j.get("error"):
                raise RuntimeError(j["error"])
            for d in j.get("data") or []:
                if d.get("stickied") or d.get("pinned"):
                    continue
                sr = d.get("subreddit") or sub
                e = _reddit_event(
                    d.get("id"), sr, d.get("title", ""), d.get("created_utc", 0),
                    d.get("selftext", ""), f"https://www.reddit.com/r/{sr}/comments/{d.get('id')}/",
                    geocoder, flair=d.get("link_flair_text") or "", score=d.get("score", 0),
                    media=_reddit_media(d))
                e.extra["removed"] = _reddit_removed(d)
                e.extra["comments"] = d.get("num_comments", 0)
                flair = (d.get("link_flair_text") or "").strip().lower()
                e.extra["sighting"] = (flair in REDDIT_SIGHTING_FLAIRS
                                       or (flair not in REDDIT_TALK_FLAIRS and (e.extra["media"] or e.has_pos)))
                out.append(e)
        except RateLimited:
            raise
        except Exception as e:  # noqa: BLE001
            errors.append(f"r/{sub}: {e}")
        time.sleep(0.5)
    if not out and errors:
        raise RuntimeError("; ".join(errors))
    return out


def fetch_nuforc_browser(geocoder, months: int = 2, on_needs_human=None) -> list[Event]:
    from . import realbrowser
    texts = realbrowser.nuforc_month_texts(nuforc_months(months), NUFORC_INDEX, NUFORC_AJAX, NUFORC_FORM,
                                           on_needs_human)
    return parse_nuforc_payload(texts, geocoder)


def fetch_nuforc_full_browser(url: str, on_needs_human=None) -> str:
    from . import realbrowser
    return trim_nuforc_report(realbrowser.page_text(url, on_needs_human))


def _retry_after(r) -> int:
    try:
        return max(120, int(float(r.headers.get("Retry-After", 600))))
    except (TypeError, ValueError):
        return 600


def _reddit_json(j, geocoder) -> list[Event]:
    out = []
    for ch in j.get("data", {}).get("children", []):
        d = ch.get("data", {})
        if d.get("stickied"):
            continue
        out.append(_reddit_event(
            d.get("id"), d.get("subreddit", "?"), d.get("title", ""), d.get("created_utc", 0),
            d.get("selftext", ""), "https://www.reddit.com" + d.get("permalink", ""),
            geocoder, flair=d.get("link_flair_text") or "", score=d.get("score", 0),
            media=bool(d.get("is_video") or d.get("post_hint") in ("image", "hosted:video", "rich:video")),
        ))
    return out


def _reddit_rss(s, multi, geocoder) -> list[Event]:
    import xml.etree.ElementTree as ET
    r = s.get(f"https://www.reddit.com/r/{multi}/new/.rss", params={"limit": 75}, timeout=TIMEOUT,
              headers={"Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8"})
    if r.status_code == 429:
        raise RateLimited("Reddit 429 — rate limited", _retry_after(r))
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for entry in ET.fromstring(r.content).findall("a:entry", ns):
        title = entry.findtext("a:title", "", ns)
        link_el = entry.find("a:link", ns)
        link = link_el.get("href") if link_el is not None else ""
        cat = entry.find("a:category", ns)
        sub = cat.get("term") if cat is not None else "?"
        upd = entry.findtext("a:published", "", ns) or entry.findtext("a:updated", "", ns)
        try:
            ts = utc_ts(datetime.fromisoformat(upd.replace("Z", "+00:00")))
        except ValueError:
            ts = 0
        pid = entry.findtext("a:id", "", ns).split("_")[-1]
        body = html_to_text(entry.findtext("a:content", "", ns))
        out.append(_reddit_event(pid, sub, title, ts, body, link, geocoder))
    return out


def _reddit_event(pid, sub, title, ts, body, url, geocoder, flair="", score=0, media=False) -> Event:
    e = Event(id=f"reddit:{pid}", source="reddit", title=title.strip()[:200], when=float(ts or 0),
              posted=float(ts or 0), summary=(body or "").strip()[:4000], url=url,
              place=f"r/{sub}", extra={"flair": flair, "score": score, "media": media, "sub": sub})
    hit = geocoder.find_in_text(title) if geocoder else None
    if hit:
        (lat, lon, approx), name = hit
        e.lat, e.lon, e.approx = lat, lon, True  # position guessed from the title: always approximate
        e.place = f"{name} • r/{sub}"
    return e


# =========================================================================== UFO Stalker (MUFON-linked)

STALKER_API = "https://ufostalker.com/api/ufostalker/v1/sightings/by-page"


def fetch_stalker(pages: int = 2, size: int = 50) -> list[Event]:
    s = _session(BROWSER_UA)
    s.headers["Accept"] = "application/json"
    out = []
    now = time.time()
    for page in range(pages):
        r = s.get(STALKER_API, params={"page": page, "size": size}, timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        if isinstance(rows, dict):
            rows = rows.get("content") or []
        for d in rows:
            e = _stalker_event(d, now)
            if e:
                out.append(e)
        if len(rows) < size:
            break
        time.sleep(0.5)
    return out


def _stalker_event(d: dict, now: float) -> Event | None:
    try:
        occurred = float(d.get("occurred") or 0) / 1000
        lat, lon = d.get("latitude"), d.get("longitude")
    except (TypeError, ValueError):
        return None
    # drop junk dates (year 1234, far future) and reports without a position
    if occurred < -2208988800 or occurred > now + 86400 or lat is None or lon is None:
        return None
    shape = (d.get("shape") or "Unknown").strip()
    if shape.count(",") >= 3:
        shape = "Multiple shapes"
    where = d.get("city") or d.get("region") or d.get("country") or "?"
    place = ", ".join(x for x in (d.get("city"), d.get("region"), d.get("country")) if x)
    summary = (d.get("summary") or "").strip()
    detail = (d.get("detailedDescription") or "").strip()
    text = summary + ("\n\n" + detail if detail and detail != summary else "")
    kind = d.get("type") or "UFO Sighting"
    return Event(
        id=f"stalker:{d.get('id')}", source="stalker",
        title=f"{shape} — {where}" + ("" if kind == "UFO Sighting" else f"  [{kind}]"),
        when=occurred, posted=float(d.get("submitted") or 0) / 1000,
        lat=float(lat), lon=float(lon), place=place, summary=text[:6000],
        approx=(d.get("addressResolution") in ("APPROXIMATE", "GEOMETRIC_CENTER")),
        url=f"https://ufostalker.com/event/{d.get('id')}",
        extra={"shape": shape, "type": kind, "colour": d.get("objectColor"), "sound": d.get("sound"),
               "flight": d.get("flightPath"), "duration": d.get("duration"),
               "witnesses": d.get("witnessCount"), "objects": d.get("numberOfObjects"),
               "distance": d.get("distance"), "altitude": d.get("altitude"), "weather": d.get("weather"),
               "context": d.get("observationContext"), "features": d.get("features"),
               "rating": d.get("rating"), "media": len(d.get("media") or []),
               "landing": d.get("landingOccurred"), "entity": d.get("entityEncountered")},
    )


# =========================================================================== Pentagon PURSUE (war.gov/UFO)

PURSUE_PAGE = "https://www.war.gov/UFO/"
PURSUE_MIRROR = "https://raw.githubusercontent.com/DenisSergeevitch/UFO-USA/main/metadata/uap-csv.csv"


def fetch_pursue(geocoder) -> list[Event]:
    """Declassified US government UAP files. Tries the official release index first,
    then a public GitHub mirror of it (release 1, May 2026)."""
    import csv
    import io
    s = _session(BROWSER_UA)
    text, origin = None, ""
    try:
        r = s.get(PURSUE_PAGE, timeout=TIMEOUT)
        if r.status_code == 200:
            m = re.findall(r'href="([^"]+\.csv[^"]*)"', r.text, re.I)
            if m:
                from urllib.parse import urljoin
                c = s.get(urljoin(PURSUE_PAGE, m[0]), timeout=TIMEOUT)
                if c.status_code == 200 and "Title" in c.text[:2000]:
                    text, origin = c.text, "war.gov"
    except Exception:  # noqa: BLE001
        pass
    if text is None:
        r = s.get(PURSUE_MIRROR, timeout=TIMEOUT)
        r.raise_for_status()
        text, origin = r.content.decode("utf-8-sig", "replace"), "GitHub mirror (war.gov release 1)"
    out = []
    for i, row in enumerate(csv.DictReader(io.StringIO(text.lstrip("\ufeff")))):
        e = _pursue_event(row, i, geocoder, origin)
        if e:
            out.append(e)
    return out


def _us_date(s: str) -> float:
    s = (s or "").strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%B %d, %Y", "%b %Y", "%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt == "%m/%d/%y" and dt.year > datetime.now().year:
                dt = dt.replace(year=dt.year - 100)       # '47 -> 1947
            return utc_ts(dt)
        except ValueError:
            continue
    return 0.0


def _pursue_event(row: dict, i: int, geocoder, origin: str) -> Event | None:
    g = lambda k: " ".join((row.get(k) or "").split())  # noqa: E731
    title = g("Title") or g("Video Title")
    if not title:
        return None
    kind = (g("Type") or "PDF").upper()
    agency = g("Agency") or "?"
    loc = g("Incident Location")
    link = g("PDF | Image Link")
    vid = g("DVIDS Video ID")
    if kind == "VID" and vid and not link:
        link = f"https://www.dvidshub.net/video/{vid}"
    blurb = g("Description Blurb")
    icon = {"PDF": "📄", "VID": "🎞", "IMG": "🖼"}.get(kind, "📄")
    e = Event(
        id=f"pursue:{hashlib.sha1((title + '|' + link + '|' + g('Video Title')).encode()).hexdigest()[:12]}",
        source="pursue",
        title=f"{icon} {agency}: {title[:90]}", when=_us_date(g("Incident Date")),
        posted=_us_date(g("Release Date")), place=loc if loc and loc != "N/A" else "",
        summary=blurb, url=link or PURSUE_PAGE,
        extra={"agency": agency, "kind": kind, "incident_date": g("Incident Date"),
               "release": g("Release Date"), "video": g("Video Title"), "origin": origin,
               "redaction": {"TRUE": "yes, partly blacked out", "FALSE": "no"}.get(
                   g("Redaction").upper(), g("Redaction"))},
    )
    pos = geocoder.locate_text(loc) if geocoder and loc else None
    if pos:
        e.lat, e.lon, e.approx = pos[0], pos[1], True
    return e


# =========================================================================== UK National Archives (MoD UFO files)

TNA_API = "https://discovery.nationalarchives.gov.uk/API/search/records"
TNA_RELEVANT = re.compile(r"\bU\.?F\.?O|unidentified (flying|aerial|anomalous)|flying saucer|"
                          r"aerial phenomen|unorthodox aircraft|\bUAP\b", re.I)


def fetch_tna(queries=("UFO", "unidentified flying objects", "flying saucers", "unidentified aerial phenomena"), max_pages: int = 5) -> list[Event]:
    s = _session(BROWSER_UA)
    s.headers["Accept"] = "application/json"
    seen, out = set(), []
    for q in queries:
        for page in range(1, max_pages + 1):
            r = s.get(TNA_API, params={"sps.searchQuery": q, "sps.recordCollections": "Records",
                                       "sps.heldByCode": "TNA", "sps.resultsPageSize": 100,
                                       "sps.page": page, "sps.sortByOption": "DATE_ASCENDING"},
                      timeout=TIMEOUT)
            r.raise_for_status()
            recs = r.json().get("records") or []
            for d in recs:
                rid = d.get("id")
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                blob = f"{d.get('title') or ''} {d.get('description') or ''}"
                if not TNA_RELEVANT.search(blob):
                    continue
                out.append(_tna_event(d))
            if len(recs) < 100:
                break
            time.sleep(0.3)
    return out


def _tna_event(d: dict) -> Event:
    ref = d.get("reference") or "?"
    desc = html_to_text(d.get("description") or "").strip()
    title = (d.get("title") or desc or "UFO file").strip()
    start = 0.0
    try:
        start = utc_ts(datetime.strptime(d.get("startDate") or "", "%d/%m/%Y"))
    except ValueError:
        pass
    lines = [desc] if desc and desc != title else []
    if d.get("coveringDates"):
        lines.append(f"Covering dates: {d['coveringDates']}")
    if d.get("context"):
        lines.append(f"Series: {d['context']}")
    if d.get("openingDate"):
        lines.append(f"Opened to the public: {d['openingDate']}")
    return Event(
        id=f"tna:{d.get('id')}", source="tna", title=f"🗂 {ref} — {title[:90]}", when=start,
        place="The National Archives, Kew (UK)", summary="\n".join(lines),
        url=f"https://discovery.nationalarchives.gov.uk/details/r/{d.get('id')}",
        extra={"reference": ref, "department": d.get("department"), "covering": d.get("coveringDates"),
               "opened": d.get("openingDate")},
    )
