#!/usr/bin/env python3
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import mysql.connector
from mysql.connector import Error
import time

# ------------------------
# App & SocketIO
# ------------------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# ------------------------
# MySQL DB Config
# ------------------------
DB_HOST = 'localhost'
DB_USER = 'erik'
DB_PASSWORD = 'video'
DB_NAME = 'race_info'

# ------------------------
# Globals
# ------------------------
last_chipid = None
last_seen = 0

# ------------------------
# Helper functions
# ------------------------
def get_racer_info(chip_id):
    """Query MySQL for all races for a given ChipId."""
    try:
        conn = mysql.connector.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD, database=DB_NAME
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

        for row in results:
            if row.get("RaceDate") and not isinstance(row.get("RaceDate"), str):
                row["RaceDate"] = row["RaceDate"].isoformat()
        return results

    except Error as e:
        print(f"MySQL Error: {e}")
        return []
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

def validate_racer_data(r):
    """Return a tuple (is_valid, problems_list)"""
    required = ["FirstName","LastName","ChipId","Bib","Category","RaceDate"]
    problems = [f for f in required if not r.get(f)]
    return (len(problems) == 0, problems)

# ------------------------
# Routes
# ------------------------
@app.route("/wrapper")
def wrapper():
    return render_template("kiosk_wrapper.html")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/tag", methods=["POST"])
def api_tag():
    """Receive a new chipid and broadcast via WebSocket."""
    global last_chipid, last_seen
    data = request.get_json()
    if not data or "chipid" not in data:
        return jsonify({"error": "chipid required"}), 400

    last_chipid = data["chipid"]
    last_seen = time.time()

    racer_info = get_racer_info(last_chipid)
    emit_data = []

    if not racer_info:
        emit_data.append({"ChipId": last_chipid, "problem": ["Not registered"]})
    else:
        for r in racer_info:
            valid, problems = validate_racer_data(r)
            if not valid:
                r["problem"] = problems
            emit_data.append(r)

    # Emit to all clients
    socketio.emit("new_racer", emit_data)
    print(f"[{time.ctime()}] New chip scanned: {last_chipid} -> {emit_data}")
    return jsonify({"status": "ok", "chipid": last_chipid})

@app.route("/health")
def health():
    return "OK", 200

# ------------------------
# WebSocket events
# ------------------------
@socketio.on("connect")
def handle_connect():
    print(f"[{time.ctime()}] Client connected")

@socketio.on("disconnect")
def handle_disconnect():
    print(f"[{time.ctime()}] Client disconnected")

# ------------------------
# Background cleanup task
# ------------------------
def expire_chip_data():
    """Clears last_chipid after 10s to force idle screen."""
    global last_chipid, last_seen
    while True:
        if last_chipid and time.time() - last_seen > 10:
            print(f"[{time.ctime()}] Expiring chip {last_chipid}, sending idle")
            last_chipid = None
            socketio.emit("new_racer", [])  # empty list to force idle
        socketio.sleep(1)

# ------------------------
# Main
# ------------------------
if __name__ == "__main__":
    socketio.start_background_task(expire_chip_data)
    socketio.run(app, host="0.0.0.0", port=5000)


