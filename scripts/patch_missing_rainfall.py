import csv
import requests
import time
from pathlib import Path

DATA_PATH = Path(__file__).parent.parent / "gaapitchfinder_data.csv"
# Use same reference period as the original data collection
START_DATE = "2010-01-01"
END_DATE = "2022-12-31"


def fetch_rainfall(lat, lon):
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": START_DATE,
        "end_date": END_DATE,
        "daily": "precipitation_sum",
        "timezone": "auto",
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    daily = data["daily"]["precipitation_sum"]

    # Split into calendar years and average
    dates = data["daily"]["time"]
    years = {}
    for date, val in zip(dates, daily):
        year = date[:4]
        if year not in years:
            years[year] = {"total": 0.0, "rain_days": 0}
        if val is not None:
            years[year]["total"] += val
            if val > 0:
                years[year]["rain_days"] += 1

    n = len(years)
    avg_rainfall = round(sum(y["total"] for y in years.values()) / n, 1)
    avg_rain_days = round(sum(y["rain_days"] for y in years.values()) / n, 1)
    return avg_rainfall, avg_rain_days


def main():
    rows = []
    with open(DATA_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    missing = [(i, r) for i, r in enumerate(rows) if not r.get("annual_rainfall", "").strip()]
    if not missing:
        print("No clubs are missing rainfall data.")
        return

    print(f"Found {len(missing)} clubs missing rainfall data:")
    for _, r in missing:
        print(f"  {r['Club']} ({r['County']}, {r['Country']}) lat={r['Latitude']} lon={r['Longitude']}")

    print()
    for i, row in missing:
        lat = row["Latitude"].strip()
        lon = row["Longitude"].strip()
        if not lat or not lon:
            print(f"  Skipping {row['Club']} — no coordinates")
            continue

        print(f"  Fetching: {row['Club']} ({row['County']})... ", end="", flush=True)
        try:
            rainfall, rain_days = fetch_rainfall(float(lat), float(lon))
            rows[i]["annual_rainfall"] = str(rainfall)
            rows[i]["rain_days"] = str(rain_days)
            print(f"{rainfall}mm, {rain_days} rain days")
        except Exception as e:
            print(f"ERROR: {e}")
        time.sleep(1)

    with open(DATA_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nDone. Updated {DATA_PATH}")


if __name__ == "__main__":
    main()
