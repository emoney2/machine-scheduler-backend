"""<Product>3Draw.psd -> 3D proof folder for the Headcover Studio.

Each clipped smart-object layer (with its warp) becomes a view: a base shading PNG
plus a UV map (.uv.bin = zlib, row-delta int16 (u,v) pairs in layout pixels,
65535 = no design). Fur, labels and logo layers become fur.png / overlay.png.
"""
import os
import json
import struct
import zlib
import numpy as np
from PIL import Image

from . import uvmap
from .psdread import parse

uvmap.MODE = "cp"   # control-point bbox: matched Photoshop within 0-5 levels / 255


def _so_size(so):
    """Width/height of the smart object's own document (the 'Sz  ' descriptor)."""
    j = so.find(b"Sz  ")
    if j < 0:
        return None
    a, b = so.find(b"Wdthdoub", j), so.find(b"Hghtdoub", j)
    if a < 0 or b < 0:
        return None
    return int(round(struct.unpack(">d", so[a + 8:a + 16])[0])), int(round(struct.unpack(">d", so[b + 8:b + 16])[0]))


def _content_size(layers):
    """Size of the product's master layout as placed in the 3Draw (each product has its own)."""
    for x in layers:
        so = x["extra"].get(b"SoLE")
        if so and x["clip"]:
            size = _so_size(so)
            if size and size[0] > 0 and size[1] > 0:
                return size
    raise SystemExit("Couldn't read the master layout size from the 3Draw's smart objects")


def build(src, out_dir, name, content_w=None, content_h=None):
    W, H, L = parse(src)
    os.makedirs(out_dir, exist_ok=True)
    if not content_w or not content_h:
        content_w, content_h = _content_size(L)

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

    def clip_base(i):
        """Shading under the design: the clip group's base plus any clipped shading layers between it and the
        smart object (e.g. a 41% multiply shadow), blended the way Photoshop does inside a clipping group."""
        j = i - 1
        while j > 0 and L[j]["clip"]:
            j -= 1
        base = full(L[j]).astype(np.float64)
        for k in range(j + 1, i):
            x = L[k]
            if x["hidden"] or b"SoLE" in x["extra"]:
                continue
            lay = full(x).astype(np.float64)
            eff = (lay[..., 3:4] / 255) * (x["op"] / 255) * (base[..., 3:4] > 0)
            c, b = lay[..., :3], base[..., :3]
            mode = x["blend"].strip()
            if mode == "mul":
                r = b * c / 255
            elif mode == "fdiv":
                r = np.minimum(255, b * 255 / np.maximum(c, 1))
            elif mode == "scrn":
                r = 255 - (255 - b) * (255 - c) / 255
            else:
                r = c
            base[..., :3] = b * (1 - eff) + r * eff
        return np.clip(np.round(base), 0, 255).astype(np.uint8)

    views = []
    for i, x in enumerate(L):
        if b"SoLE" in x["extra"] and x["clip"]:
            base = clip_base(i)
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
        # where the fur sits in the stack: the number of views Photoshop draws beneath it
        fur_order = sum(1 for x in L[:L.index(fur[0])] if b"SoLE" in x["extra"] and x["clip"])
        f = full(fur[0])
        ys, xs = np.where(f[..., 3] > 0)
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        furd = {"bbox": [int(x0), int(y0), int(x1 - x0), int(y1 - y0)], "png": png(f[y0:y1, x0:x1], "fur.png"),
                "order": fur_order}

    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for idx, x in enumerate(L):
        if (x["hidden"] or x["sect"] in (1, 2, 3) or b"SoLE" in x["extra"]
                or x["name"] == "Background" or x["name"].upper() == "FUR"):
            continue
        if x["clip"] or (idx + 1 < len(L) and L[idx + 1]["clip"]):
            continue   # clip bases and clipped shading layers are folded into each view's shading, not the overlay
        ov.alpha_composite(Image.fromarray(full(x), "RGBA"))
    ov.save(os.path.join(out_dir, "overlay.png"), optimize=True)

    proof = {"name": name, "w": W, "h": H, "contentW": content_w, "contentH": content_h,
             "views": views, "fur": furd, "overlay": "overlay.png"}
    json.dump(proof, open(os.path.join(out_dir, "proof.json"), "w"), indent=1)
    return proof
