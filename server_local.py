import os

# ✅ LOCAL IDENTIFIER (only used here)
os.environ["RUNNING_LOCAL"] = "1"

from server import app  # import the existing Flask app
from flask import jsonify
from supabase import create_client

# -------------------------------
# SUPABASE SETUP (READ-ONLY)
# -------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")


supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# -------------------------------
# LOCAL OVERRIDE: /api/combined
# -------------------------------
@app.route("/api/combined", methods=["GET"])
def local_api_combined():
    # 🧪 Pull orders from Supabase instead of Sheets
    orders_resp = (
        supabase
        .table("Production Orders TEST")
        .select("*")
        .limit(100)
        .execute()
    )


    orders = orders_resp.data if orders_resp.data else []

    return jsonify({
        "mode": "local_supabase",
        "orders": orders,
        "links": {}
    })


# -------------------------------
# RUN SERVER (LOCAL ONLY)
# -------------------------------
from server import socketio

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=10000,
        debug=True
    )

