import os, time, json, requests

UPS_CLIENT_ID     = os.getenv("UPS_CLIENT_ID")
UPS_CLIENT_SECRET = os.getenv("UPS_CLIENT_SECRET")
UPS_ACCOUNT       = os.getenv("UPS_ACCOUNT")
if not (UPS_CLIENT_ID and UPS_CLIENT_SECRET and UPS_ACCOUNT):
    raise RuntimeError("UPS credentials missing")

# ← SANDBOX endpoints
OAUTH_URL = "https://wwwcie.ups.com/security/v1/oauth/token"
RATE_URL  = "https://wwwcie.ups.com/ship/v1/rating/Rate"

_token_cache = {}

def _get_access_token():
    now = time.time()
    if _token_cache.get("expires_at",0) > now:
        return _token_cache["access_token"]

    resp = requests.post(
        OAUTH_URL,
        auth=(UPS_CLIENT_ID, UPS_CLIENT_SECRET),
        data={"grant_type":"client_credentials"}
    )
    print("🔑 OAuth status:", resp.status_code, resp.text)
    resp.raise_for_status()
    tk = resp.json()
    access_token = tk["access_token"]
    expires_in   = int(tk.get("expires_in",86400))
    _token_cache.update({
        "access_token": access_token,
        "expires_at":    now + expires_in - 60
    })
    return access_token

def get_rate(shipper, recipient, packages):
    token = _get_access_token()
    headers = {
        "Content-Type":"application/json",
        "Authorization":f"Bearer {token}"
    }
    shipment = {
        "Shipper":  {**shipper, "ShipperNumber":UPS_ACCOUNT},
        "ShipTo":   recipient,
        "ShipFrom": shipper,
        "Package":  []
    }
    for pkg in packages:
        dims = pkg.get("Dimensions", {})
        p = {
            "PackagingType":{"Code":pkg.get("PackagingType","02")},
            "PackageWeight":{
                "UnitOfMeasurement":{"Code":"LBS"},
                "Weight":str(pkg["Weight"])
            }
        }
        if dims:
            p["Dimensions"] = {
                "UnitOfMeasurement":{"Code":"IN"},
                "Length":str(dims["Length"]),
                "Width":str(dims["Width"]),
                "Height":str(dims["Height"])
            }
        shipment["Package"].append(p)

    payload = {"RateRequest":{"Request":{"RequestOption":"Rate"},"Shipment":shipment}}
    print("📦 Rate payload:", json.dumps(payload))
    resp = requests.post(RATE_URL, json=payload, headers=headers)
    print("🚚 Rate status:", resp.status_code, resp.text)
    resp.raise_for_status()

    data = resp.json()
    return [
        {
            "serviceCode": rs["Service"]["Code"],
            "method":      rs["Service"].get("Description",""),
            "rate":        rs["TotalCharges"]["MonetaryValue"],
            "currency":    rs["TotalCharges"]["CurrencyCode"],
            "deliveryDate": rs.get("GuaranteedDelivery",{}).get("Date","")
        }
        for rs in data.get("RateResponse",{}).get("RatedShipment",[])
    ]
