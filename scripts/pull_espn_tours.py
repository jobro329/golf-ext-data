"""Pull DP World Tour and LIV round scores from ESPN -> pq/rounds_ext.csv (+ .parquet)

GitHub Actions incremental weekly version. Preserves the existing validated history and adds recent completed events.
Arguments for the independent GitHub runner:
  --skip-file reference/pga_feed_event_ids.csv   skip events already in our PGA Tour ESPN feed (majors,
                                                 co-sanctioned events), since there are no holes_*.parquet files here
  --out DIR                            output folder (default: <ROOT>/pq)
  --baseline FILE                      validated history (default: data/rounds_ext.csv; required)
  --recent-days N                     look back N days (default: 120)

Needs network access to site.api.espn.com for season calendars and site.web.api.espn.com for
completed-event leaderboards. The older site.api.espn.com scoreboard?event= endpoint ignores the
event parameter and returns the current event (zero historic rounds); do not use it.
ESPN athlete ids are shared across tours, so these rounds join our PGA Tour players automatically.

Output columns: year, tour, tournament_id, tournament_name, start_date, round, player_id, player_name,
                score, par, position
Events already in the PGA Tour ESPN feed (majors, co-sanctioned events) are skipped (same ESPN event id).
Team events, match play and Stableford formats are skipped by name.

Endpoints (public ESPN JSON):
  calendar : https://site.api.espn.com/apis/site/v2/sports/golf/{league}/scoreboard?dates={year}
  event    : https://site.web.api.espn.com/apis/site/v2/sports/golf/leaderboard?league={league}&event={event_id}
For weekly updates, only missing recent completed events are fetched. Old history remains unchanged.
Use a separate rate-limited backfill script for missing historical tournaments.
Historical endpoints and parser were checked against Nov 21, 2026 ESPN diagnostics. Weekly incremental flow is tested offline.
"""
import glob
import json
import os
import re
import sys
import time
import urllib.request
from urllib.parse import urlencode
from datetime import datetime, timezone
import pandas as pd

# Runs anywhere: in the Claude workspace it writes to /home/claude/golfdata/pq; on your own computer it writes
# to a "pq" folder next to wherever you run it (python3 pull_espn_tours.py). Upload pq/rounds_ext.csv to Claude.
ROOT = os.environ.get("GOLF_ROOT", "/home/claude/golfdata" if os.path.isdir("/home/claude/golfdata") else os.getcwd())
PQ = f"{ROOT}/pq"
os.makedirs(PQ, exist_ok=True)
SKIP = re.compile(r"team|match play|stableford|ryder|presidents|pro-am only|shootout|hero cup|q-school", re.I)
# Explicitly confirmed cancelled events which ESPN still lists in its season calendars.
# Do not broaden this to all scoreless events: an actual completed tournament without
# scores must still fail validation rather than silently go missing.
CANCELLED_EVENT_IDS = {
    "401843582": "2026 LIV Golf Michigan - Stroke Play (cancelled before play)",
}
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.espn.com/",
}


def get(url, tries=3):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(1.5 * (k + 1))
    raise last


def calendar(league, year):
    d = get(f"https://site.api.espn.com/apis/site/v2/sports/golf/{league}/scoreboard?dates={year}")
    cal = []
    for lg in d.get("leagues", []):
        for c in lg.get("calendar", []):
            ev = c.get("event", c) if isinstance(c, dict) else {}
            eid = str(ev.get("id") or c.get("id", "")).strip() if isinstance(c, dict) else ""
            if eid:
                cal.append({"id": eid, "label": c.get("label") or ev.get("name", ""),
                            "start": c.get("startDate") or ev.get("date"), "end": c.get("endDate")})
    return cal


def historical_leaderboard(league, event_id):
    """Fetch the actual historical event. Check IDs to guard against a silently stale/current page."""
    url = ("https://site.web.api.espn.com/apis/site/v2/sports/golf/leaderboard?"
           + urlencode({"league": league, "event": str(event_id)}))
    d = get(url)
    events = d.get("events", []) if isinstance(d, dict) else []
    matches = [e for e in events if str(e.get("id")) == str(event_id)]
    if len(matches) != 1:
        got = [e.get("id") for e in events if isinstance(e, dict)]
        raise ValueError(f"ESPN historical event mismatch: requested {event_id}, returned {got}")
    return {"events": matches}


