# app.py
from flask import Flask, request, jsonify
import pgeocode

app = Flask(__name__)

@app.route("/verify_zip", methods=["POST"])
def verify_zip():
    data = request.get_json(force=True)
    country = data.get("country")
    state = (data.get("state") or "").strip().lower()
    city = (data.get("city") or "").strip().lower()
    postal = str(data.get("zip") or "").strip()

    if not (country and postal):
        return jsonify({"valid": False, "message": "Country and zip required"}), 400

    try:
        nomi = pgeocode.Nominatim(country)
    except Exception:
        return jsonify({"valid": False, "message": f"Unsupported country {country}"}), 400

    rec = nomi.query_postal_code(postal)

    if rec is None or rec.empty or str(rec["place_name"]) == "nan":
        return jsonify({"valid": False, "message": "Postal code not found"})

    # Normalize and compare
    valid_city = city in str(rec["place_name"]).lower()
    valid_state = state in str(rec["state_name"]).lower() if state else True

    if valid_city and valid_state:
        return jsonify({"valid": True, "message": "Matches", "record": rec.to_dict()})
    else:
        return jsonify({
            "valid": False,
            "message": "Mismatch in city/state",
            "expected": {"city": rec["place_name"], "state": rec["state_name"]}
        })

if __name__ == "__main__":
    app.run(port=8000, debug=True)
