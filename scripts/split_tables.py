"""Split rounds_ext.csv into the four compact tables stored in the Claude project (data/ext_*.csv).

The single CSV (~9 MB) is too big for the project's space limit; these four tables (~3 MB) rebuild to exactly the
same rows, and the model's history_build.py reads them directly. The same cleaning history_build.py applies is done
first (drop partial rounds, whose par differs from the event-round par, and Dunhill Links rounds 1-3), so these tables
match the format of the copy already in the project. Run after validate.py passes:
  python scripts/split_tables.py output/rounds_ext.csv output/project_tables
"""
import os
import sys

import pandas as pd


def main(src, out):
    os.makedirs(out, exist_ok=True)
    X = pd.read_csv(src, dtype={"player_id": str, "tournament_id": str})
    n0 = len(X)
    mode = X.groupby(["tournament_id", "round"]).par.transform(lambda v: v.mode().iloc[0])
    multi = X.tournament_name.str.contains("Dunhill Links", case=False) & (X["round"] < 4)
    X = X[(X.par == mode) & ~multi].copy()
    print(f"cleaning: kept {len(X):,} of {n0:,} rows")
    ev = X.groupby(["tournament_id", "round"]).agg(year=("year", "first"), tour=("tour", "first"),
                                                   tournament_name=("tournament_name", "first"),
                                                   start_date=("start_date", "first"), par=("par", "first")).reset_index()
    pl = X.drop_duplicates("player_id")[["player_id", "player_name"]]
    pe = X.drop_duplicates(["tournament_id", "player_id"])[["tournament_id", "player_id", "position"]]
    sc = X[["tournament_id", "round", "player_id", "score"]]
    # round-trip check: the four tables must rebuild the cleaned rows exactly (names: one spelling per id)
    back = sc.merge(ev, on=["tournament_id", "round"]).merge(pl, on="player_id").merge(pe, on=["tournament_id", "player_id"])
    k = ["tournament_id", "round", "player_id"]
    a = X.drop(columns=["player_name"]).sort_values(k).reset_index(drop=True)
    b = back[X.columns].drop(columns=["player_name"]).sort_values(k).reset_index(drop=True)
    if len(a) != len(b) or not a.astype(str).equals(b.astype(str)):
        sys.exit("round-trip check failed: tables do not rebuild rounds_ext.csv")
    ev.to_csv(f"{out}/ext_event_rounds.csv", index=False)
    pl.to_csv(f"{out}/ext_players.csv", index=False)
    pe.to_csv(f"{out}/ext_positions.csv", index=False)
    sc.to_csv(f"{out}/ext_scores.csv", index=False)
    print(f"wrote 4 tables to {out}: {len(sc):,} scores, {len(ev):,} event-rounds, {len(pl):,} players")


if __name__ == "__main__":
    main(*sys.argv[1:3])
