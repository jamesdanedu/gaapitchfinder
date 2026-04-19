"""
Fixes duplicate rows and null coordinates in Supabase.
Run locally:  python3 scripts/fix_supabase_duplicates.py
"""

SUPABASE_URL = "https://aadlyvremwukemzbwhth.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFhZGx5dnJlbXd1a2VtemJ3aHRoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzU3MzgyNzAsImV4cCI6MjA5MTMxNDI3MH0.4ErP3VCEWk-LLmkbgUkd30KpEBdx9_vC4HZ3lV2UIoA"
TABLE = "pitches"

# Correct coordinates for St. Faithleach's from the CSV
FAITHLEACHS_LAT = 53.6761571
FAITHLEACHS_LON = -8.01000103


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
    print(f"Total rows: {len(rows)}")

    # --- 1. Find and remove duplicates ---
    # Group by (club, county) and keep only the lowest id
    from collections import defaultdict
    groups = defaultdict(list)
    for row in rows:
        key = (row["club"], row["county"])
        groups[key].append(row)

    ids_to_delete = []
    for (club, county), group in groups.items():
        if len(group) > 1:
            # Sort by id, keep smallest, delete the rest
            group.sort(key=lambda r: r["id"])
            dupes = group[1:]
            print(f"DUPLICATE: '{club}' ({county}) — keeping id={group[0]['id']}, "
                  f"deleting ids={[d['id'] for d in dupes]}")
            ids_to_delete.extend(d["id"] for d in dupes)

    if ids_to_delete:
        print(f"\nDeleting {len(ids_to_delete)} duplicate rows...")
        for id_ in ids_to_delete:
            client.table(TABLE).delete().eq("id", id_).execute()
            print(f"  Deleted id={id_}")
    else:
        print("No duplicates found.")

    # --- 2. Fix null coordinates for St. Faithleach's ---
    null_coords = [
        r for r in rows
        if not r.get("latitude") or not r.get("longitude")
    ]
    if null_coords:
        print(f"\nRows with null coordinates: {len(null_coords)}")
        for row in null_coords:
            club = row["club"]
            if "faithleach" in club.lower() or "faithleachs" in club.lower():
                print(f"  Fixing coordinates for '{club}' (id={row['id']}): "
                      f"lat={FAITHLEACHS_LAT}, lon={FAITHLEACHS_LON}")
                client.table(TABLE).update({
                    "latitude": FAITHLEACHS_LAT,
                    "longitude": FAITHLEACHS_LON,
                }).eq("id", row["id"]).execute()
            else:
                print(f"  WARNING: null coordinates for '{club}' (id={row['id']}) "
                      f"— manual fix needed")
    else:
        print("No null coordinates found.")

    # --- Final count ---
    final = fetch_all(client, TABLE)
    print(f"\nDone. Rows before: {len(rows)}  Rows after: {len(final)}")


if __name__ == "__main__":
    main()
