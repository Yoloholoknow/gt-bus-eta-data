"""Export each route's 120-minute averages and unbucketed fallback CSVs."""

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

try:
    from .build_predictions import COLUMNS, ROOT, atomic_csv
except ImportError:
    from build_predictions import COLUMNS, ROOT, atomic_csv


def split_averages(source_path, output_dir, routes_path=ROOT / "reference/routes.json"):
    """Preserve source columns and rows, retaining only bucket widths 120 and 0."""
    source_path, output_dir = Path(source_path), Path(output_dir)
    route_names = {
        str(route["RouteID"]): str(route["Description"]).strip()
        for route in json.loads(Path(routes_path).read_text(encoding="utf-8"))
    }
    routes = defaultdict(lambda: {120: [], 0: []})
    with source_path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        columns = reader.fieldnames
        if not set(COLUMNS).issubset(columns or []):
            raise ValueError(f"Averages CSV must contain columns: {', '.join(COLUMNS)}")
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed averages row at line {reader.line_num}")
            if row["bucket_minutes"] not in ("0", "120"):
                continue
            rid = row["route_id"]
            if not rid or any(char not in "0123456789" for char in rid):
                raise ValueError(f"Invalid route ID at line {reader.line_num}: {rid!r}")
            routes[rid][int(row["bucket_minutes"])].append(row)
    if not routes:
        raise ValueError("No 120-minute or unbucketed averages found")
    for rid in routes:
        name = route_names.get(rid, "")
        if not name or any(char in name for char in '/\\\0'):
            raise ValueError(f"Missing or invalid route name for route {rid}")

    counts = {}
    for rid, groups in sorted(routes.items(), key=lambda item: int(item[0])):
        route_dir = output_dir / f"{rid} - {route_names[rid]}"
        for width, filename in ((120, "bucketed.csv"), (0, "unbucketed.csv")):
            target = route_dir / filename
            if target.resolve() == source_path.resolve():
                raise ValueError("Output must not overwrite the source averages CSV")
            atomic_csv(target, columns, groups[width])
        counts[rid] = {"bucketed_rows": len(groups[120]), "unbucketed_rows": len(groups[0])}
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--averages", type=Path, default=ROOT / "prediction/averages.csv")
    parser.add_argument("--routes", type=Path, default=ROOT / "reference/routes.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "prediction/route_averages",
                        help="Write <route_id> - <route_color>/bucketed.csv and unbucketed.csv here")
    args = parser.parse_args()
    try:
        counts = split_averages(args.averages, args.output_dir, args.routes)
    except (OSError, ValueError, csv.Error) as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(json.dumps(counts, indent=2))
    print(f"Wrote route files to {args.output_dir}")


if __name__ == "__main__":
    main()
