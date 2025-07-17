# ups_service.py

import os
import time
import json
import logging
import requests

# Load UPS OAuth2 credentials and account number from environment
UPS_CLIENT_ID     = os.getenv("UPS_CLIENT_ID")
UPS_CLIENT_SECRET = os.getenv("UPS_CLIENT_SECRET")
UPS_ACCOUNT       = os.getenv("UPS_ACCOUNT")
if not (UPS_CLIENT_ID and UPS_CLIENT_SECRET and UPS_ACCOUNT):
    raise RuntimeError(
        "UPS credentials missing – ensure UPS_CLIENT_ID, UPS_CLIENT_SECRET, and UPS_ACCOUNT are set"
    )

# Sandbox OAuth2 & Rate endpoints
OAUTH_URL = "https://wwwcie.ups.com/security/v1/oauth/token"
RATE_URL  = "https://wwwcie.ups.com/ship/v1/rating/Rate"

# In‑process cache for the access token
_token_cache = {}

def _get_access_token():
    now = time.time()
    # Reuse cached token if still valid
    if _token_cache.get("expires_at", 0) > now:
        return _token_cache["access_token"]

    # Request a new token with Rate scope
    resp = requests.post(
        OAUTH_URL,
        auth=(UPS_CLIENT_ID, UPS_CLIENT_SECRET),
        data={
            "grant_type": "client_credentials",
            "scope":      "Rate"
        }
    )
    # Log OAuth response
    logging.info(f"🔑 OAuth status: {resp.status_code}  body: {resp.text}")
    resp.raise_for_status()

    token_data = resp.json()
    logging.info(f"🔑 Parsed token_data: {token_data}")

    access_token = token_data["access_token"]
    expires_in   = int(token_data.get("expires_in", 86400))
    _token_cache.update({
        "access_token": access_token,
        "expires_at":    now + expires_in - 60
    })
    return access_token

def get_rate(shipper, recipient, packages):
    """
    :param shipper:   dict with keys Name, AttentionName, Phone, Address{...}
    :param recipient: same shape as shipper
    :param packages:  list of dicts, each with:
                       - PackagingType (UPS code as string, e.g. "02")
                       - Weight (number in lbs)
                       - Dimensions (optional dict with Length, Width, Height in inches)
    :returns:         list of rate dicts:
                       [{
                         serviceCode, method, rate, currency, deliveryDate
                       }, ...]
    """
    token = _get_access_token()
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {token}"
    }

    # Build the Shipment payload
    shipment = {
        "Shipper":   {**shipper, "ShipperNumber": UPS_ACCOUNT},
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
    # Log Rate request payload
    logging.info(f"📦 Rate payload: {json.dumps(payload)}")

    resp = requests.post(RATE_URL, json=payload, headers=headers)
    logging.info(f"🚚 Rate status: {resp.status_code}  body: {resp.text}")
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
