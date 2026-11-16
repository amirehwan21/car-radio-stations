"""Weekly check of v1/stations.json: test every stream, repair broken URLs, hide long-dead stations.

Rules (car-launcher PROJECT_PLAN §6.5):
- A URL passes only if real audio arrives: >= 32 KB within 10 s for icy/http, or for HLS the
  playlist parses and its first media segment downloads.
- All URLs fail -> repair, in order: Radio Browser by uuid -> Radio Browser search by name (MY) ->
  known URL patterns (revma HLS <-> direct, StreamTheWorld AAC <-> MP3 mounts). A candidate is
  accepted only if it passes the same test; it is put first, old URLs are kept after it.
- Never deletes a station. Failing 3 runs in a row -> hidden; passing again -> unhidden.
- Every http:// URL whose https:// copy passes gets that copy put just before it: Android apps
  block plain http by default (seen on the head unit in car-launcher 0.3.2), older app versions
  included, so https comes first wherever a station offers it.
- A revma direct stream (stream.rcs.revma.com/<id>) gets its HLS twin (<id>/hls.m3u8) when that
  passes: first if the direct stream is slow to start (the app gives a URL 8 s to start; Fly FM's
  direct stream often sent nothing for 8 s while its HLS segments came in 1.5 s), else as a fallback.
- Logos must be https: an http favicon is switched to https if that serves an image, else removed
  (the app then shows its default radio icon).
- Stations marked "check": "geo" are skipped (streams that refuse non-Malaysian IPs).
- If more than 20 % of tested stations fail, nothing is written (network or checker problem) and
  the exit code is 2.
- checked_at always changes; version (+1) and generated_at only when station content changes.

Usage: python3 checker/check.py v1/stations.json [--report report.md] [--only id,id] [--dry-run]
Standard library only.
"""
import argparse
import concurrent.futures as futures
import copy
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UA = "car-radio-stations-checker/1 (+https://github.com/amirehwan21/car-radio-stations)"
AUDIO_BYTES = 32 * 1024
SEGMENT_BYTES = 8 * 1024
TIMEOUT = 10
HIDE_AFTER = 3
MAX_FAIL_SHARE = 0.20
RADIO_BROWSER = "https://all.api.radio-browser.info/json"
# Fields that only record check results; changing them doesn't bump the list version.
RESULT_FIELDS = ("status", "last_ok", "fail_count")


