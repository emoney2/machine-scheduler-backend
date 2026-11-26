"""Headcover Studio PSD tools (run on a PC, not on Render).

    python -m headcover_tools add-design  "CamoTest.psd"   --product Driver
    python -m headcover_tools add-proof   "Driver3Draw.psd" --name Driver
    python -m headcover_tools build-swatches "Page 23 - Materials 1.psd" "Page 24 - Materials 2.psd" ...

Output goes straight into frontend/public/headcover-studio/ and catalog.json is updated,
so the new design / proof / swatches show up in the studio after the next frontend build.
Needs only numpy + pillow (see requirements-tools.txt).
"""
import argparse
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.normpath(os.path.join(HERE, "..", "..", "frontend", "public", "headcover-studio"))


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _catalog(out):
    p = os.path.join(out, "catalog.json")
    cat = json.load(open(p)) if os.path.exists(p) else {"version": 1, "swatches": "swatches/swatches.json", "templates": [], "proofs": []}
    return p, cat


def _register(out, key, entry):
    p, cat = _catalog(out)
    cat[key] = [e for e in cat[key] if e["id"] != entry["id"]] + [entry]
    json.dump(cat, open(p, "w"), indent=2)


def main():
    ap = argparse.ArgumentParser(prog="headcover_tools")
    ap.add_argument("--out", default=DEFAULT_OUT, help="studio data folder (default: frontend/public/headcover-studio)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("add-design", help="design PSD -> template")
    d.add_argument("psd")
    d.add_argument("--product", help="product the template wraps onto, e.g. Driver (or a JRCO_PRODUCT: layer)")
    d.add_argument("--id", help="template id (default: <psd name>-<product>)")
    d.add_argument("--scale", type=float, default=0.2)

    p = sub.add_parser("add-proof", help="<Product>3Draw.psd -> 3D proof")
    p.add_argument("psd")
    p.add_argument("--name", required=True, help="product name, must match templates' product, e.g. Driver")

    s = sub.add_parser("build-swatches", help="catalog materials PSDs -> swatch library")
    s.add_argument("psds", nargs="+")
    s.add_argument("--fix", action="append", default=[], help='label fix, e.g. "p26@436,1779=Black" (see "source" in swatches.json)')

    a = ap.parse_args()
    out = a.out
    if a.cmd == "add-design":
        from .design import convert
        base = os.path.splitext(os.path.basename(a.psd))[0]
        tid = a.id or _slug(f"{base}-{a.product or ''}")
        tpl = convert(a.psd, os.path.join(out, "templates", tid), a.product, a.scale)
        _register(out, "templates", {"id": tid, "dir": f"templates/{tid}/"})
        print(f"template {tid}: {len(tpl['slots'])} slots, product {tpl['product']}", *tpl["notes"], sep="\n  ")
    elif a.cmd == "add-proof":
        from .proof import build
        pid = _slug(a.name)
        pr = build(a.psd, os.path.join(out, "proofs", pid), a.name)
        _register(out, "proofs", {"id": pid, "dir": f"proofs/{pid}/"})
        print(f"proof {pid}: {len(pr['views'])} views, fur={'yes' if pr['fur'] else 'no'}")
    elif a.cmd == "build-swatches":
        from .swatches import build_library
        psds = []
        for path in a.psds:
            m = re.search(r"Page\s*(\d+(?:\.\d+)?)", os.path.basename(path))
            psds.append((path, "p" + (m.group(1) if m else _slug(os.path.basename(path)))))
        fx_path = os.path.join(HERE, "swatch_fixes.json")   # label corrections confirmed by the owner
        fixes = json.load(open(fx_path)) if os.path.exists(fx_path) else {}
        fixes.update(dict(f.split("=", 1) for f in a.fix))
        mats, skipped = build_library(psds, os.path.join(out, "swatches"), fixes)
        print(f"{len(mats)} materials; review swatch_contact_sheet.png in this folder")
        if skipped:
            print("skipped (headers / no clean square):", ", ".join(skipped))


if __name__ == "__main__":
    main()
