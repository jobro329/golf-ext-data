# DP World / LIV pull validation

File: `output/rounds_ext.csv`, 105,129 rows

## Checks
- PASS **schema**: columns match the model schema
- PASS **tour values**: ['eur', 'liv']
- PASS **scores are real strokes**: 0 rows with missing/non-integer/out-of-range scores
- PASS **par present**: 0 rows with missing/odd par
- WARN **playoff rows**: 4 rows with round > 4 in 2 events: ESPN logs playoffs as an extra period. Not real 18-hole rounds; the model's field-size filter keeps them out of ratings.
- PASS **no placeholder rounds**: 0 event-rounds with no real spread of scores
- PASS **scoring averages plausible**: 0 event-rounds averaging outside -6/+8 vs par
- PASS **field sizes**: 0 events whose largest round field is under 30 (DP World) / 40 (LIV)
- WARN **partial rounds**: 504 rows whose par differs from the event-round par (partial rounds). The model drops these automatically.
- WARN **Dunhill Links R1-3**: 2859 rows from the three-course rotation. The model drops these automatically.
- PASS **no duplicate player-rounds**: 0 repeated tournament_id + round + player_id rows
- PASS **one tour and name per event id**: 0 ids on two tours, 0 ids with two names
- PASS **no duplicate tournaments**: 0 event ids sharing a name and start date
- PASS **no overlap with PGA Tour feed**: 0 events already in the PGA data
- PASS **player ids match the model**: 2218 of 2218 ids already in our crosswalk carry the same player (0.00% mismatch)
- WARN **new player ids**: 12 ids not yet in the crosswalk (new names; the model adds them). e.g. ['Jack Dyer', 'Louis Dobbelar', 'Steve Lewton', 'Damien McGrane', 'JR Galbraith', 'Greg Dalziel', 'Coby Carruthers', 'Matt Jager']
- PASS **leaderboards consistent**: lowest total holds 1st in 100.0% of events; median Spearman(position, total) 0.997
- PASS **baseline rows still present**: 100.00% of 105,129 previously validated rows found
- PASS **baseline scores unchanged**: 100.00% identical
- PASS **row count not shrinking**: 105,129 rows now vs 105,129 before

## New since last copy
0 rows beyond the baseline, 0 new events:

## Coverage
```
           events  rounds
tour year                
eur  2018      13    5701
     2020       7    2515
     2021      34   13812
     2022      39   15857
     2023      37   15451
     2024      36   14881
     2025      35   14601
     2026      30   12519
liv  2022       7    1003
     2023      13    1862
     2024      13    2096
     2025      13    2099
     2026      12    2732
```

## Result: PASSED (0 failures, 4 warnings)
