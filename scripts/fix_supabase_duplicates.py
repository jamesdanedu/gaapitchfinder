"""
Fixes null coordinates in Supabase for pitches that are missing lat/lon.

Multiple pitches per club (e.g. Kenmare Shamrocks, St Brigid's Kiltoom,
St Faithleach's) are legitimate — this script does NOT delete them.

Run locally:  python3 scripts/fix_supabase_duplicates.py
"""

SUPABASE_URL = "https://aadlyvremwukemzbwhth.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFhZGx5dnJlbXd1a2VtemJ3aHRoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzU3MzgyNzAsImV4cCI6MjA5MTMxNDI3MH0.4ErP3VCEWk-LLmkbgUkd30KpEBdx9_vC4HZ3lV2UIoA"
TABLE = "pitches"

# Fallback coordinates from the CSV for clubs whose pitches lack coordinates.
# Both St Faithleach's pitches share the club grounds area — update with precise
# per-pitch coordinates once they are available.
FALLBACK_COORDS = {
    "st faithleachs gaa": (53.6761571, -8.01000103),
    "st. faithleachs gaa": (53.6761571, -8.01000103),
}


def fetch_all(client, table):
    rows, offset, limit = [], 0, 1000
    while True:
        batch = client.table(table).select("*").range(offset, offset + limit - 1).execute().data
        rows.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return rows


def main():
    try:
        from supabase import create_client
    except ImportError:
        print("pip install supabase")
        return

    client = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("Fetching all rows...")
    rows = fetch_all(client, TABLE)
    print(f"Total rows in database: {len(rows)}")

    # Fix null coordinates — multiple pitches per club are intentional
    null_coords = [r for r in rows if not r.get("latitude") or not r.get("longitude")]
    if not null_coords:
        print("No null coordinates found — nothing to do.")
        return

    print(f"\nRows with null coordinates: {len(null_coords)}")
    fixed = 0
    for row in null_coords:
        club_key = row["club"].strip().lower()
        pitch = row.get("pitch_name") or "(no pitch name)"
        coords = FALLBACK_COORDS.get(club_key)
        if coords:
            lat, lon = coords
            print(f"  Fixing: '{row['club']}' / '{pitch}' (id={row['id']}) -> {lat}, {lon}")
            client.table(TABLE).update({"latitude": lat, "longitude": lon}).eq("id", row["id"]).execute()
            fixed += 1
        else:
            print(f"  WARNING: no fallback coords for '{row['club']}' / '{pitch}' "
                  f"(id={row['id']}, county={row['county']}) — update manually")

    print(f"\nFixed {fixed}/{len(null_coords)} rows.")
    if fixed < len(null_coords):
        print("Remaining rows need coordinates added manually via the Supabase dashboard.")


if __name__ == "__main__":
    main()