def to_par(s):
    s = str(s).strip().upper()
    if s in ("E", "EVEN", "0"):
        return 0
    m = re.match(r"^([+-]?\d+)$", s)
    return int(m.group(1)) if m else None


def parse_event(d, league, meta):
    rows = []
    for ev in d.get("events", []):
        name = ev.get("name") or meta.get("label", "")
        for comp in ev.get("competitions", []):
            for c in comp.get("competitors", []):
                ath = c.get("athlete", {})
                pid = str(c.get("id") or ath.get("id"))
                pname = ath.get("displayName") or ath.get("fullName")
                # Legacy baseline uses ESPN sortOrder, not tied finishing-place IDs.
                # Preserve its ranking semantics so historical rows remain unchanged.
                pos = c.get("sortOrder") or c.get("order") or (c.get("status", {}) or {}).get("position", {}).get("id")
                for ls in c.get("linescores", []) or []:
                    rnd = ls.get("period")
                    val = ls.get("value")
                    holes = ls.get("linescores") or []
                    if rnd is None or val is None:
                        continue
                    if holes and len(holes) != 18:          # incomplete round
                        continue
                    tp = to_par(ls.get("displayValue"))
                    par = (val - tp) if tp is not None else None
                    if not (55 <= val <= 95):
                        continue
                    rows.append({"tour": league, "tournament_id": str(ev.get("id")), "tournament_name": name,
                                 "start_date": (ev.get("date") or meta.get("start") or "")[:10], "round": int(rnd),
                                 "player_id": pid, "player_name": pname, "score": float(val), "par": par,
                                 "position": pd.to_numeric(pos, errors="coerce")})
    return rows


