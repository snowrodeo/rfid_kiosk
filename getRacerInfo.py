#!/usr/bin/env python3
import mysql.connector
import argparse
import json
from mysql.connector import Error

# ---------- CONFIG ----------
DB_HOST = 'localhost'
DB_USER = 'erik'
DB_PASSWORD = 'video'
DB_NAME = 'race_info'

# ---------- FUNCTIONS ----------
def get_racer_data_by_chip(chip_id):
    try:
        conn = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT r.FirstName, r.LastName, rp.ChipId, rp.Bib, rp.Category, ra.Date AS RaceDate
            FROM racers r
            JOIN race_participants rp ON r.RacerId = rp.RacerId
            JOIN races ra ON rp.RaceId = ra.RaceId
            WHERE rp.ChipId = %s
        """
        cursor.execute(query, (chip_id,))
        results = cursor.fetchall()

        # Convert datetime.date to string for JSON serialization
        for row in results:
            if isinstance(row.get("RaceDate"), (str,)):
                continue
            row["RaceDate"] = row["RaceDate"].isoformat()

        return results

    except Error as e:
        print(f"MySQL Error: {e}")
        return []
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()

# ---------- MAIN ----------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch racer data by ChipId")
    parser.add_argument('chipid', type=str, help='ChipId to look up')
    args = parser.parse_args()

    data = get_racer_data_by_chip(args.chipid)
    print(json.dumps(data, indent=2))


