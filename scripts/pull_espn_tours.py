"""Pull DP World Tour and LIV round scores from ESPN -> pq/rounds_ext.csv (+ .parquet)

GitHub Actions copy. Same pull, same parsing and the same output schema as the model's pipeline/pull_espn_tours.py.
Additions for running outside the Claude workspace (all optional, defaults unchanged):
  --skip-file reference/pga_feed_event_ids.csv   skip events already in our PGA Tour ESPN feed (majors,
                                                 co-sanctioned events), since there are no holes_*.parquet files here
  --out DIR                                      output folder (default: <ROOT>/pq)
  season range runs to next calendar year automatically (the model copy hard-codes 2018-2026)

Needs network access to site.api.espn.com (and sports.core.api.espn.com for the fallback).
ESPN athlete ids are shared across tours, so these rounds join our PGA Tour players automatically.

Output columns: year, tour, tournament_id, tournament_name, start_date, round, player_id, player_name,
                score, par, position
Events already in the PGA Tour ESPN feed (majors, co-sanctioned events) are skipped (same ESPN event id).
Team events, match play and Stableford formats are skipped by name.

Endpoints (public ESPN JSON):
  calendar : https://site.api.espn.com/apis/site/v2/sports/golf/{league}/scoreboard?dates={year}
  event    : https://site.api.espn.com/apis/site/v2/sports/golf/{league}/scoreboard?event={event_id}
  fallback : https://sports.core.api.espn.com/v2/sports/golf/leagues/{league}/events/{id}/competitions/{id}/competitors
If ESPN changes a field name, run with --inspect to dump one raw event JSON and adjust parse_event().
"""
import glob
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
import pandas as pd

# Runs anywhere: in the Claude workspace it writes to /home/claude/golfdata/pq; on your own computer it writes
# to a "pq" folder next to wherever you run it (python3 pull_espn_tours.py). Upload pq/rounds_ext.csv to Claude.
ROOT = os.environ.get("GOLF_ROOT", "/home/claude/golfdata" if os.path.isdir("/home/claude/golfdata") else os.getcwd())
PQ = f"{ROOT}/pq"
os.makedirs(PQ, exist_ok=True)
_NEXT = datetime.now(timezone.utc).year + 2  # range end is exclusive: covers the season that starts late this year
LEAGUES = {"eur": range(2018, _NEXT), "liv": range(2022, _NEXT)}
SKIP = re.compile(r"team|match play|stableford|ryder|presidents|pro-am only|shootout|hero cup|q-school", re.I)
UA = {"User-Agent": "Mozilla/5.0 (golf model research)"}


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
                pos = (c.get("status", {}) or {}).get("position", {}).get("id") or c.get("order") or c.get("sortOrder")
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


def main(inspect=False, skip_file=None, out_dir=None):
    global PQ
    if out_dir:
        PQ = out_dir
        os.makedirs(PQ, exist_ok=True)
    have = set()
    for f in glob.glob(f"{PQ}/holes_*.parquet"):
        have |= set(pd.read_parquet(f, columns=["tournament_id"]).tournament_id.astype(str))
    if skip_file:
        have |= set(pd.read_csv(skip_file, dtype={"tournament_id": str}).tournament_id)
        print(f"skip list: {len(have)} event ids already in the PGA Tour feed")
    now = datetime.now(timezone.utc).isoformat()
    out, log = [], []
    for league, years in LEAGUES.items():
        for y in years:
            try:
                cal = calendar(league, y)
            except Exception as e:
                log.append(f"{league} {y}: calendar failed ({e})"); continue
            for m in cal:
                if m["id"] in have or SKIP.search(m["label"] or ""):
                    continue
                if (m.get("end") or m.get("start") or "9999") > now:
                    continue                                  # not finished yet
                try:
                    d = get(f"https://site.api.espn.com/apis/site/v2/sports/golf/{league}/scoreboard?event={m['id']}")
                except Exception as e:
                    log.append(f"{league} {m['id']} {m['label']}: fetch failed ({e})"); continue
                if inspect:
                    json.dump(d, open(f"{PQ}/espn_sample_{league}.json", "w"))
                    print("wrote sample JSON; stopping (--inspect)"); return
                rows = parse_event(d, league, m)
                if not rows:
                    log.append(f"{league} {m['id']} {m['label']}: no round scores parsed")
                out += [dict(r, year=y) for r in rows]
                time.sleep(0.25)
            print(f"{league} {y}: {len(cal)} calendar events, total rounds so far {len(out):,}")
    X = pd.DataFrame(out)
    if X.empty:
        print("NO ROUNDS PULLED. Check network allowlist / run with --inspect.\n" + "\n".join(log[:20]))
        sys.exit(1)
    X = X.drop_duplicates(["tournament_id", "round", "player_id"])
    # par missing for some rows: use the event-round mode
    X["par"] = X.par.fillna(X.groupby(["tournament_id", "round"]).par.transform(lambda s: s.mode().iloc[0] if s.notna().any() else 72))
    X.to_csv(f"{PQ}/rounds_ext.csv", index=False)
    try:
        X.to_parquet(f"{PQ}/rounds_ext.parquet", index=False)
    except ImportError:
        print("pyarrow not installed: wrote pq/rounds_ext.csv only (that's the file to upload)")
    print(f"rounds_ext: {len(X):,} rounds, {X.tournament_id.nunique()} events, {X.player_id.nunique()} players")
    print(X.groupby(["tour", "year"]).agg(events=("tournament_id", "nunique"), rounds=("score", "size")).to_string())
    if log:
        print("issues:\n  " + "\n  ".join(log[:40]))


def _arg(flag):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


if __name__ == "__main__":
    main(inspect="--inspect" in sys.argv, skip_file=_arg("--skip-file"), out_dir=_arg("--out"))
