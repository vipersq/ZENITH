"""Quick data-source check: python -m zenith.selftest"""
import json
import time

from . import realbrowser, sources
from .geocode import Geocoder


def main():
    g = Geocoder(online=False)
    print("Browser for NUFORC:", realbrowser.find_browser() or "MISSING (install Edge or Chrome)", "\n")

    def human(v):
        if v:
            print("   >>> NUFORC asks for a click — a browser window appeared, tick the checkbox.")

    tests = [
        ("UFO Stalker", sources.fetch_stalker),
        ("Reddit", lambda: sources.fetch_reddit(g)),
        ("NASA fireballs", sources.fetch_fireballs),
        ("NOAA Kp", lambda: [sources.fetch_kp()]),
        ("PURSUE files", lambda: sources.fetch_pursue(g)),
        ("UK Archives", sources.fetch_tna),
        ("NUFORC", lambda: sources.fetch_nuforc_browser(g, 1, human)),
    ]
    for name, fn in tests:
        if name == "NUFORC":
            print("NUFORC: starting Edge/Chrome in the background (up to ~1.5 min)...")
        t = time.time()
        try:
            res = fn()
            on_map = sum(1 for e in res if getattr(e, "has_pos", False))
            first = getattr(res[0], "title", "") if res else "-"
            print(f"[ OK ] {name:15} {len(res):5} rec. ({on_map} on map) {time.time()-t:5.1f}s  e.g.: {str(first)[:55]}")
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {name:15} {type(e).__name__}: {e}")

    print("\nNUFORC — per month (last 3):")
    try:
        months = sources.nuforc_months(3)
        texts = realbrowser.nuforc_month_texts(months, sources.NUFORC_INDEX, sources.NUFORC_AJAX,
                                               sources.NUFORC_FORM, human)
        for ym, t in zip(months, texts):
            j = json.loads(t)
            print(f"   {ym}: rows {len(j.get('data') or []):4}   recordsTotal={j.get('recordsTotal')}  "
                  f"recordsFiltered={j.get('recordsFiltered')}")
    except Exception as e:  # noqa: BLE001
        print(f"   error: {e}")
    input("\nPress Enter to exit")


if __name__ == "__main__":
    main()
