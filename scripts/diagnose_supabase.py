"""
Run this locally to diagnose which counties/clubs are missing from Supabase.
Usage:
    pip install supabase
    python3 scripts/diagnose_supabase.py
"""

import csv
from pathlib import Path
from collections import defaultdict

SUPABASE_URL = "https://aadlyvremwukemzbwhth.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFhZGx5dnJlbXd1a2VtemJ3aHRoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzU3MzgyNzAsImV4cCI6MjA5MTMxNDI3MH0.4ErP3VCEWk-LLmkbgUkd30KpEBdx9_vC4HZ3lV2UIoA"
CSV_PATH = Path(__file__).parent.parent / "gaapitchfinder_data.csv"

TARGET_COUNTIES = [
    "Limerick", "Kilkenny",
    "Antrim", "Armagh", "Cavan", "Derry", "Donegal",
    "Down", "Fermanagh", "Monaghan", "Tyrone",
]


def load_csv():
    county_counts = defaultdict(int)
    clubs = set()
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            county_counts[row["County"]] += 1
            clubs.add(row["Club"])
    return county_counts, clubs


def fetch_all(client, table):
    """Fetch all rows from a table, handling Supabase's 1000-row default limit."""
    rows = []
    offset = 0
    limit = 1000
    while True:
        resp = client.table(table).select("*").range(offset, offset + limit - 1).execute()
        batch = resp.data
        rows.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return rows


def main():
    try:
        from supabase import create_client
    except ImportError:
        print("Install supabase-py first:  pip install supabase")
        return

    print(f"Connecting to {SUPABASE_URL}...")
    client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Discover table name — try common names
    table_name = None
    for candidate in ("pitches", "clubs", "gaa_pitches", "gaa_clubs", "gaapitchfinder", "grounds"):
        try:
            resp = client.table(candidate).select("count", count="exact").limit(1).execute()
            table_name = candidate
            print(f"Found table: '{table_name}'")
            break
        except Exception:
            pass

    if table_name is None:
        print("\nCould not auto-detect table name.")
        print("Please check your Supabase dashboard and re-run with the correct table name.")
        return

    # Fetch all rows
    print("Fetching all rows from database...")
    db_rows = fetch_all(client, table_name)
    print(f"Total rows in database: {len(db_rows)}")

    # Count by county in DB
    db_county_counts = defaultdict(int)
    db_clubs = set()
    county_col = None
    club_col = None

    if db_rows:
        # Detect column names
        sample = db_rows[0]
        print(f"\nColumns in table: {list(sample.keys())}")
        for col in sample:
            if col.lower() in ("county",):
                county_col = col
            if col.lower() in ("club", "club_name", "name"):
                club_col = col

        for row in db_rows:
            if county_col:
                db_county_counts[row[county_col]] += 1
            if club_col:
                db_clubs.add(row[club_col])

    # Load CSV counts
    csv_county_counts, csv_clubs = load_csv()

    # Compare
    print("\n" + "=" * 70)
    print("COUNTY COMPARISON: CSV vs Database")
    print("=" * 70)
    print(f"{'County':<15} {'CSV':>6} {'DB':>6} {'Diff':>6} {'Status'}")
    print("-" * 70)

    all_counties = sorted(set(list(csv_county_counts.keys()) + list(db_county_counts.keys())))
    problems = []
    for county in all_counties:
        csv_n = csv_county_counts.get(county, 0)
        db_n = db_county_counts.get(county, 0)
        diff = db_n - csv_n
        if db_n == 0 and csv_n > 0:
            status = "*** COMPLETELY MISSING ***"
            problems.append(county)
        elif diff < 0:
            status = f"missing {abs(diff)} clubs"
            problems.append(county)
        elif diff > 0:
            status = f"{diff} extra in DB"
        else:
            status = "OK"
        print(f"{county:<15} {csv_n:>6} {db_n:>6} {diff:>+6}  {status}")

    print("\n" + "=" * 70)
    if problems:
        print(f"PROBLEM COUNTIES ({len(problems)}): {', '.join(problems)}")

        # Find clubs in CSV but not in DB
        missing_clubs = csv_clubs - db_clubs
        if missing_clubs and club_col:
            print(f"\nClubs in CSV but NOT in database ({len(missing_clubs)}):")
            for club in sorted(missing_clubs)[:30]:
                print(f"  - {club}")
            if len(missing_clubs) > 30:
                print(f"  ... and {len(missing_clubs) - 30} more")
    else:
        print("All counties match between CSV and database.")

    # Check for null coordinates in DB
    if db_rows:
        lat_col = next((c for c in db_rows[0] if "lat" in c.lower()), None)
        lon_col = next((c for c in db_rows[0] if "lon" in c.lower()), None)
        if lat_col and lon_col:
            null_coords = [r for r in db_rows if not r.get(lat_col) or not r.get(lon_col)]
            if null_coords:
                print(f"\nRows with null coordinates in DB: {len(null_coords)}")
                for r in null_coords[:10]:
                    print(f"  {r.get(club_col,'?')} ({r.get(county_col,'?')})")


if __name__ == "__main__":
    main()