def http_get(url, headers=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def read_up_to(resp, limit, deadline):
    got = 0
    while got < limit and time.monotonic() < deadline:
        chunk = resp.read(min(16 * 1024, limit - got))
        if not chunk:
            break
        got += len(chunk)
    return got


def test_icy(url):
    deadline = time.monotonic() + TIMEOUT
    with http_get(url, {"Icy-MetaData": "1"}) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        if r.status != 200:
            return False, f"HTTP {r.status}"
        if "html" in ctype or "json" in ctype:
            return False, f"not audio ({ctype})"
        if "mpegurl" in ctype:
            return test_hls(url)
        got = read_up_to(r, AUDIO_BYTES, deadline)
        return (got >= AUDIO_BYTES, f"{got} bytes in {TIMEOUT}s" if got < AUDIO_BYTES else "audio")


def test_hls(url, depth=0):
    with http_get(url) as r:
        text = r.read(256 * 1024).decode("utf-8", "replace")
        base = r.geturl()
    if "#EXTM3U" not in text:
        return False, "not an HLS playlist"
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    uris = [l for l in lines if not l.startswith("#")]
    if not uris:
        return False, "empty playlist"
    if "#EXT-X-STREAM-INF" in text:
        if depth > 2:
            return False, "too many playlist levels"
        return test_hls(urllib.parse.urljoin(base, uris[0]), depth + 1)
    segment = urllib.parse.urljoin(base, uris[0])
    with http_get(segment) as s:
        got = read_up_to(s, SEGMENT_BYTES, time.monotonic() + TIMEOUT)
    return (got > 0, "segment ok" if got else "empty segment")


def test_once(url, fmt):
    try:
        if fmt == "hls" or ".m3u8" in url:
            return test_hls(url)
        return test_icy(url)
    except Exception as e:  # network errors are results, not crashes
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def test_url(url, fmt):
    """One retry after 2 s: a single network blip shouldn't count as a failing week."""
    ok, why = test_once(url, fmt)
    if not ok and not why.startswith("HTTPError: HTTP Error 404"):
        time.sleep(2)
        ok, why = test_once(url, fmt)
    return ok, why


_rb_servers = None


def radio_browser_servers():
    """Radio Browser asks clients to pick from its mirror list; all.api.* alone is flaky."""
    global _rb_servers
    if _rb_servers is None:
        try:
            with http_get("https://all.api.radio-browser.info/json/servers") as r:
                names = sorted({s["name"] for s in json.load(r) if s.get("name")})
        except Exception:
            names = []
        _rb_servers = [f"https://{n}/json" for n in names] or [RADIO_BROWSER]
    return _rb_servers


def radio_browser_get(path):
    last = None
    for base in radio_browser_servers()[:4]:
        try:
            with http_get(base + path) as r:
                return json.load(r)
        except Exception as e:
            last = e
    raise RuntimeError(f"Radio Browser unreachable: {type(last).__name__}")


def radio_browser_candidates(station, notes):
    out = []
    try:
        if station.get("uuid"):
            for s in radio_browser_get(f"/stations/byuuid/{station['uuid']}"):
                out += [s.get("url_resolved"), s.get("url")]
        q = urllib.parse.urlencode({"name": station["name"], "countrycode": "MY", "hidebroken": "true", "limit": 5})
        for s in radio_browser_get(f"/stations/search?{q}"):
            if s.get("name", "").strip().lower() == station["name"].lower():
                out += [s.get("url_resolved"), s.get("url")]
    except Exception as e:
        notes.append(str(e))
    return out


def pattern_candidates(url):
    out = []
    m = re.match(r"(https://stream\.rcs\.revma\.com/[a-z0-9]+)(/hls\.m3u8)?$", url)
    if m:
        out.append(m.group(1) if m.group(2) else m.group(1) + "/hls.m3u8")
    m = re.match(r"(https?://[^/]*streamtheworld\.com/.*/)([A-Z0-9_]+?)(AAC\.aac|AAC_SC|_SC|\.mp3|AAC)$", url)
    if m:
        head, mount = m.group(1), m.group(2)
        out += [f"{head}{mount}AAC.aac", f"{head}{mount}.mp3", f"{head}{mount}AAC_SC", f"{head}{mount}_SC"]
    return [u for u in out if u != url]


def prefer_https(s):
    """Puts a working https:// copy ahead of each http:// URL. Returns the URLs added."""
    out, added = [], []
    for u in s["urls"]:
        if u.startswith("http://"):
            h = "https://" + u[len("http://"):]
            if h not in s["urls"] and test_url(h, s.get("format"))[0]:
                out.append(h)
                added.append(h)
        out.append(u)
    s["urls"] = out
    return added


START_BYTES = 16 * 1024
START_SECONDS = 6
REVMA_DIRECT = re.compile(r"https://stream\.rcs\.revma\.com/[a-z0-9]+$")


def starts_fast(url, tries=2):
    """True if every try delivers START_BYTES within START_SECONDS: what a player needs to start."""
    for _ in range(tries):
        try:
            with http_get(url, {"Icy-MetaData": "1"}) as r:
                if read_up_to(r, START_BYTES, time.monotonic() + START_SECONDS) < START_BYTES:
                    return False
        except Exception:
            return False
    return True


def prefer_revma_hls(s):
    """Adds the HLS twin of a revma direct URL (see the rules above). Returns a note, or None."""
    for u in s["urls"]:
        hls = u + "/hls.m3u8"
        if not REVMA_DIRECT.match(u) or hls in s["urls"] or not test_url(hls, "hls")[0]:
            continue
        i = s["urls"].index(u)
        if starts_fast(u):
            s["urls"].insert(i + 1, hls)
            return f"HLS fallback added: {hls}"
        s["urls"].insert(i, hls)
        if i == 0:
            s["format"] = "hls"
        return f"slow direct stream, HLS first: {hls}"
    return None


def secure_logo(s):
    """Logos over http could be swapped on the way (security review, car-launcher 0.3.3)."""
    logo = s.get("favicon") or ""
    if not logo.startswith("http://"):
        return None
    https = "https://" + logo[len("http://"):]
    try:
        with http_get(https) as r:
            if r.status == 200 and (r.headers.get("Content-Type") or "").lower().startswith("image/"):
                s["favicon"] = https
                return f"logo now https: {https}"
    except Exception:
        pass
    s["favicon"] = None
    return "http-only logo removed"


def check_station(s):
    """Returns (station_after, outcome, detail). outcome: ok | repaired | failing | hidden | skipped."""
    s = copy.deepcopy(s)
    today = datetime.now(timezone.utc).date().isoformat()
    if s.get("check") == "geo":
        return s, "skipped", "geo-restricted, not tested"
    reasons = []
    https = prefer_https(s)
    notes = [n for n in (prefer_revma_hls(s), secure_logo(s)) if n]
    for url in s["urls"]:
        ok, why = test_url(url, s.get("format"))
        if ok:
            s.update(status="ok", last_ok=today, fail_count=0, hidden=False)
            if https or notes:
                return s, "repaired", "; ".join((["https added: " + ", ".join(https)] if https else []) + notes)
            return s, "ok", why
        reasons.append(f"{url} → {why}")
    seen = set(s["urls"])
    candidates = radio_browser_candidates(s, reasons) + [c for u in s["urls"] for c in pattern_candidates(u)]
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        ok, why = test_url(c, "hls" if ".m3u8" in c else "icy/http")
        if not ok:
            reasons.append(f"candidate {c} → {why}")
        if ok:
            s["urls"] = [c] + s["urls"]
            s["format"] = "hls" if ".m3u8" in c else "icy/http"
            s.update(status="ok", last_ok=today, fail_count=0, hidden=False)
            return s, "repaired", f"new URL {c}"
    s["fail_count"] = int(s.get("fail_count") or 0) + 1
    s["status"] = "failing"
    if s["fail_count"] >= HIDE_AFTER:
        s["hidden"] = True
        return s, "hidden", "; ".join(reasons)
    return s, "failing", "; ".join(reasons)


def content_key(stations):
    return json.dumps([{k: v for k, v in s.items() if k not in RESULT_FIELDS} for s in stations], sort_keys=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--report", default=None)
    ap.add_argument("--only", default=None, help="comma-separated ids to check (others kept as they are)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    data = json.load(open(a.path, encoding="utf-8"))
    only = set(a.only.split(",")) if a.only else None
    todo = [s for s in data["stations"] if not only or s["id"] in only]

    results = {}
    with futures.ThreadPoolExecutor(max_workers=12) as pool:
        for s_after, outcome, detail in pool.map(check_station, todo):
            results[s_after["id"]] = (s_after, outcome, detail)

    tested = [r for r in results.values() if r[1] != "skipped"]
    failing = [r for r in tested if r[1] in ("failing", "hidden")]
    # Against the whole list, so a partial run (--only) can't trip the guard on its own.
    share = len(failing) / max(1, len(data["stations"]))
    counts = {k: sum(1 for r in results.values() if r[1] == k) for k in ("ok", "repaired", "failing", "hidden", "skipped")}

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    lines = [f"# Station check {now}", "", f"Checked {len(results)} of {len(data['stations'])} stations: " +
             ", ".join(f"{v} {k}" for k, v in counts.items() if v), ""]
    guard = share > MAX_FAIL_SHARE
    if guard:
        lines += [f"**Not written:** {share:.0%} of tested stations failed (limit {MAX_FAIL_SHARE:.0%}). "
                  "Probably a network or checker problem, not the stations.", ""]
    for title, kind in (("Repaired", "repaired"), ("Failing (hidden after 3 runs)", "failing"), ("Hidden", "hidden")):
        rows = sorted((r for r in results.values() if r[1] == kind), key=lambda r: r[0]["name"])
        if rows:
            lines += [f"## {title}", ""] + [f"- **{r[0]['name']}** (`{r[0]['id']}`, fail {r[0]['fail_count']}): {r[2]}" for r in rows] + [""]
    report = "\n".join(lines)
    if a.report:
        open(a.report, "w", encoding="utf-8").write(report)
    print(report)

    if guard:
        sys.exit(2)
    before = content_key(data["stations"])
    data["stations"] = [results.get(s["id"], (s,))[0] for s in data["stations"]]
    data["checked_at"] = now
    if content_key(data["stations"]) != before:
        data["version"] = int(data["version"]) + 1
        data["generated_at"] = now
    if not a.dry_run:
        with open(a.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")


if __name__ == "__main__":
    main()
