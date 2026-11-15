# car-radio-stations

Hosted list of Malaysian internet radio streams for the Car Launcher head-unit app.

- **List:** [`v1/stations.json`](v1/stations.json), served at
  `https://amirehwan21.github.io/car-radio-stations/v1/stations.json`
  (mirror: `https://cdn.jsdelivr.net/gh/amirehwan21/car-radio-stations@main/v1/stations.json`).
- **Weekly check:** every Monday 04:00 MYT GitHub Actions runs [`checker/check.py`](checker/check.py):
  it tests every stream (real audio must arrive), repairs broken URLs (Radio Browser, known URL
  patterns), hides a station after 3 failing weeks and unhides it when it passes, and keeps one
  issue **"Stations needing attention"** up to date. Run it any time from the Actions tab
  (**Weekly station check → Run workflow**) or locally: `python3 checker/check.py v1/stations.json --dry-run`.
- **Schema v1** is a contract with the app: never rename or remove a field; a breaking change goes to `v2/`.
  Every station has a permanent `id` (favourites are stored by id). Fields: `id, name, language, tags,
  urls (tried in order), format (hls | icy/http), codec, bitrate, now_playing (icy | hls-id3 | none),
  favicon, logo_shape (square | wordmark | default), gfk_weekly_listeners, gfk_wave, votes, uuid, fm,
  status, hidden, last_ok, fail_count`; optional `"check": "geo"` skips stations that refuse
  non-Malaysian IPs (GitHub runners are in the US).
- **Editing:** add or fix a station by editing `v1/stations.json` (keep `id`s stable) and bump `version`.
  `tools/convert_seed.py` was the one-time import from the app repo's seed list.

Streams are the stations' own public web streams, listed by a community directory; for personal
in-car listening, not for redistribution.
