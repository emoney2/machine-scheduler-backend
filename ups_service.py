# ups_service.py
import base64, os, time, uuid, tempfile, json
from typing import List, Dict, Any, Tuple
import requests

UPS_ENV = (os.getenv("UPS_ENV") or "sandbox").lower()
CLIENT_ID = os.getenv("UPS_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("UPS_CLIENT_SECRET", "")
SHIPPER_NUMBER = os.getenv("UPS_ACCOUNT_NUMBER", "")
NEGOTIATED = (os.getenv("UPS_NEGOTIATED_RATES", "true").lower() == "true")

# Ship-from defaults
FROM = {
    "name":   os.getenv("SHIP_FROM_NAME", "JR & Co."),
    "phone":  os.getenv("SHIP_FROM_PHONE", "0000000000"),
    "addr1":  os.getenv("SHIP_FROM_ADDRESS1", ""),
    "addr2":  os.getenv("SHIP_FROM_ADDRESS2", "") or None,
    "city":   os.getenv("SHIP_FROM_CITY", ""),
    "state":  os.getenv("SHIP_FROM_STATE", ""),
    "zip":    os.getenv("SHIP_FROM_ZIP", ""),
    "country":os.getenv("SHIP_FROM_COUNTRY", "US"),
}

UNITS = (os.getenv("UPS_UNITS", "IN_LBS").upper())
DIM_UNIT = "IN" if UNITS == "IN_LBS" else "CM"
WT_UNIT  = "LBS" if UNITS == "IN_LBS" else "KGS"

PICKUP_TYPE = os.getenv("UPS_PICKUP_TYPE", "DailyPickup")
LABEL_FORMAT = os.getenv("UPS_LABEL_FORMAT", "PDF").upper()
LABEL_SIZE = os.getenv("UPS_LABEL_SIZE", "4x6")

HOST = "https://onlinetools.ups.com" if UPS_ENV == "production" else "https://wwwcie.ups.com"

# Known service codes you likely want visible; we'll loop to “shop” rates
UPS_SERVICES = [
    ("01", "Next Day Air"),
    ("14", "Next Day Air Early"),
    ("13", "Next Day Air Saver"),
    ("02", "2nd Day Air"),
    ("12", "3 Day Select"),
    ("03", "Ground"),
]

# ------- OAuth token cache -------
_token_cache: Dict[str, Any] = {"access_token": None, "exp": 0}

def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()

def get_access_token() -> str:
    now = time.time()
    if _token_cache["access_token"] and _token_cache["exp"] - 60 > now:
        return _token_cache["access_token"]

    url = f"{HOST}/security/v1/oauth/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": "Basic " + _b64(f"{CLIENT_ID}:{CLIENT_SECRET}"),
    }
    data = "grant_type=client_credentials"
    r = requests.post(url, headers=headers, data=data, timeout=20)
    r.raise_for_status()
    js = r.json()
    _token_cache["access_token"] = js["access_token"]
    _token_cache["exp"] = now + int(js.get("expires_in", 3300))
    return _token_cache["access_token"]

# ------- Helpers to build UPS address/package JSON -------
def _addr(name: str, phone: str, a1: str, city: str, state: str, postal: str, country: str, a2: str|None=None) -> Dict[str, Any]:
    out = {
        "Name": name[:35] if name else "Recipient",
        "Phone": {"Number": phone or "0000000000"},
        "Address": {
            "AddressLine": [a1] if not a2 else [a1, a2],
            "City": city, "StateProvinceCode": state,
            "PostalCode": postal, "CountryCode": country
        }
    }
    return out

def _shipper() -> Dict[str, Any]:
    return {
        "Name": FROM["name"][:35],
        "ShipperNumber": SHIPPER_NUMBER,
        "Phone": {"Number": FROM["phone"]},
        "Address": {
            "AddressLine": [FROM["addr1"]] if not FROM["addr2"] else [FROM["addr1"], FROM["addr2"]],
            "City": FROM["city"], "StateProvinceCode": FROM["state"],
            "PostalCode": FROM["zip"], "CountryCode": FROM["country"]
        }
    }

def _ship_from() -> Dict[str, Any]:
    return _addr(FROM["name"], FROM["phone"], FROM["addr1"], FROM["city"], FROM["state"], FROM["zip"], FROM["country"], FROM["addr2"])

