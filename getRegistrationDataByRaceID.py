#!/usr/bin/env python3
import requests
import mysql.connector
from mysql.connector import Error
from datetime import datetime
import argparse
import sys

# ---------- CONFIG ----------
DB_HOST = 'localhost'
DB_USER = 'erik'
DB_PASSWORD = 'video'
DB_NAME = 'race_info'

API_PRIV = '766ffb66'
API_ID = '11616'

# ---------- FUNCTIONS ----------
def fetch_race_json(raceid):
    url = f"https://www.webscorer.com/json/registerlist?raceid={raceid}&apiid={API_ID}&apipriv={API_PRIV}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/115.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

def save_race_data(race_json):
    result = {
        'race_id': race_json['RaceInfo']['RaceId'],
        'added': 0,
        'updated': 0,
        'unchanged': 0,
        'race_status': 'ok',
        'error': None
    }
    try:
        conn = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        cursor = conn.cursor()

        # ---- Insert/update race info ----
        race = race_json['RaceInfo']
        race_id = int(race['RaceId'])
        start_time = datetime.strptime(race['StartTime'].split(' (')[0], "%A, %B %d, %Y %I:%M %p")
        race_date = datetime.strptime(race.get('Date','1970-01-01'), '%b %d, %Y').date()

        cursor.execute("""
            INSERT INTO races (RaceId, Name, City, Date, StartTime, Type)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE Name=VALUES(Name), City=VALUES(City), Date=VALUES(Date), StartTime=VALUES(StartTime), Type=VALUES(Type)
        """, (
            race_id,
            race.get('Name', ''),
            race.get('City', ''),
            race_date,
            start_time,
            race.get('Type', '')
        ))

        # ---- Insert/update participants ----
        for p in race_json.get('StartList', []):
            # Upsert racer
            cursor.execute("""
                INSERT INTO racers (FirstName, LastName, Email, Gender, YearOfBirth, Age, TeamName)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    Gender=VALUES(Gender), YearOfBirth=VALUES(YearOfBirth),
                    Age=VALUES(Age), TeamName=VALUES(TeamName)
            """, (
                p.get('FirstName',''),
                p.get('LastName',''),
                p.get('Email',''),
                p.get('Gender',''),
                p.get('YearOfBirth'),
                p.get('Age'),
                p.get('TeamName','')
            ))
            # Get RacerId
            cursor.execute("SELECT RacerId FROM racers WHERE FirstName=%s AND LastName=%s AND Email=%s",
                           (p.get('FirstName',''), p.get('LastName',''), p.get('Email','')))
            racer_id = cursor.fetchone()[0]

            # Upsert race_participants
            cursor.execute("""
                INSERT INTO race_participants (RaceId, RacerId, Bib, ChipId, Category)
                VALUES (%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE Bib=VALUES(Bib), ChipId=VALUES(ChipId), Category=VALUES(Category)
            """, (
                race_id,
                racer_id,
                p.get('Bib'),
                p.get('ChipId'),
                p.get('Category','')
            ))

        conn.commit()
        result['added'] = len(race_json.get('StartList', []))
    except Error as e:
        result['race_status'] = 'error'
        result['error'] = str(e)
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()
    return result

# ---------- MAIN ----------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Fetch race registration data and store in MySQL.')
    parser.add_argument('raceid', type=int, help='Race ID to fetch')
    args = parser.parse_args()

    try:
        data = fetch_race_json(args.raceid)
        res = save_race_data(data)
        print(res)
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

