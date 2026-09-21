import glob
import json

import pandas as pd

rows = []
for path in sorted(glob.glob("data/*.jsonl")):
    with open(path) as f:
        for line in f:
            rows.append(json.loads(line))

print(f"{len(rows)} polls loaded")

vehicles = pd.DataFrame(
    v | {"fetched_at": r["fetched_at"]}
    for r in rows
    for v in r["vehicles"]
)
print(vehicles.head())
print(vehicles.shape)

# Starting point only. Open questions for the team:
#   - what's the actual target variable (predicted vs. actual arrival error)?
#   - how do we derive "actual arrival" from the estimates data?
#   - what should results be grouped by (route? stop? time of day?)?
