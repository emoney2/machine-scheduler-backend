import os
import time
import requests

# Pull these from your .env
UPS_CLIENT_ID     = os.getenv("UPS_CLIENT_ID")
UPS_CLIENT_SECRET = os.getenv("UPS_CLIENT_SECRET")
UPS_ACCOUNT       = os.getenv("UPS_ACCOUNT")

# Sandbox endpoints
OAUTH_URL = "https://wwwcie.ups.com/security/v1/oauth/token"
RATE_URL  = "https://wwwcie.ups.com/ship/v1/rating/Rate"

# Simple in‑process token cache
_token_cache = {}

def _get_access_token():
    now = time.time()
    if _token_cache and _token_cache["expires_at"] > now:
        return _token_cache["access_token"]

    resp = requests.post(
        OAUTH_URL,
        auth=(UPS_CLIENT_ID, UPS_CLIENT_SECRET),
        data={"grant_type": "client_credentials"}
    )
    resp.raise_for_status()
    token_data = resp.json()
    access_token = token_data["access_token"]
    expires_in   = token_data.get("expires_in", 86400)
    _token_cache.update({
        "access_token": access_token,
        "expires_at":    now + expires_in - 60
    })
    return access_token

def get_rate(shipper, recipient, packages):
    token = _get_access_token()
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {token}"
    }

    # Build the core Shipment block
    shipment = {
        "Shipper":   {**shipper,   "ShipperNumber": UPS_ACCOUNT},
        "ShipTo":    recipient,
        "ShipFrom":  shipper,
        "Package":   []
    }

    for pkg in packages:
        dims = pkg.get("Dimensions", {})
        pkg_obj = {
            "PackagingType": {"Code": pkg.get("PackagingType", "02")},
            "PackageWeight": {
                "UnitOfMeasurement": {"Code": "LBS"},
                "Weight": str(pkg["Weight"])
            }
        }
        if dims:
            pkg_obj["Dimensions"] = {
                "UnitOfMeasurement": {"Code": "IN"},
                "Length":            str(dims["Length"]),
                "Width":             str(dims["Width"]),
                "Height":            str(dims["Height"])
            }
        shipment["Package"].append(pkg_obj)

    payload = {
        "RateRequest": {
            "Request": {"RequestOption": "Rate"},
            "Shipment": shipment
        }
    }

    resp = requests.post(RATE_URL, json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for rs in data.get("RateResponse", {}).get("RatedShipment", []):
        svc     = rs["Service"]
        charges = rs["TotalCharges"]
        results.append({
            "serviceCode":  svc["Code"],
            "method":       svc.get("Description", ""),
            "rate":         charges["MonetaryValue"],
            "currency":     charges["CurrencyCode"],
            "deliveryDate": rs.get("GuaranteedDelivery", {}).get("Date", "")
        })
    return results
