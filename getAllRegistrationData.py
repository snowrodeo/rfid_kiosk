#!/usr/bin/env python3
from datetime import datetime
import requests
import argparse
import subprocess
from datetime import datetime
import mysql.connector
from mysql.connector import Error

API_ID = 11616
URL = f"https://www.webscorer.com/json/mystartlists?apiid={API_ID}&filt=R"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/114.0.0.0 Safari/537.36"
}

DB_HOST = 'localhost'
DB_USER = 'erik'
DB_PASSWORD = 'video'
DB_NAME = 'race_info'

def get_race_ids_for_date(target_date):
    resp = requests.get(URL, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    race_ids = []
    for race in data.get("StartLists", []):
        race_date_str = race.get("Date")
        if not race_date_str:
            continue
        race_date = datetime.strptime(race_date_str, "%b %d, %Y").date()
        if race_date == target_date:
            race_ids.append((race.get("RaceId"), race.get("Name",""), race_date))
    return race_ids

def ensure_race_in_db(race_id, name, race_date):
    try:
        conn = mysql.connector.connect(host=DB_HOST,user=DB_USER,password=DB_PASSWORD,database=DB_NAME)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT IGNORE INTO races (RaceId, Name, Date) VALUES (%s, %s, %s)
        """, (race_id, name, race_date))
        conn.commit()
    except Error as e:
        print(f"MySQL Error inserting RaceID {race_id}: {e}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


# Days of week to run (Mon=0 .. Sun=6)
ALLOWED_DAYS = { 6}  # Saturday=5, Sunday=6

def main():
    parser = argparse.ArgumentParser(description="Fetch race IDs for a specific date and call getRegistrationDataByRaceID.py")
    parser.add_argument("-d", "--date", type=str, help="Target date in MM/DD/YY format (default today)")
    parser.add_argument("-p", "--parallel", action="store_true", help="Run fetches in parallel")
    args = parser.parse_args()

    if args.date:
        # If date explicitly provided, always run
        target_date = datetime.strptime(args.date, "%m/%d/%y").date()
    else:
        # Default = today, but only run on allowed days
        today = datetime.today()
        if today.weekday() not in ALLOWED_DAYS:
            print(f"Today ({today.strftime('%A')}) is not in allowed days, exiting.")
            return
        target_date = today.date()

    print(f"Fetching races for {target_date}")
    race_entries = get_race_ids_for_date(target_date)
    print(f"Found {len(race_entries)} races: {[r[0] for r in race_entries]}")

    processes = []
    for race_id, name, race_date in race_entries:
        # Ensure race exists in DB
        ensure_race_in_db(race_id, name, race_date)
        print(f"\nFetching registration data for RaceID {race_id}...")
        cmd = ["./getRegistrationDataByRaceID.py", str(race_id)]
        if args.parallel:
            p = subprocess.Popen(cmd)
            processes.append((race_id, p))
        else:
            subprocess.run(cmd, check=True)

    if args.parallel:
        for race_id, p in processes:
            p.wait()

if __name__ == "__main__":
    main()

