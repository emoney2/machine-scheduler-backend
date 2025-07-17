# ups_service.py

import os
import time
import json
import logging
import requests

# Load UPS OAuth2 creds and account number from env
UPS_CLIENT_ID     = os.getenv("UPS_CLIENT_ID")
UPS_CLIENT_SECRET = os.getenv("UPS_CLIENT_SECRET")
UPS_ACCOUNT       = os.getenv("UPS_ACCOUNT")
if not (UPS_CLIENT_ID and UPS_CLIENT_SECRET and UPS_ACCOUNT):
    raise RuntimeError(
        "Missing UPS OAuth2 creds. Set UPS_CLIENT_ID, UPS_CLIENT_SECRET, and UPS_ACCOUNT."
    )

# Sandbox OAuth2 & JSON Rate endpoints
OAUTH_URL = "https://wwwcie.ups.com/security/v1/oauth/token"
RATE_URL  = "https://wwwcie.ups.com/ship/v1/rating/Rate"

_token_cache = {}

def _get_access_token():
    now = time.time()
    if _token_cache.get("expires_at", 0) > now:
        return _token_cache["access_token"]

    resp = requests.post(
        OAUTH_URL,
        auth=(UPS_CLIENT_ID, UPS_CLIENT_SECRET),
        data={"grant_type":"client_credentials","scope":"Rate"}
    )
    logging.info(f"🔑 OAuth status: {resp.status_code} body: {resp.text}")
    resp.raise_for_status()

    tk = resp.json()
    logging.info(f"🔑 Parsed token_data: {tk}")

    _token_cache["access_token"] = tk["access_token"]
    _token_cache["expires_at"] = now + int(tk.get("expires_in",86400)) - 60
    return _token_cache["access_token"]

def get_rate(shipper, recipient, packages):
    token = _get_access_token()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }

    shipment = {
        "Shipper":   {**shipper,  "ShipperNumber": UPS_ACCOUNT},
        "ShipTo":    recipient,
        "ShipFrom":  shipper,
        "Package":   []
    }
    for pkg in packages:
        dims = pkg.get("Dimensions", {})
        pkg_obj = {
            "PackagingType": {"Code": pkg.get("PackagingType","02")},
            "PackageWeight": {
                "UnitOfMeasurement": {"Code":"LBS"},
                "Weight": str(pkg["Weight"])
            }
        }
        if dims:
            pkg_obj["Dimensions"] = {
                "UnitOfMeasurement": {"Code":"IN"},
                "Length": str(dims["Length"]),
                "Width":  str(dims["Width"]),
                "Height": str(dims["Height"])
            }
        shipment["Package"].append(pkg_obj)

    payload = {
        "RateRequest": {
            "Request":  {"RequestOption": "Rate"},
            "Shipment": shipment
        }
    }
    logging.info(f"📦 Rate payload: {json.dumps(payload)}")

    resp = requests.post(RATE_URL, json=payload, headers=headers)
    logging.info(f"🚚 Rate status: {resp.status_code} body: {resp.text}")
    resp.raise_for_status()

    data = resp.json()
    return [
        {
            "serviceCode":  rs["Service"]["Code"],
            "method":       rs["Service"].get("Description",""),
            "rate":         rs["TotalCharges"]["MonetaryValue"],
            "currency":     rs["TotalCharges"]["CurrencyCode"],
            "deliveryDate": rs.get("GuaranteedDelivery",{}).get("Date","")
        }
        for rs in data.get("RateResponse",{}).get("RatedShipment",[])
    ]
