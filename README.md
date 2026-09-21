# gt-bus-eta-data

Skeleton for collecting GT bus data (`bus.gatech.edu`, no API key needed) and figuring out, as a team, how to analyze it toward better ETA predictions.

Deliberately minimal — this is a starting point for the team to design the analysis together, not a finished pipeline.

## Setup

```
pip install -r requirements.txt
```

## Poll

```
python3 poller.py
```

Polls vehicle positions + stop estimates every 5s, appends to `data/YYYY-MM-DD.jsonl`. Ctrl+C to stop.

## Analyze

```
python3 analyze.py
```

Loads whatever's in `data/` into a dataframe and prints a shape check. Everything past that is open — see the questions at the bottom of `analyze.py`.

Related issues: gtiosclub/Georgia-Tech-App#237-#241.