def main(inspect=False, skip_file=None, out_dir=None, baseline_file="data/rounds_ext.csv", recent_days=120):
    """Safe weekly incremental pull: preserve known rounds and fetch only missing recent completed events.

    The historical file is authoritative. Older gaps (e.g. 2018/2019) need a separate, rate-limited
    backfill process; attempting to fetch hundreds of old events every week causes unreliable pulls.
    """
    global PQ
    from datetime import timedelta
    if out_dir:
        PQ = out_dir
    os.makedirs(PQ, exist_ok=True)
    if not os.path.isfile(baseline_file):
        sys.exit(f"Baseline missing: {baseline_file}. Refusing to rebuild history from scratch.")

    keys = ["tournament_id", "round", "player_id"]
    cols = ["year", "tour", "tournament_id", "tournament_name", "start_date", "round",
            "player_id", "player_name", "score", "par", "position"]
    B = pd.read_csv(baseline_file, dtype={"tournament_id": str, "player_id": str})
    if list(B.columns) != cols or B.empty or B.duplicated(keys).any():
        sys.exit("Baseline has missing columns, no rows, or duplicate player-round IDs; refusing to proceed.")

    have = set(B.tournament_id.astype(str))
    pga = set()
    if skip_file:
        pga = set(pd.read_csv(skip_file, dtype={"tournament_id": str}).tournament_id.astype(str))
    print(f"baseline: {len(B):,} preserved rounds in {len(have)} events; PGA skip list: {len(pga)}", flush=True)

    # Two proven historical responses verify that live scores, not just calendars, remain reachable.
    # Both were independently inspected in the Nov 21, 2026 GitHub production diagnostics.
    for league, event_id, minimum in [("eur", "401734047", 100), ("liv", "401713458", 100)]:
        try:
            response = historical_leaderboard(league, event_id)
            rows = parse_event(response, league, {"label": "", "start": ""})
            if len(rows) < minimum:
                raise ValueError(f"only {len(rows)} parsed rounds (expected at least {minimum})")
            print(f"health check {league} {event_id}: {len(rows)} completed player-rounds", flush=True)
        except Exception as exc:
            sys.exit(f"ESPN scoring health check failed for {league} {event_id}: {exc}")

    now_dt = datetime.now(timezone.utc)
    cutoff = (now_dt - timedelta(days=recent_days)).date()
    today = now_dt.date()
    out, errors = [], []
    seen = set()
    # Each tour's season may span two calendar years. The recent window only needs these 3 calendars.
    for league in ("eur", "liv"):
        for year in range(today.year - 1, today.year + 2):
            try:
                cal = calendar(league, year)
            except Exception as exc:
                errors.append(f"{league} {year} calendar: {exc}")
                continue
            if year == today.year and not cal:
                errors.append(f"{league} {year}: current-year calendar was empty")
                continue
            eligible = 0
            for m in cal:
                eid = str(m["id"])
                if eid in CANCELLED_EVENT_IDS:
                    print(f"skipped cancelled event {league} {year} {eid}: {CANCELLED_EVENT_IDS[eid]}", flush=True)
                    continue
                if eid in seen or eid in have or eid in pga or SKIP.search(m.get("label") or ""):
                    continue
                seen.add(eid)
                # Prefer completed-event end date, but parse both Z and +00:00 consistently.
                stamp = m.get("end") or m.get("start")
                if not stamp:
                    continue
                try:
                    dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    event_day = dt.date()
                except (ValueError, TypeError):
                    errors.append(f"{league} {eid}: invalid calendar date {stamp!r}")
                    continue
                if event_day < cutoff or event_day >= today:
                    continue
                eligible += 1
                try:
                    response = historical_leaderboard(league, eid)
                    if inspect:
                        path = f"{PQ}/espn_sample_{league}.json"
                        with open(path, "w") as f:
                            json.dump(response, f)
                        print(f"wrote {path}; stopping (--inspect)", flush=True)
                        return
                    rows = parse_event(response, league, m)
                    # Reject incomplete/placeholder results instead of letting a bad weekly pull overwrite data.
                    r1 = sum(r["round"] == 1 for r in rows)
                    complete_rounds = {r["round"] for r in rows if 1 <= r["round"] <= 4}
                    min_field = 40 if league == "liv" else 30
                    if r1 < min_field or len(complete_rounds) < 2:
                        raise ValueError(f"incomplete scores: {len(rows)} rows, {r1} R1 golfers, rounds {sorted(complete_rounds)}")
                    out += [dict(r, year=year) for r in rows]
                    print(f"added {league} {year} {eid} {m['label']}: {len(rows)} rounds", flush=True)
                except Exception as exc:
                    errors.append(f"{league} {year} {eid} {m['label']}: {exc}")
                time.sleep(0.7)
            print(f"{league} {year}: {len(cal)} calendar entries, {eligible} missing recent completed events", flush=True)

    # In particular, never claim success if a newly completed event could not be loaded.
    if errors:
        print("FAILED: ESPN fetch problems (baseline not overwritten):\n" + "\n".join(errors), flush=True)
        sys.exit(1)

    if out:
        N = pd.DataFrame(out, columns=cols)
        N["par"] = N.par.fillna(N.groupby(["tournament_id", "round"]).par.transform(
            lambda s: s.mode().iloc[0] if s.notna().any() else 72))
        N = N.drop_duplicates(keys)
        X = pd.concat([B, N], ignore_index=True).drop_duplicates(keys, keep="first")
        X.to_csv(f"{PQ}/rounds_ext.csv", index=False)
    else:
        # Byte-for-byte copy avoids gratuitous rewriting of the 9 MB history every Tuesday.
        import shutil
        shutil.copyfile(baseline_file, f"{PQ}/rounds_ext.csv")
        X = B
    X.to_parquet(f"{PQ}/rounds_ext.parquet", index=False)
    added = len(X) - len(B)
    print(f"weekly result: {len(X):,} total rows, {added} added rows, "
          f"{X.tournament_id.nunique()} events; existing history preserved", flush=True)
    print("Older missing seasons are not backfilled by the weekly job.", flush=True)


def _arg(flag):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


if __name__ == "__main__":
    main(inspect="--inspect" in sys.argv, skip_file=_arg("--skip-file"), out_dir=_arg("--out"),
         baseline_file=_arg("--baseline") or "data/rounds_ext.csv",
         recent_days=int(_arg("--recent-days") or 120))
