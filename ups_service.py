# ups_service.py

import os, time, json, requests

# 1) Pull these from your environment (Render config/.env)
UPS_CLIENT_ID     = os.getenv("UPS_CLIENT_ID")
UPS_CLIENT_SECRET = os.getenv("UPS_CLIENT_SECRET")
UPS_ACCOUNT       = os.getenv("UPS_ACCOUNT")
if not (UPS_CLIENT_ID and UPS_CLIENT_SECRET and UPS_ACCOUNT):
    raise RuntimeError("UPS credentials missing")

# 2) Sandbox OAuth2 & Rate endpoints
OAUTH_URL = "https://wwwcie.ups.com/security/v1/oauth/token"
RATE_URL  = "https://wwwcie.ups.com/ship/v1/rating/Rate"

_token_cache = {}

def _get_access_token():
    now = time.time()
    # reuse cached token if still valid
    if _token_cache.get("expires_at", 0) > now:
        return _token_cache["access_token"]

    # 3) Request a token *with* the Rating scope
    resp = requests.post(
        OAUTH_URL,
        auth=(UPS_CLIENT_ID, UPS_CLIENT_SECRET),
        data={
            "grant_type": "client_credentials",
            "scope":       "Rate"
        }
    )
    # debug logging
    print("🔑 OAuth status:", resp.status_code)
    print("🔑 OAuth response:", resp.text)
    resp.raise_for_status()

    token_data = resp.json()
    # debug logging
    print("🔑 Parsed token_data:", token_data)

    access_token = token_data["access_token"]
    expires_in   = int(token_data.get("expires_in", 86400))
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

    # build your Shipment payload exactly as before
    shipment = {
        "Shipper":  {**shipper, "ShipperNumber": UPS_ACCOUNT},
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
                "Height»:            str(dims["Height"])
            }
        shipment["Package"].append(pkg_obj)

    payload = {
        "RateRequest": {
            "Request":  {"RequestOption": "Rate"},
            "Shipment": shipment
        }
    }
    # debug logging
    print("📦 Rate payload:", json.dumps(payload))

    resp = requests.post(RATE_URL, json=payload, headers=headers)
    print("🚚 Rate status:", resp.status_code, "body:", resp.text)
    resp.raise_for_status()

    data = resp.json()
    return [
        {
            "serviceCode":  rs["Service"]["Code"],
            "method":       rs["Service"].get("Description", ""),
            "rate":         rs["TotalCharges"]["MonetaryValue"],
            "currency":     rs["TotalCharges"]["CurrencyCode"],
            "deliveryDate": rs.get("GuaranteedDelivery", {}).get("Date", "")
        }
        for rs in data.get("RateResponse", {}).get("RatedShipment", [])
    ]