def _pkg(dim: Dict[str, Any], weight_lbs: float|int) -> Dict[str, Any]:
    L, W, H = str(dim["L"]), str(dim["W"]), str(dim["H"])
    WGT = f"{float(weight_lbs):.2f}"
    return {
        "PackagingType": {"Code": "02"},  # Customer Supplied Package
        "Dimensions": {
            "UnitOfMeasurement": {"Code": DIM_UNIT},
            "Length": L, "Width": W, "Height": H
        },
        "PackageWeight": {
            "UnitOfMeasurement": {"Code": WT_UNIT},
            "Weight": WGT
        }
    }

def _label_spec() -> Dict[str, Any]:
    # PDF recommended for your print flow
    img_code = "PDF" if LABEL_FORMAT == "PDF" else ("PNG" if LABEL_FORMAT == "PNG" else "ZPL")
    return {
        "LabelImageFormat": {"Code": img_code},
        "HTTPUserAgent": "JRCO-App",
        "LabelStockSize": {"Height": "4", "Width": "6"} if LABEL_SIZE == "4x6" else None
    }

# ------- Rating -------
def get_rate(
    ship_to: Dict[str, str],
    packages: List[Dict[str, Any]],
    ask_all_services: bool = True
) -> List[Dict[str, Any]]:
    """
    ship_to: { name, phone, addr1, addr2, city, state, zip, country }
    packages: [{ L,W,H, weight }, ...]
    returns: [{code, method, rate, currency, delivery}, ...]
    """
    token = get_access_token()
    url = f"{HOST}/api/rating/v1/Rate"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "transId": str(uuid.uuid4()),
        "transactionSrc": "JRCO",
    }

    # Build base shipment body
    base_shipment = {
        "Shipper": _shipper(),
        "ShipTo": _addr(
            ship_to["name"], ship_to.get("phone",""),
            ship_to["addr1"], ship_to["city"], ship_to["state"], ship_to["zip"], ship_to.get("country","US"),
            ship_to.get("addr2") or None
        ),
        "ShipFrom": _ship_from(),
        "PaymentDetails": {
            "ShipmentCharge": [{
                "Type": "01",
                "BillShipper": {"AccountNumber": SHIPPER_NUMBER}
            }]
        },
        "Package": [_pkg(p, p.get("weight", 1.0)) for p in packages],
        "DeliveryTimeInformation": {"PackageBillType": "03"}  # DAP (shipper pays)
    }

    results: List[Dict[str, Any]] = []

    if ask_all_services:
        # Loop through common services (works like "shop" rates)
        for code, name in UPS_SERVICES:
            body = {
                "RateRequest": {
                    "Shipment": {
                        **base_shipment,
                        "Service": {"Code": code}
                    },
                    "Request": {"SubVersion": "1707"}
                }
            }
            if NEGOTIATED:
                body["RateRequest"]["Shipment"]["ShipmentRatingOptions"] = {"NegotiatedRatesIndicator": "Y"}

            resp = requests.post(url, headers=headers, json=body, timeout=25)
            if resp.status_code >= 400:
                # Skip services UPS doesn’t allow for this route/package
                continue
            data = resp.json()
            try:
                rated = data["RateResponse"]["RatedShipment"][0]
            except Exception:
                continue

            total = rated.get("TotalCharges", rated.get("NegotiatedRateCharges", {}))
            money = total.get("MonetaryValue") or rated.get("TotalCharges", {}).get("MonetaryValue")
            curr = total.get("CurrencyCode") or rated.get("TotalCharges", {}).get("CurrencyCode")
            eta  = rated.get("GuaranteedDelivery", {}).get("BusinessDaysInTransit") \
                   or rated.get("TimeInTransit", {}).get("DaysInTransit")

            results.append({
                "code": code,
                "method": name,
                "rate": float(money) if money else None,
                "currency": curr or "USD",
                "delivery": f"{eta} business days" if eta else None
            })
    else:
        # Single service expected in ship_to["service_code"]
        code = ship_to.get("service_code", "03")
        name = next((n for c, n in UPS_SERVICES if c == code), code)
        body = {
            "RateRequest": {
                "Shipment": {
                    **base_shipment,
                    "Service": {"Code": code}
                },
                "Request": {"SubVersion": "1707"}
            }
        }
        if NEGOTIATED:
            body["RateRequest"]["Shipment"]["ShipmentRatingOptions"] = {"NegotiatedRatesIndicator": "Y"}

        resp = requests.post(url, headers=headers, json=body, timeout=25)
        resp.raise_for_status()
        data = resp.json()
        rated = data["RateResponse"]["RatedShipment"][0]
        total = rated.get("TotalCharges", rated.get("NegotiatedRateCharges", {}))
        money = total.get("MonetaryValue") or rated.get("TotalCharges", {}).get("MonetaryValue")
        curr = total.get("CurrencyCode") or rated.get("TotalCharges", {}).get("CurrencyCode")
        eta  = rated.get("GuaranteedDelivery", {}).get("BusinessDaysInTransit") \
               or rated.get("TimeInTransit", {}).get("DaysInTransit")
        results.append({
            "code": code,
            "method": name,
            "rate": float(money) if money else None,
            "currency": curr or "USD",
            "delivery": f"{eta} business days" if eta else None
        })

    # Sort by price ascending, put None at end
    results.sort(key=lambda x: (x["rate"] is None, x["rate"] if x["rate"] is not None else 1e9))
    return results

