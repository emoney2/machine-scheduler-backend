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
        "name": os.path.splitext(os.path.basename(src))[0],
        "product": markers.get("JRCO_PRODUCT", product),
        "w": w, "h": h, "pxPerInch": dpi * scale,
        "slots": [], "notes": [], "markers": markers,
    }
    for x in top:
        nm = x["name"]
        if x["hidden"]:
            tpl["notes"].append("ignored hidden: " + nm)
            continue
        if x.get("kids") is not None:
            pix = [k for k in x["kids"] if not k["hidden"] and k["rect"][2] > k["rect"][0] and 0 in k["data"]]
            if nm.lower() == "camo" or len(pix) > 1:
                for k in pix:
                    i = len(tpl["slots"])
                    tpl["slots"].append({"name": k["name"], "mask": save_mask(k, f"slot-{i}.png")})
            else:
                tpl["body"] = {"name": nm, "material": markers.get("JRCO_MATERIAL")}
            continue
        if nm.upper() == "BASE":
            full_rgba(x).resize((w, h), Image.LANCZOS).save(os.path.join(out_dir, "frame.png"), optimize=True)
            tpl["frame"] = "frame.png"
        elif nm.upper() == "FUR":
            tpl["fur"] = {"name": markers.get("JRCO_FUR_NAME", "Fur"), "mask": save_mask(x, "fur.png")}
        elif nm.lower().startswith("logo"):
            t, l, b, r = x["rect"]
            tpl["logo"] = {"x": l * scale, "y": t * scale, "w": (r - l) * scale, "h": (b - t) * scale}
        elif nm.upper() == "PRINT BASE":
            pass
        else:
            tpl["notes"].append("unmapped: " + nm)
    if not tpl["product"]:
        raise SystemExit("No product given: pass --product or add a JRCO_PRODUCT:<Name> layer to the PSD")
    json.dump(tpl, open(os.path.join(out_dir, "template.json"), "w"), indent=1)
    return tpl
