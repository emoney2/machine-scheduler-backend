# ups_service.py

import os
import time
import requests

# Pull these from your environment (e.g., .env or Render config)
UPS_CLIENT_ID     = os.getenv("UPS_CLIENT_ID")
UPS_CLIENT_SECRET = os.getenv("UPS_CLIENT_SECRET")
UPS_ACCOUNT       = os.getenv("UPS_ACCOUNT")

# UPS sandbox OAuth2 & Rate endpoints
OAUTH_URL = "https://wwwcie.ups.com/security/v1/oauth/token"
RATE_URL  = "https://wwwcie.ups.com/ship/v1/rating/Rate"

# In‑process token cache to avoid fetching on every call
_token_cache = {}

def _get_access_token():
    now = time.time()
    if _token_cache.get("expires_at", 0) > now:
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
    """
    :param shipper:   dict with fields Name, AttentionName, Phone, Address{...}
    :param recipient: same shape as shipper
    :param packages:  list of dicts with keys:
                       - PackagingType (UPS code, e.g. "02")
                       - Weight (number, in lbs)
                       - Dimensions (optional dict {Length, Width, Height} in inches)
    :returns:         list of rate dicts:
                       [{serviceCode, method, rate, currency, deliveryDate}, ...]
    """
    token = _get_access_token()
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {token}"
    }

    # Build the Shipment payload
    shipment = {
        "Shipper":  {**shipper,  "ShipperNumber": UPS_ACCOUNT},
        "ShipTo":   recipient,
        "ShipFrom": shipper,
        "Package":  []
    }

    for pkg in packages:
        dims = pkg.get("Dimensions", {})
        pkg_obj = {
            "PackagingType": {"Code": pkg.get("PackagingType", "02")},
            "PackageWeight": {
                "UnitOfMeasurement": {"Code": "LBS"},
                "Weight":             str(pkg["Weight"])
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
            "Request":  {"RequestOption": "Rate"},
            "Shipment": shipment
        }
    }

    resp = requests.post(RATE_URL, json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    # Extract the relevant rate details
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
