import pandas as pd

# Verified live via GetStops. Same physical stop can have a different
# RouteStopID per route (e.g. Weber Loop is 419 under Gold, 301 under Blue).
KNOWN_HOLD_STOPS = {
    250: "North Ave Apts - To West Campus",
    328: "North Ave Apts - To West Campus",
    289: "North Ave Apts - To East Campus",
    273: "West Village",
    282: "West Village",
    313: "West Village",
    299: "Fitten Hall",
    312: "Fitten Hall",
    419: "Weber Loop",
    301: "Weber Building Loop",
    209: "GT Competition Center",
    396: "MARTA Midtown Station",
    385: "MARTA Midtown Station",
    337: "MARTA Midtown Station",
    392: "Clough Commons",
}
EXPECTED_HOLD_SECONDS = 180


def summarize_positions():
    df = pd.read_csv("data/positions.csv")
    print(f"\n[positions] {len(df)} rows, {df['VehicleID'].nunique()} unique vehicles")
    print(df.groupby("RouteID").size())


def summarize_capacities():
    df = pd.read_csv("data/capacities.csv")
    print(f"\n[capacities] {len(df)} rows, avg occupancy {(df['Percentage'].mean() * 100):.1f}%")


def summarize_visits():
    df = pd.read_csv("data/visits.csv")
    print(f"\n[visits] {len(df)} stop visits detected")
    if not len(df):
        return
    print(f"avg dwell: {df['dwell_seconds'].mean():.0f}s")
    print(f"avg travel to next stop: {df['travel_to_next_seconds'].mean():.0f}s")

    df["stop_name"] = df["RouteStopID"].map(KNOWN_HOLD_STOPS)
    known = df[df["stop_name"].notna()]
    if len(known):
        print(f"\n[known long-dwell stops] actual dwell vs. ~{EXPECTED_HOLD_SECONDS}s expected:")
        print(known.groupby("stop_name")["dwell_seconds"].agg(["mean", "count"]))


# Still open, needs the team: is dwell/travel time here good enough to
# build a corrected ETA on top of, or does the speed-threshold arrival
# detection in arrivals.py need the GPS-distance refinement first?


if __name__ == "__main__":
    summarize_positions()
    summarize_capacities()
    summarize_visits()
