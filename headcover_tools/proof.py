"""<Product>3Draw.psd -> 3D proof folder for the Headcover Studio.

Each clipped smart-object layer (with its warp) becomes a view: a base shading PNG
plus a UV map (.uv.bin = zlib, row-delta int16 (u,v) pairs in layout pixels,
65535 = no design). Fur, labels and logo layers become fur.png / overlay.png.
"""
import os
import json
import zlib
import numpy as np
from PIL import Image

from . import uvmap
from .psdread import parse

uvmap.MODE = "cp"   # control-point bbox: matched Photoshop within 0-5 levels / 255


def build(src, out_dir, name, content_w=6300, content_h=5252):
    W, H, L = parse(src)
    os.makedirs(out_dir, exist_ok=True)

    def full(x):
        d = x["data"]
        t, l, b, r = x["rect"]
        out = np.zeros((H, W, 4), np.uint8)
        if 0 not in d:
            return out
        rgba = np.dstack([d[0], d[1], d[2], d.get(-1, np.full_like(d[0], 255))])
        y0, x0 = max(t, 0), max(l, 0)
        y1, x1 = min(b, H), min(r, W)
        out[y0:y1, x0:x1] = rgba[y0 - t:y1 - t, x0 - l:x1 - l]
        return out

    def png(a, fname):
        Image.fromarray(a, "RGBA").save(os.path.join(out_dir, fname), optimize=True)
        return fname

    views = []
    for i, x in enumerate(L):
        if b"SoLE" in x["extra"] and x["clip"]:
            base = full(L[i - 1])
            ys, xs = np.where(base[..., 3] > 0)
            y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
            uv = uvmap.map_layer(x["extra"][b"SoLE"], content_w, content_h, W, H, ss=2)[y0:y1, x0:x1]
            u = np.where(np.isnan(uv[..., 0]), 65535, np.clip(uv[..., 0], 0, content_w - 1)).astype(np.uint16)
            v = np.where(np.isnan(uv[..., 1]), 65535, np.clip(uv[..., 1], 0, content_h - 1)).astype(np.uint16)
            inter = np.stack([u, v], -1).astype(np.int32)
            d = inter.copy()
            d[:, 1:, :] = inter[:, 1:, :] - inter[:, :-1, :]       # row delta
            k = len(views)
            open(os.path.join(out_dir, f"view-{k}.uv.bin"), "wb").write(zlib.compress(d.astype(np.int16).tobytes(), 9))
            views.append({"name": x["name"], "bbox": [int(x0), int(y0), int(x1 - x0), int(y1 - y0)],
                          "base": png(base[y0:y1, x0:x1], f"view-{k}.png"), "uv": f"view-{k}.uv.bin"})

    fur = [x for x in L if x["name"].upper() == "FUR" and not x["hidden"]]
    furd = None
    if fur:
        f = full(fur[0])
        ys, xs = np.where(f[..., 3] > 0)
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        furd = {"bbox": [int(x0), int(y0), int(x1 - x0), int(y1 - y0)], "png": png(f[y0:y1, x0:x1], "fur.png")}

    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for idx, x in enumerate(L):
        if (x["hidden"] or x["sect"] in (1, 2, 3) or b"SoLE" in x["extra"]
                or x["name"] == "Background" or x["name"].upper() == "FUR"):
            continue
        if idx + 1 < len(L) and L[idx + 1]["clip"]:
            continue   # clip bases are the shading, not overlay
        ov.alpha_composite(Image.fromarray(full(x), "RGBA"))
    ov.save(os.path.join(out_dir, "overlay.png"), optimize=True)

    proof = {"name": name, "w": W, "h": H, "contentW": content_w, "contentH": content_h,
             "views": views, "fur": furd, "overlay": "overlay.png"}
    json.dump(proof, open(os.path.join(out_dir, "proof.json"), "w"), indent=1)
    return proof
