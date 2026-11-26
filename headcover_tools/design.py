"""Design PSD -> template folder for the Headcover Studio.

Looks for BASE (frame), FUR, Logo*, the body group, and a group of pattern pieces
(each child layer = one material slot). Writes template.json + PNG masks.
"""
import os
import json
import numpy as np
from PIL import Image

from .psdread import parse


def _tree(layers):
    stack = [[]]
    for x in layers:
        if x["sect"] == 3:
            stack.append([])
            continue
        if x["sect"] in (1, 2):
            kids = stack.pop()
            x["kids"] = kids
            stack[-1].append(x)
            continue
        stack[-1].append(x)
    return stack[0]


def _default_logo(piece, base, W, H, scale):
    """Place the logo like the Driver's: centred on the front panel, 27.4% down, 13.85% of the panel wide.
    The front panel is the first transparent gap in BASE (opaque around the panels) on that row."""
    t, l, b, r = piece["rect"]
    t, b = max(t, 0), min(b, H)
    cy = int(t + (b - t) * 0.274)
    px0, px1 = max(l, 0), min(r, W)
    a = base["data"].get(-1) if base is not None else None
    if a is not None:
        bt, bl, bb, br = base["rect"]
        if bt <= cy < bb:
            row = np.zeros(W, bool)
            seg = a[cy - bt] > 0
            x0, x1 = max(bl, 0), min(br, W)
            row[x0:x1] = seg[x0 - bl:x1 - bl]
            xs = np.where(~row)[0]
            if xs.size:
                splits = np.where(np.diff(xs) > 1)[0]
                gaps = np.split(xs, splits + 1)
                gaps = [g for g in gaps if g.size > 0.2 * (r - l)]
                if gaps:
                    px0, px1 = int(gaps[0][0]), int(gaps[0][-1]) + 1
    lw = (px1 - px0) * 0.1385
    cx = (px0 + px1) / 2
    return {"x": (cx - lw / 2) * scale, "y": (cy - lw / 2) * scale, "w": lw * scale, "h": lw * scale}


def convert(src, out_dir, product, scale=0.2, dpi=300):
    W, H, L = parse(src)
    w, h = int(W * scale), int(H * scale)
    top = _tree(L)
    os.makedirs(out_dir, exist_ok=True)

    def full_rgba(x):
        d = x["data"]
        t, l, b, r = x["rect"]
        if 0 not in d:
            return None
        a = d.get(-1, np.full_like(d[0], 255))
        im = Image.fromarray(np.dstack([d[0], d[1], d[2], a]), "RGBA")
        c = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        c.paste(im, (l, t))
        return c

    def save_mask(x, fname):
        im = full_rgba(x).resize((w, h), Image.LANCZOS)
        out = np.zeros((h, w, 4), np.uint8)
        out[..., 3] = np.asarray(im)[..., 3]
        Image.fromarray(out, "RGBA").save(os.path.join(out_dir, fname), optimize=True)
        return fname

    markers = {}

    def walk(items):
        for x in items:
            if x["name"].startswith("JRCO_") and ":" in x["name"]:
                k, v = x["name"].split(":", 1)
                markers[k] = v
            walk(x.get("kids", []))

    walk(top)
    tpl = {
        "name": "Camo",
        "product": markers.get("JRCO_PRODUCT", product),
        "w": w, "h": h, "pxPerInch": dpi * scale,
        "slots": [], "notes": [], "markers": markers,
    }
    first_piece = base_layer = None
    for x in top:
        nm = x["name"]
        # the Camo group is the design even when it's hidden in the product template
        if x["hidden"] and not (nm.lower() == "camo" and x.get("kids") is not None):
            tpl["notes"].append("ignored hidden: " + nm)
            continue
        if x.get("kids") is not None:
            pix = [k for k in x["kids"] if not k["hidden"] and k["rect"][2] > k["rect"][0] and 0 in k["data"]]
            if nm.lower() == "camo" or len(pix) > 1:
                for k in pix:   # each child layer = one material slot
                    i = len(tpl["slots"])
                    tpl["slots"].append({"name": k["name"], "mask": save_mask(k, f"slot-{i}.png")})
                    if first_piece is None:
                        first_piece = k
            else:
                tpl["body"] = {"name": nm, "material": markers.get("JRCO_MATERIAL")}
            continue
        if nm.upper() == "BASE":
            base_layer = x
            full_rgba(x).resize((w, h), Image.LANCZOS).save(os.path.join(out_dir, "frame.png"), optimize=True)
            tpl["frame"] = "frame.png"
        elif nm.strip().lower() in ("fur", "fur color", "fur colour"):
            tpl["fur"] = {"name": markers.get("JRCO_FUR_NAME", "Fur"), "mask": save_mask(x, "fur.png")}
        elif nm.lower().startswith("logo"):
            t, l, b, r = x["rect"]
            tpl["logo"] = {"x": l * scale, "y": t * scale, "w": (r - l) * scale, "h": (b - t) * scale}
        elif nm.upper() == "PRINT BASE":
            pass
        else:
            tpl["notes"].append("unmapped: " + nm)
    tpl["name"] = "Camo" if tpl["slots"] else "Solid"
    if "logo" not in tpl:
        # no camo piece (solid design): use the whole canvas to find the front panel
        piece = first_piece or {"rect": (0, 0, H, W)}
        tpl["logo"] = _default_logo(piece, base_layer, W, H, scale)
        tpl["notes"].append("logo: default placement (add a 'Logo' layer to the PSD to control it)")
    if not tpl["product"]:
        raise SystemExit("No product given: pass --product or add a JRCO_PRODUCT:<Name> layer to the PSD")
    json.dump(tpl, open(os.path.join(out_dir, "template.json"), "w"), indent=1)
    return tpl
