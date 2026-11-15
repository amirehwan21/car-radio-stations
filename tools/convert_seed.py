"""One-time conversion of the app repo's seed list (docs/radio/malaysia-stations.json) to schema v1.

Schema v1 is the contract with the launcher (car-launcher ARCHITECTURE §7.2):
  top level: schema, version, generated_at, checked_at, min_app_version, default_favourites, stations
  station:   id (permanent), name, language, tags, urls (tried in order), format, codec, bitrate,
             now_playing, favicon, logo_shape, gfk_weekly_listeners, gfk_wave, votes, uuid, fm,
             status, hidden, last_ok, fail_count

Usage: python3 tools/convert_seed.py <path to malaysia-stations.json> > v1/stations.json
"""
import json
import re
import sys
from datetime import datetime, timezone

# Tidy directory names (shown in the app). Keys are the seed names.
NAME_FIXES = {
    "HITZ | Malaysia's #1 Hit Music Station - All the hitz, all the time!": "HITZ",
    "Hot FM Malaysia": "Hot FM",
    "MIX FM Malaysia": "MIX FM",
    "LITE FM Malaysia": "LITE FM",
    "Klasik FM 101,1 MHz": "Klasik FM",
    "Nasional FM 88,5 MHz": "Nasional FM",
    "ALL FM [91.9]": "ALL FM",
    "Radio Klasik (Klasik Nasional)": "Radio Klasik",
    "REDfm": "Red FM",
    "BFM 89.9": "BFM",
    "Rentak Pelangi MuzikFM": "Rentak Pelangi Muzik FM",
    "GeGar Muzik FM": "Gegar Muzik FM",
    "Gegar Musik FM": "Gegar Muzik FM",
}

# Permanent ids for the 9 default favourites (PROJECT_PLAN §6.5); everything else is a slug of the name.
ID_FIXES = {"HITZ": "hitz-fm"}

DEFAULT_FAVOURITES = ["era-fm", "hot-fm", "sinar-fm", "suria-fm", "hitz-fm", "rakita", "johor-fm", "mix-fm", "zayan"]


def slug(name):
    return ID_FIXES.get(name) or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main(path):
    seed = json.load(open(path, encoding="utf-8"))
    merged = {}
    order = []
    for s in seed["stations"]:
        name = NAME_FIXES.get(s["name"], s["name"]).strip()
        sid = slug(name)
        urls = [u for u in (s.get("url"), s.get("url_fallback")) if u]
        if sid in merged:
            # Same station listed twice (another server): keep one entry, add its URLs as fallbacks.
            target = merged[sid]
            target["urls"] += [u for u in urls if u not in target["urls"]]
            target["votes"] = max(target["votes"], s.get("votes") or 0)
            target["uuid"] = target["uuid"] or s.get("uuid")
            continue
        merged[sid] = {
            "id": sid,
            "name": name,
            "language": s.get("language") or "",
            "tags": s.get("tags") or "",
            "urls": urls,
            "format": s.get("format") or "icy/http",
            "codec": s.get("codec") or "",
            "bitrate": s.get("bitrate") or 0,
            "now_playing": s.get("now_playing") or "none",
            "favicon": s.get("favicon") or None,
            "logo_shape": s.get("logo_shape") or "default",
            "gfk_weekly_listeners": s.get("gfk_weekly_listeners") or 0,
            "gfk_wave": s.get("gfk_wave") or "",
            "votes": s.get("votes") or 0,
            "uuid": s.get("uuid") or None,
            "fm": s.get("fm") or None,
            "status": "ok",
            "hidden": False,
            "last_ok": s.get("verified") or seed.get("verified"),
            "fail_count": 0,
        }
        order.append(sid)

    missing = [f for f in DEFAULT_FAVOURITES if f not in merged]
    if missing:
        sys.exit(f"default favourites missing from the seed: {missing}")

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    out = {
        "schema": 1,
        "version": 1,
        "generated_at": now,
        "checked_at": now,
        "min_app_version": 1,
        "default_favourites": DEFAULT_FAVOURITES,
        "stations": [merged[i] for i in order],
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    print(f"{len(order)} stations ({len(seed['stations'])} in the seed)", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