# ------- Create Shipment (labels) -------
def create_shipment(
    ship_to: Dict[str, str],
    packages: List[Dict[str, Any]],
    service_code: str
) -> Tuple[List[str], List[str]]:
    """
    Returns (label_urls, tracking_numbers)
    """
    token = get_access_token()
    url = f"{HOST}/api/shipments/v1/shipments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "transId": str(uuid.uuid4()),
        "transactionSrc": "JRCO",
    }

    shipment = {
        "ShipmentRequest": {
            "Request": {"SubVersion": "1707"},
            "Shipment": {
                "Description": "JRCO shipment",
                "Shipper": _shipper(),
                "ShipFrom": _ship_from(),
                "ShipTo": _addr(
                    ship_to["name"], ship_to.get("phone",""),
                    ship_to["addr1"], ship_to["city"], ship_to["state"], ship_to["zip"], ship_to.get("country","US"),
                    ship_to.get("addr2") or None
                ),
                "Service": {"Code": service_code},
                "PaymentInformation": {
                    "ShipmentCharge": [{
                        "Type": "01",
                        "BillShipper": {"AccountNumber": SHIPPER_NUMBER}
                    }]
                },
                "Package": [_pkg(p, p.get("weight", 1.0)) for p in packages],
                "ShipmentServiceOptions": {}
            },
            "LabelSpecification": _label_spec()
        }
    }

    if NEGOTIATED:
        shipment["ShipmentRequest"]["Shipment"]["ShipmentRatingOptions"] = {"NegotiatedRatesIndicator": "Y"}

    resp = requests.post(url, headers=headers, json=shipment, timeout=35)
    try:
        resp.raise_for_status()
    except Exception as e:
        # Bubble up a readable error
        raise RuntimeError(f"UPS Ship error {resp.status_code}: {resp.text[:500]}") from e

    data = resp.json()
    # Collect labels & tracking
    label_urls: List[str] = []
    tracking: List[str] = []

    try:
        results = data["ShipmentResponse"]["ShipmentResults"]["PackageResults"]
        if isinstance(results, dict):
            results = [results]
        for pkg in results:
            trk = pkg["TrackingNumber"]
            img = pkg["ShippingLabel"]["GraphicImage"]  # base64 string
            ext = "pdf" if LABEL_FORMAT == "PDF" else ("png" if LABEL_FORMAT == "PNG" else "zpl")
            fname = f"ups_{trk}.{ext}"
            fpath = os.path.join(tempfile.gettempdir(), fname)
            # For PDF/PNG UPS returns base64; write to tmp and expose via /labels/<file>
            with open(fpath, "wb") as f:
                f.write(base64.b64decode(img))
            label_urls.append(f"/labels/{fname}")
            tracking.append(trk)
    except Exception as e:
        raise RuntimeError(f"Could not parse UPS label response: {json.dumps(data)[:800]}") from e

    return label_urls, tracking
