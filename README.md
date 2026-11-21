# DP World Tour + LIV weekly data pull (GitHub Actions)

Every Tuesday this repo pulls DP World Tour and LIV round scores from ESPN's public API, checks them, and saves
them in the exact format the golf model already uses. Nothing here touches the model itself. It only produces the
input file the model reads (`rounds_ext.csv`).

## What's in here

| Path | What it is |
|---|---|
| `scripts/pull_espn_tours.py` | The same pull the model uses (same parsing, same columns). Adds `--skip-file` and `--out`, and rolls the season range forward automatically. |
| `scripts/validate.py` | Checks every pull before it's saved (see below). If any check fails, nothing is saved. |
| `scripts/split_tables.py` | Writes the four small tables the Claude project stores (`ext_scores`, `ext_event_rounds`, `ext_players`, `ext_positions`), after the same cleaning the model applies. |
| `reference/players.csv` | The model's ESPN player crosswalk (3,499 players), used to check that player ids still mean the same people. |
| `reference/pga_feed_event_ids.csv` | The 404 ESPN event ids already in our PGA Tour data (majors, co-sanctioned events), so they're never counted twice. |
| `data/rounds_ext.csv` | The last validated pull (105,129 rounds through the Nov 2026 DP World Tour Championship). Each week's pull is compared against it. |
| `.github/workflows/weekly_pull.yml` | The schedule: Tuesdays 09:17 UTC, plus a manual "Run workflow" button. |
| `requirements.txt` | pandas, pyarrow, numpy (tested versions, Python 3.13). |

## One-time setup (about 10 minutes)

1. **Create a repo.** On github.com: New repository, name it e.g. `golf-ext-data`. Private is fine.
2. **Upload the files.** On the new repo page choose *uploading an existing file*, drag in everything from this ZIP
   (keep the folders), and commit. The `.github` folder starts with a dot, so on a Mac press Cmd+Shift+. in Finder
   to see it. If drag-and-drop skips it, create the file in GitHub instead: *Add file > Create new file*, name it
   `.github/workflows/weekly_pull.yml`, and paste the contents.
3. **Allow the workflow to save data.** Settings > Actions > General > Workflow permissions: choose
   **Read and write permissions** and Save. (Without this the run works but can't commit the new data.)
4. **Run it once by hand.** Actions tab > *DP World + LIV weekly pull* > **Run workflow**. It takes about 10-15
   minutes. A green check means the data passed every check and was saved. Open the run to see the validation report.
5. **Get notified.** GitHub emails you when a scheduled run fails (on by default under your profile's Settings >
   Notifications > Actions).

## Getting the files each week

Either of these, after the Tuesday run:

- **From the repo:** the `data/` folder always holds the latest validated copy:
  - `data/rounds_ext.csv` and `data/rounds_ext.parquet` (full file, model schema)
  - `data/project_tables/ext_*.csv` (the four small tables for the Claude project)
  - `data/validation_report.md` (what was checked and what's new this week)
  - `data/status.json` (written after every run, pass or fail: conclusion, finish time, time of the last good run). The Claude model reads this to tell whether this week's data is real and fresh.

  Click a file, then the download button (top right of the file view).
- **From the run:** Actions > the run > *Artifacts* > `dpwt-liv-<number>` (a ZIP of everything, kept 30 days).
  Failed runs keep their files here too, so you can see what went wrong. Failed data is never committed.

**Giving it to Claude:** nothing to do. The Claude model's Tuesday 2:34 PM Atlantic run downloads `data/status.json`
and the four `data/project_tables/ext_*.csv` files straight from this repo (raw.githubusercontent.com), checks them
again, and uses them. That only works while the repo is **public**: Claude's scheduled runs have no GitHub login,
so a private repo returns 404 and the model falls back to the last validated copy (and says so).

## The checks (`scripts/validate.py`)

Any FAIL stops the run, and nothing is saved.

1. **Schema:** the exact 11 columns, in the order the model reads them.
2. **Actual round scores:** integer strokes 55-95 and par 60-75 on every row. Every event-round must show a real
   spread of scores. (ESPN sometimes serves a stale pre-tournament page with everyone at "E" and no scores; this
   catches it.) Plausible field sizes, and scoring averages within -6/+8 of par.
3. **Duplicates:** no repeated tournament + round + player; one tour and name per event id; no two ids for the same
   tournament (same name and start date); no event that's already in our PGA Tour data.
4. **Player ids match the model:** every ESPN id already in our crosswalk must be the same player (the same name
   normalization the model uses, including known spellings). More than 1% mismatches fails. New ids are listed as
   warnings; the model adds them itself.
5. **Leaderboards:** the lowest 72-hole total holds 1st place in at least 95% of events, and finish position tracks
   total score.
6. **Nothing lost or changed:** at least 98% of last week's validated rows are still there, at least 99.5% of
   those with identical scores, and the file didn't shrink by more than 2%.

Warnings (don't stop the run): partial rounds and Dunhill Links rounds 1-3 (the model drops both), ESPN's playoff
rows (logged as a round 5; the model's field-size filter excludes them), new player ids, and ids with two spellings.

The checks were tested against the current data (passes) and against eight deliberately broken files: a
placeholder event, duplicate rows, a tournament under a second id, scrambled player ids, an event overlapping the
PGA feed, a missing season, 2,000 altered historic scores, and a renamed column. Each one failed as it should.

## Good to know

- **Schedule:** the cron is in UTC. GitHub sometimes starts scheduled runs late during busy periods, which is why it
  runs early Tuesday morning.
- **Inactive repos:** GitHub pauses schedules in repos with no activity for 60 days. The weekly commits keep it
  active; if a quiet stretch (no new events) ever pauses it, re-enable it on the Actions tab.
- **If ESPN blocks GitHub's servers or changes its format,** the run fails and you get an email. Run
  `python scripts/pull_espn_tours.py --inspect --out output` to save a sample of ESPN's raw response and share it
  with Claude to fix the parser.
- **Reference files** are a snapshot from Nov 21 2026. They don't need regular updates. If the model's player
  crosswalk changes a lot, ask Claude for a fresh `reference/players.csv`.
