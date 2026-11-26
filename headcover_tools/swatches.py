"""Catalog materials PSDs -> tileable swatch library for the Headcover Studio.

Each catalog swatch tag is a white shape with the material photo (and any colour
fills) clipped onto it, and a text label underneath. For every tag we:
  1. composite its whole clipped stack (normal / multiply, with opacity) like Photoshop,
  2. take the largest clean square below the punch hole,
  3. flatten the studio lighting and make the square seamless (wrap-around blend),
  4. measure its colour in linear light,
and group tags under the row header they sit next to ("Gator Leather", "made in italy"...).
"""
import os
import re
import json
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .psdread import parse

LINE_RX = re.compile(r"leather|vinyl solid|lining", re.I)
TILE_PX = 256
TILE_INCHES = 2.1   # assumed width of the cropped square on a real swatch tag


# ---------- 1. extract tags + labels from one PSD ----------
def _rgba(x):
    d = x["data"]
    if 0 not in d:
        return None
    return np.dstack([d[0], d[1], d[2], d.get(-1, np.full_like(d[0], 255))])


def _centre(x):
    t, l, b, r = x["rect"]
    return (l + r) / 2, (t + b) / 2


def extract(psd_path, page):
    """Return [{page, idx, rect, labels, image(RGBA ndarray)}] for every swatch tag."""
    W, H, L = parse(psd_path)
    texts = [x for x in L if "TySh" in x["keys"] and not x["hidden"]]
    tags = []
    for i, x in enumerate(L):
        if x["clip"] or x["hidden"] or x["sect"] or "TySh" in x["keys"]:
            continue
        t, l, b, r = x["rect"]
        if not (300 < r - l < 700 and 500 < b - t < 1100):   # swatch-tag sized base
            continue
        stack, j = [], i + 1
        while j < len(L) and L[j]["clip"]:
            y = L[j]
            if not y["hidden"] and "TySh" not in y["keys"] and 0 in y["data"]:
                stack.append(y)
            j += 1
        if not stack:
            continue
        # label: text centred inside the tag, closest to its visible strip (tags overlap like a fan)
        cands = [tx for tx in texts if l <= _centre(tx)[0] <= r and t <= _centre(tx)[1] <= b]
        cands.sort(key=lambda tx: abs(_centre(tx)[0] - (r - 126)))
        base = _rgba(x).astype(np.float32) / 255
        out, A = base[..., :3].copy(), base[..., 3]
        cov = np.zeros_like(A)       # where a material photo actually covers the tag (page-wide fills don't count)
        cov_all = np.zeros_like(A)   # fallback when a tag's only photo is a large layer
        for y in stack:
            P = _rgba(y).astype(np.float32) / 255
            pt, pl, pb, pr = y["rect"]
            y0, x0 = max(pt, t), max(pl, l)
            y1, x1 = min(pb, b), min(pr, r)
            if y1 <= y0 or x1 <= x0:
                continue
            src = P[y0 - pt:y1 - pt, x0 - pl:x1 - pl]
            a = src[..., 3:4] * (y["op"] / 255)
            dst = out[y0 - t:y1 - t, x0 - l:x1 - l]
            if y["blend"].strip() == "mul":
                dst[:] = dst * (1 - a + a * src[..., :3])
            else:
                dst[:] = dst * (1 - a) + src[..., :3] * a
            ca = cov_all[y0 - t:y1 - t, x0 - l:x1 - l]
            np.maximum(ca, src[..., 3], out=ca)
            if (pb - pt) * (pr - pl) < 3 * (b - t) * (r - l):
                c = cov[y0 - t:y1 - t, x0 - l:x1 - l]
                np.maximum(c, src[..., 3], out=c)
        if cov.max() == 0:
            cov = cov_all
        img = np.dstack([np.clip(out * 255, 0, 255), np.clip(A * cov * 255, 0, 255)]).astype(np.uint8)
        tags.append(dict(page=page, idx=len(tags), rect=[l, t, r - l, b - t],
                         labels=[tx["name"] for tx in cands[:3]], image=img))
    return tags


# ---------- 2-4. seamless tile + colour ----------
def _s2l(c):
    c = c / 255
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _l2s(c):
    c = np.clip(c, 0, 1)
    return 255 * np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def _best_square(a):
    h, w = a.shape
    for s in range(int(min(w * 0.86, h * 0.62)), 40, -8):
        for y in range(int(h * 0.22), h - s, 6):
            x = (w - s) // 2
            if a[y:y + s, x:x + s].min() >= 250:
                return x, y, s
    return None


def _seamless(a):
    n = a.shape[0]
    half = n // 2
    rolled = np.roll(np.roll(a, half, 0), half, 1)
    t = np.minimum(np.arange(n), n - 1 - np.arange(n)) / (n / 2)
    w1 = np.clip((t - 0.05) / 0.35, 0, 1)
    w1 = w1 * w1 * (3 - 2 * w1)
    W = np.minimum.outer(w1, w1)[..., None]
    return a * W + rolled * (1 - W)


