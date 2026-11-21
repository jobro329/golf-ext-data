"""Validate a fresh DP World / LIV pull before it is used by the golf model.

Usage:
  python scripts/validate.py --new output/rounds_ext.csv --baseline data/rounds_ext.csv \
      --players reference/players.csv --pga-events reference/pga_feed_event_ids.csv --out output

Exit code 0 = every FAIL check passed (WARN lines are informational). Exit code 1 = do not use this pull.
Writes <out>/validation_report.md and <out>/validation_summary.json.

FAIL checks
  1. Schema      exact column list and order the model reads; tour is eur or liv.
  2. Real scores every row has an integer score 55-95 and a par 60-75; every event-round has a real spread of
                 scores (ESPN's pre-tournament placeholder pages show everyone at "E" with no values); every event
                 has a plausible field; event-round scoring averages sit within -6/+8 of par.
  3. Duplicates  no repeated tournament_id + round + player_id; one tour and one name per tournament_id; no two
                 tournament ids with the same name and start date; no event that is already in our PGA Tour feed.
  4. Player IDs  every ESPN id that is already in our model's crosswalk (reference/players.csv) carries the same
                 player (normalized name or a known spelling). More than 1% mismatches fails.
  5. Leaderboard lowest 72-hole total holds 1st place (incl. ties) in at least 95% of events, and finish position
                 tracks total score (median Spearman >= 0.95).
  6. Baseline    nothing already validated went missing or changed: >= 98% of baseline rows still present, >= 99.5%
                 of those with the same score, and the row count did not shrink by more than 2%.
"""
import argparse
import json
import os
import re
import sys
import unicodedata

import numpy as np
import pandas as pd

SCHEMA = ["year", "tour", "tournament_id", "tournament_name", "start_date", "round", "player_id", "player_name",
          "score", "par", "position"]

# identical to the model's history_build.norm_name, so "same player" means the same thing in both places
SPECIAL = str.maketrans({"ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "ß": "ss", "ł": "l", "Ł": "l",
                         "đ": "d", "Đ": "d", "ð": "d", "þ": "th", "’": "", "'": ""})


