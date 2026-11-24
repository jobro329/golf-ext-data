"""Hourly weather for PGA Tour courses from Open-Meteo (free, no API key).

Two modes:
  python scripts/pull_weather.py history    one-time backfill: actual hourly weather for every PGA Tour event-course
                                             since 2022 (reference/event_courses.csv) -> data/weather/history_hourly.csv
  python scripts/pull_weather.py forecast   hourly forecast for every course with an event starting in the next 9
                                             days -> data/weather/forecast_latest.csv (+ a dated copy in
                                             data/weather/forecasts/ so forecasts can later be scored against actuals)

All times are written in UTC (column time_utc), the same clock as the PGA Tour tee-time feed the model uses.
Units: wind and gusts in mph, temperature in C, precipitation in mm.

Checks (the run fails, and nothing is committed, if any fail):
  - Open-Meteo's own timezone for each course's coordinates must match the course's timezone in the PGA Tour
    tee-time feed (catches a wrong latitude/longitude in reference/courses.csv)
  - no event-course may be missing hours; wind 0-80 mph
  - history: at least 95% of event-courses must come back
Writes data/weather/status.json: mode, time, rows, courses, problems.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

OUT = "data/weather"
HOURLY = "wind_speed_10m,wind_gusts_10m,wind_direction_10m,temperature_2m,precipitation"
COURSES = pd.read_csv("reference/courses.csv", dtype={"course_id": str})


def get(url, params, tries=4):
    q = url + "?" + urllib.parse.urlencode(params)
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(q, headers={"User-Agent": "golf-model-weather"}), timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(3 * (k + 1))
    raise last


def frame(d, course_id):
    h = d["hourly"]
    off = d.get("utc_offset_seconds", 0)
    local = pd.to_datetime(h["time"])
    f = pd.DataFrame({"course_id": course_id, "time_utc": local - pd.to_timedelta(off, unit="s"),
                      "wind_mph": h["wind_speed_10m"], "gust_mph": h["wind_gusts_10m"], "wind_dir": h["wind_direction_10m"],
                      "temp_c": h["temperature_2m"], "precip_mm": h["precipitation"]})
    f["om_timezone"] = d.get("timezone")
    return f


def tz_ok(course, om_tz, when):
    """Coordinates sanity check: Open-Meteo's timezone for the point must be within 1 hour of the tee-time feed's
    timezone on that date. (Names can differ, e.g. America/Toronto vs New_York, and the feed labels some venues
    loosely, e.g. Vidanta Vallarta as America/Chicago, which differs from Mexico by 1 hour in summer since Mexico
    dropped DST. A wrong country or coast is off by 2+ hours.)"""
    if not om_tz:
        return False
    t = datetime.combine(when, datetime.min.time())
    a, b = ZoneInfo(course.tz).utcoffset(t), ZoneInfo(om_tz).utcoffset(t)
    return abs((a - b).total_seconds()) <= 3600


def check(f, problems, label):
    if f[["wind_mph", "gust_mph"]].isna().any().any():
        problems.append(f"{label}: missing hours")
    if not f.wind_mph.between(0, 80).all():
        problems.append(f"{label}: wind outside 0-80 mph")
    # (no gust-vs-wind check: Open-Meteo's weather models sometimes report gusts a touch below the sustained wind
    #  for an hour; that is normal model output, not bad data. Nov 24 2026 fix after a false failure.)


def history():
    ix = pd.read_csv("reference/event_courses.csv", dtype={"course_id": str})
    C = COURSES.set_index("course_id")
    out, problems, failed = [], [], []
    for r in ix.itertuples():
        c = C.loc[r.course_id]
        s = (date.fromisoformat(r.start) - timedelta(days=1)).isoformat()
        e = (date.fromisoformat(r.end) + timedelta(days=1)).isoformat()
        try:
            d = get("https://archive-api.open-meteo.com/v1/archive",
                    {"latitude": c.lat, "longitude": c.lon, "start_date": s, "end_date": e, "hourly": HOURLY,
                     "wind_speed_unit": "mph", "timezone": "auto"})
        except Exception as ex:
            failed.append(f"{r.season} {r.tournament_name} / {c.course_name}: {ex}")
            continue
        if not tz_ok(c, d.get("timezone"), date.fromisoformat(r.start)):
            problems.append(f"{c.course_name}: coordinates resolve to {d.get('timezone')}, tee-time feed says {c.tz}")
        f = frame(d, r.course_id)
        f["tournament_id"], f["season"] = r.tournament_id, r.season
        check(f, problems, f"{r.season} {r.tournament_name} / {c.course_name}")
        out.append(f)
        time.sleep(0.4)
        print(f"{r.season} {r.tournament_name} / {c.course_name}: {len(f)} hours", flush=True)
    if len(failed) > 0.05 * len(ix):
        problems.append(f"{len(failed)} of {len(ix)} event-courses failed to download: {failed[:5]}")
    X = pd.concat(out).drop_duplicates(["course_id", "time_utc"])
    return X, problems, {"event_courses": len(ix), "downloaded": len(out), "failed": failed[:20]}


def schedule_courses(days=9):
    """Upcoming PGA Tour events (golfastr schedule) -> course ids by matching event names to past event-courses."""
    import pyreadr
    yrs = {date.today().year, (date.today() + timedelta(days=days)).year}
    sch = []
    for y in yrs:
        url = f"https://github.com/array-carpenter/golfastr/releases/download/schedules/schedule_{y}.rds"
        tmp = f"/tmp/schedule_{y}.rds"
        try:
            urllib.request.urlretrieve(url, tmp)
            sch.append(list(pyreadr.read_r(tmp).values())[0])
        except Exception as e:
            print(f"schedule {y}: {e}")
    S = pd.concat(sch)
    S["start_date"] = pd.to_datetime(S.start_date).dt.date
    S["end_date"] = pd.to_datetime(S.end_date).dt.date
    up = S[(S.start_date >= date.today()) & (S.start_date <= date.today() + timedelta(days=days))]
    ix = pd.read_csv("reference/event_courses.csv", dtype={"course_id": str})
    key = lambda s: " ".join(w for w in str(s).lower().replace("the ", "").split() if w.isalpha())[:22]
    ix["k"] = ix.tournament_name.map(key)
    rows, unknown = [], []
    for e in up.itertuples():
        past = ix[ix.k == key(e.tournament_name)].sort_values("season")
        if past.empty:
            unknown.append(e.tournament_name)
            continue
        last = past[past.season == past.season.max()]
        for cid in last.course_id.unique():
            rows.append({"event_id": e.event_id, "event": e.tournament_name, "start": e.start_date, "end": e.end_date, "course_id": cid})
    return pd.DataFrame(rows), unknown


def forecast():
    ev, unknown = schedule_courses()
    problems = [f"no course on file for upcoming event '{u}' (new venue): add it to reference/courses.csv and event_courses.csv"
                for u in unknown]
    C = COURSES.set_index("course_id")
    out = []
    for r in ev.drop_duplicates("course_id").itertuples():
        c = C.loc[r.course_id]
        d = get("https://api.open-meteo.com/v1/forecast",
                {"latitude": c.lat, "longitude": c.lon, "hourly": HOURLY, "wind_speed_unit": "mph",
                 "timezone": "auto", "forecast_days": 16})
        if not tz_ok(c, d.get("timezone"), date.today()):
            problems.append(f"{c.course_name}: coordinates resolve to {d.get('timezone')}, tee-time feed says {c.tz}")
        f = frame(d, r.course_id)
        f = f[(f.time_utc.dt.date >= r.start - timedelta(days=1)) & (f.time_utc.dt.date <= r.end + timedelta(days=1))]
        f["event_id"], f["event"] = r.event_id, r.event
        check(f, problems, f"{r.event} / {c.course_name}")
        out.append(f)
        print(f"{r.event} / {c.course_name}: {len(f)} forecast hours", flush=True)
    X = pd.concat(out) if out else pd.DataFrame()
    return X, problems, {"events": sorted(ev.event.unique().tolist()) if len(ev) else [], "unknown_venues": unknown}


def main(mode):
    os.makedirs(f"{OUT}/forecasts", exist_ok=True)
    X, problems, info = history() if mode == "history" else forecast()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = {"mode": mode, "generated_utc": now, "passed": not problems, "rows": int(len(X)),
              "courses": int(X.course_id.nunique()) if len(X) else 0, "problems": problems, **info}
    json.dump(status, open(f"{OUT}/status_{mode}.json", "w"), indent=2, default=str)
    print(json.dumps(status, indent=2, default=str))
    if problems:
        sys.exit(1)
    if mode == "history":
        X.to_csv(f"{OUT}/history_hourly.csv", index=False)
    else:
        X.to_csv(f"{OUT}/forecast_latest.csv", index=False)
        X.to_csv(f"{OUT}/forecasts/forecast_{now[:10]}.csv", index=False)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "forecast")