def make_tile(img):
    sq = _best_square(img[..., 3])
    if not sq:
        return None, None
    x, y, s = sq
    k = int(s * 0.12)   # stay clear of the tag's edge shading, which shows as a grid on light materials
    x, y, s = x + k, y + k, s - 2 * k
    crop = img[y:y + s, x:x + s, :3].astype(np.float32)
    lin = _s2l(crop)
    blur = np.asarray(Image.fromarray(crop.astype(np.uint8)).filter(ImageFilter.GaussianBlur(s / 4))).astype(np.float32)
    mean = lin.reshape(-1, 3).mean(0)
    flat = lin / np.maximum(_s2l(blur), 1e-3) * mean
    tile = Image.fromarray(np.clip(_seamless(_l2s(flat)), 0, 255).astype(np.uint8)).resize((TILE_PX, TILE_PX), Image.LANCZOS)
    hexc = "#%02x%02x%02x" % tuple(int(round(v)) for v in _l2s(mean))
    return tile, hexc


# ---------- grouping ----------
def _clean(s):
    s = re.sub(r"\s+copy(\s*\d+)?$", "", s.strip(), flags=re.I)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"^(.+?)(?:\s+\1)+$", r"\1", s, flags=re.I)   # "Custom Print Custom Print" -> "Custom Print"
    return " ".join(w[:1].upper() + w[1:].lower() for w in s.split(" "))


def _line(labels):
    nm = _clean([l for l in labels if LINE_RX.search(l)][0]).replace("Distressedleather", "Distressed Leather")
    origin = "Italy" if any("italy" in l.lower() for l in labels) else ("USA" if any("usa" in l.lower() for l in labels) else "")
    if nm.lower() == "classic leather":
        origin = "USA"
    return nm, origin


def build_library(psds, out_dir, fixes=None, sheet=True):
    """psds: [(path, page_id)]. fixes: {"p26@436,1779": "Black"} label corrections keyed by page + tag position."""
    fixes = fixes or {}
    os.makedirs(out_dir, exist_ok=True)
    materials, skipped = [], []
    for path, page in psds:
        tags = extract(path, page)
        heads = [t for t in tags if t["labels"] and (LINE_RX.search(t["labels"][0]) or t["labels"][0].lower().startswith("made in italy"))]
        for t in tags:
            if t in heads:
                continue
            hs = [h for h in heads if abs(h["rect"][1] - t["rect"][1]) < 120 and h["rect"][0] < t["rect"][0]]
            if hs:
                hs = sorted(hs, key=lambda h: -h["rect"][0])[:1]
            else:   # continuation row: nearest header above
                hs = sorted([h for h in heads if h["rect"][1] < t["rect"][1]], key=lambda h: t["rect"][1] - h["rect"][1])[:1]
            if not hs or not t["labels"]:
                skipped.append(f"{page}#{t['idx']}")
                continue
            tile, hexc = make_tile(t["image"])
            if tile is None:
                skipped.append(f"{page}#{t['idx']}")
                continue
            line, origin = _line(hs[0]["labels"])
            key = f"{page}@{t['rect'][0]},{t['rect'][1]}"      # page + tag position: stable when tags are added
            name = fixes.get(key) or _clean(t["labels"][0])
            mid = re.sub(r"[^a-z0-9]+", "-", f"{line}-{name}".lower()).strip("-")
            while any(m["id"] == mid for m in materials):
                mid += "-2"
            tile.save(os.path.join(out_dir, mid + ".jpg"), quality=88)
            materials.append({"id": mid, "name": name, "line": line, "origin": origin,
                              "kind": "fur" if "lining" in line.lower() else "material",
                              "hex": hexc, "tile": mid + ".jpg", "tileInches": TILE_INCHES,
                              "source": key})
    json.dump({"version": 1, "materials": materials}, open(os.path.join(out_dir, "swatches.json"), "w"), indent=1)
    if sheet:   # written to the current folder, not into the deployed data
        contact_sheet(materials, out_dir, sheet if isinstance(sheet, str) else "swatch_contact_sheet.png")
    return materials, skipped


def contact_sheet(materials, tile_dir, out_png, cell=150, cols=9, pad=14, lab=40):
    try:
        F = ImageFont.truetype("DejaVuSans.ttf", 13)
        FB = ImageFont.truetype("DejaVuSans-Bold.ttf", 22)
    except OSError:
        F = FB = ImageFont.load_default()
    groups = {}
    for m in materials:
        groups.setdefault((m["line"], m["origin"]), []).append(m)
    rows = sum((len(v) + cols - 1) // cols for v in groups.values())
    S = Image.new("RGB", (cols * (cell + pad) + pad, 60 + len(groups) * 44 + rows * (cell + lab + pad)), (246, 244, 240))
    d = ImageDraw.Draw(S)
    y = 20
    d.text((pad, y), "JR & Co. material swatch library (tiles repeated 3x3)", font=FB, fill=(0, 0, 0))
    y += 40
    for (line, origin), ms in groups.items():
        d.text((pad, y + 8), f"{line}" + (f"  ({origin})" if origin else "") + f"  ·  {len(ms)}", font=FB, fill=(40, 40, 40))
        y += 44
        for i, m in enumerate(ms):
            x = pad + (i % cols) * (cell + pad)
            yy = y + (i // cols) * (cell + lab + pad)
            t = Image.open(os.path.join(tile_dir, m["tile"])).resize((cell // 3, cell // 3), Image.LANCZOS)
            for a in range(3):
                for b in range(3):
                    S.paste(t, (x + a * (cell // 3), yy + b * (cell // 3)))
            d.text((x, yy + cell + 4), m["name"][:22], font=F, fill=(0, 0, 0))
            d.text((x, yy + cell + 21), m["hex"], font=F, fill=(120, 110, 100))
        y += ((len(ms) + cols - 1) // cols) * (cell + lab + pad)
    S.save(out_png, optimize=True)