def norm_name(s):
    s = str(s).translate(SPECIAL)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().replace("-", " ").replace(".", " ")
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", " ", s)
    s = re.sub(r"[^a-z ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_event(s):
    s = re.sub(r"[^a-z0-9 ]", " ", str(s).lower())
    return re.sub(r"\s+", " ", s).strip()


class Report:
    def __init__(self):
        self.lines, self.fails, self.warns, self.stats = [], [], [], {}

    def check(self, name, ok, detail):
        self.lines.append(f"- {'PASS' if ok else 'FAIL'} **{name}**: {detail}")
        if not ok:
            self.fails.append(f"{name}: {detail}")

    def warn(self, name, detail):
        self.lines.append(f"- WARN **{name}**: {detail}")
        self.warns.append(f"{name}: {detail}")

    def info(self, text):
        self.lines.append(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", required=True)
    ap.add_argument("--baseline")
    ap.add_argument("--players", required=True)
    ap.add_argument("--pga-events", required=True)
    ap.add_argument("--out", default="output")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    R = Report()
    ids = {"player_id": str, "tournament_id": str}
    X = pd.read_csv(a.new, dtype=ids)
    R.info(f"# DP World / LIV pull validation\n\nFile: `{a.new}`, {len(X):,} rows\n\n## Checks")

    # 1. schema -------------------------------------------------------------------------------------------
    R.check("schema", list(X.columns) == SCHEMA,
            "columns match the model schema" if list(X.columns) == SCHEMA else f"got {list(X.columns)}")
    if list(X.columns) != SCHEMA:
        return finish(R, a.out)
    R.check("tour values", set(X.tour.unique()) <= {"eur", "liv"}, f"{sorted(X.tour.unique())}")
    for c in ("score", "par", "round", "year", "position"):
        X[c] = pd.to_numeric(X[c], errors="coerce")

    # 2. real round scores -------------------------------------------------------------------------------
    bad_score = X.score.isna() | (X.score % 1 != 0) | ~X.score.between(55, 95)
    R.check("scores are real strokes", not bad_score.any(), f"{int(bad_score.sum())} rows with missing/non-integer/out-of-range scores")
    bad_par = X.par.isna() | ~X.par.between(60, 75)
    R.check("par present", bad_par.sum() <= 0.001 * len(X), f"{int(bad_par.sum())} rows with missing/odd par")
    g = X.groupby(["tournament_id", "round"]).score
    er = pd.DataFrame({"sd": g.std(), "distinct": g.nunique(), "mean": g.mean(),
                       "par": X.groupby(["tournament_id", "round"]).par.agg(lambda s: s.mode().iloc[0])})
    po = X[X["round"] > 4]
    if len(po):
        R.warn("playoff rows", f"{len(po)} rows with round > 4 in {po.tournament_id.nunique()} events: ESPN logs playoffs "
                               "as an extra period. Not real 18-hole rounds; the model's field-size filter keeps them out of ratings.")
    er4 = er[er.index.get_level_values("round") <= 4]
    flat = er4[(er4.sd.fillna(0) < 1.0) | (er4.distinct < 5)]
    R.check("no placeholder rounds", flat.empty, f"{len(flat)} event-rounds with no real spread of scores"
            + (f" (e.g. {list(flat.index[:3])})" if len(flat) else ""))
    off = er4[~(er4["mean"] - er4.par).between(-6, 8)]
    R.check("scoring averages plausible", off.empty, f"{len(off)} event-rounds averaging outside -6/+8 vs par")
    r1 = X[X["round"] == 1].groupby(["tour", "tournament_id"]).player_id.nunique().reset_index()
    small = r1[r1.player_id < np.where(r1.tour == "liv", 40, 30)]
    R.check("field sizes", small.empty, f"{len(small)} events with a round-1 field under 30 (DP World) / 40 (LIV)")
    partial = int((X.par != X.groupby(["tournament_id", "round"]).par.transform(lambda s: s.mode().iloc[0])).sum())
    if partial:
        R.warn("partial rounds", f"{partial} rows whose par differs from the event-round par (partial rounds). "
                                 "The model drops these automatically.")
    dl = int((X.tournament_name.str.contains("Dunhill Links", case=False) & (X["round"] < 4)).sum())
    if dl:
        R.warn("Dunhill Links R1-3", f"{dl} rows from the three-course rotation. The model drops these automatically.")

    # 3. duplicates --------------------------------------------------------------------------------------
    dup = int(X.duplicated(["tournament_id", "round", "player_id"]).sum())
    R.check("no duplicate player-rounds", dup == 0, f"{dup} repeated tournament_id + round + player_id rows")
    per_id = X.groupby("tournament_id").agg(tours=("tour", "nunique"), names=("tournament_name", "nunique"))
    R.check("one tour and name per event id", (per_id.max() <= 1).all(),
            f"{int((per_id.tours > 1).sum())} ids on two tours, {int((per_id.names > 1).sum())} ids with two names")
    ev = X.drop_duplicates("tournament_id")[["tournament_id", "tournament_name", "start_date", "tour", "year"]].copy()
    ev["k"] = ev.tournament_name.map(norm_event) + "|" + ev.start_date.astype(str)
    twins = ev[ev.k.duplicated(keep=False)].sort_values("k")
    R.check("no duplicate tournaments", twins.empty, f"{len(twins)} event ids sharing a name and start date"
            + (f": {twins[['tournament_id', 'tournament_name', 'start_date']].values.tolist()[:4]}" if len(twins) else ""))
    pga = set(pd.read_csv(a.pga_events, dtype={"tournament_id": str}).tournament_id)
    both = sorted(set(ev.tournament_id) & pga)
    R.check("no overlap with PGA Tour feed", not both, f"{len(both)} events already in the PGA data" + (f": {both[:5]}" if both else ""))

    # 4. player ids vs the model's crosswalk ---------------------------------------------------------------
    P = pd.read_csv(a.players, dtype={"espn_id": str})
    known = {r.espn_id: {norm_name(r.player_name)} | {norm_name(s) for s in str(r.other_spellings).split(" / ")}
             for r in P.itertuples()}
    xp = X.drop_duplicates("player_id")[["player_id", "player_name"]]
    shared = xp[xp.player_id.isin(known)]
    mism = shared[[norm_name(n) not in known[i] for i, n in zip(shared.player_id, shared.player_name)]]
    rate = len(mism) / max(len(shared), 1)
    R.check("player ids match the model", rate <= 0.01,
            f"{len(shared) - len(mism)} of {len(shared)} ids already in our crosswalk carry the same player ({rate:.2%} mismatch)"
            + (f"; mismatches: {mism.values.tolist()[:8]}" if len(mism) else ""))
    new_ids = xp[~xp.player_id.isin(known)]
    if len(new_ids):
        R.warn("new player ids", f"{len(new_ids)} ids not yet in the crosswalk (new names; the model adds them). "
                                 f"e.g. {new_ids.player_name.head(8).tolist()}")
    multi = X.groupby("player_id").player_name.agg(lambda s: len({norm_name(v) for v in s}))
    if (multi > 1).any():
        R.warn("ids with two spellings", f"{int((multi > 1).sum())} ids appear under different spellings")

    # 5. leaderboard consistency -------------------------------------------------------------------------
    T = X.groupby(["tournament_id", "player_id"]).agg(tot=("score", "sum"), nr=("round", "nunique"), pos=("position", "first")).reset_index()
    T = T[T.nr == T.groupby("tournament_id").nr.transform("max")]
    best = T.groupby("tournament_id").tot.transform("min")
    win_ok = T[T.tot == best].groupby("tournament_id").pos.min().le(1).mean()
    rho = T.groupby("tournament_id").apply(lambda d: d.tot.corr(d.pos, method="spearman") if d.pos.notna().sum() > 5 else np.nan, include_groups=False).median()
    R.check("leaderboards consistent", win_ok >= 0.95 and rho >= 0.95,
            f"lowest total holds 1st in {win_ok:.1%} of events; median Spearman(position, total) {rho:.3f}")

    # 6. regression vs the last validated copy --------------------------------------------------------------
    if a.baseline and os.path.exists(a.baseline):
        B = pd.read_csv(a.baseline, dtype=ids)
        k = ["tournament_id", "round", "player_id"]
        m = B[k + ["score"]].merge(X[k + ["score"]], on=k, how="left", suffixes=("_b", "_n"))
        kept = m.score_n.notna().mean()
        same = (m.score_b == m.score_n)[m.score_n.notna()].mean()
        R.check("baseline rows still present", kept >= 0.98, f"{kept:.2%} of {len(B):,} previously validated rows found")
        R.check("baseline scores unchanged", same >= 0.995, f"{same:.2%} identical")
        R.check("row count not shrinking", len(X) >= 0.98 * len(B), f"{len(X):,} rows now vs {len(B):,} before")
        new_ev = sorted(set(X.tournament_id) - set(B.tournament_id))
        names = X.drop_duplicates("tournament_id").set_index("tournament_id")
        R.info(f"\n## New since last copy\n{len(X) - len(m):,} rows beyond the baseline, {len(new_ev)} new events:")
        for t in new_ev:
            R.info(f"- {names.at[t, 'start_date']} {names.at[t, 'tour']} {names.at[t, 'tournament_name']} ({t})")
    else:
        R.warn("baseline", "no baseline file: regression checks skipped (first run)")

    s = X.groupby(["tour", "year"]).agg(events=("tournament_id", "nunique"), rounds=("score", "size"))
    R.info("\n## Coverage\n```\n" + s.to_string() + "\n```")
    R.stats = {"rows": int(len(X)), "events": int(X.tournament_id.nunique()), "players": int(X.player_id.nunique())}
    return finish(R, a.out)


def finish(R, out):
    R.info(f"\n## Result: {'FAILED, do not use this pull' if R.fails else 'PASSED'} "
           f"({len(R.fails)} failures, {len(R.warns)} warnings)")
    open(f"{out}/validation_report.md", "w").write("\n".join(R.lines) + "\n")
    json.dump({"passed": not R.fails, "failures": R.fails, "warnings": R.warns, **R.stats},
              open(f"{out}/validation_summary.json", "w"), indent=2)
    print("\n".join(R.lines))
    return 1 if R.fails else 0


if __name__ == "__main__":
    sys.exit(main())
