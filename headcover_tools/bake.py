"""Exact 3D-proof UV maps, baked once by Photoshop itself (offline; the live app never uses Photoshop).

How it works: each 3Draw smart object is re-filled with a 16-bit "coordinate image" (red = x, green = y
across the master layout), and Photoshop renders every view through its own warp. Reading the colour of
each rendered pixel gives the exact layout position it shows. bake_uv.jsx never saves the 3Draw or the
master layout: it works on a duplicate, embeds the linked layout first, and aborts if a layer is still linked.

    python -m headcover_tools bake-prepare "...\\3D Models\\Driver3Draw.psd" ...   # coord images + job list
    (Photoshop: File > Scripts > Browse... > headcover_tools/photoshop/bake_uv.jsx)
    python -m headcover_tools bake-apply                                         # renders -> view-N.uv.bin
"""
import glob
import json
import os
import struct
import zlib
import numpy as np

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "photoshop", "work")


def write_coord_tiff(path, w, h):
    """Uncompressed 16-bit RGB TIFF: R = x * 65535/(w-1), G = y * 65535/(h-1), B = 0."""
    u = np.round(np.arange(w) * 65535 / (w - 1)).astype("<u2")
    v = np.round(np.arange(h) * 65535 / (h - 1)).astype("<u2")
    row = np.zeros((w, 3), "<u2"); row[:, 0] = u
    n = w * 6
    tags = [(256, 4, w), (257, 4, h), (258, 3, None), (259, 3, 1), (262, 3, 2), (273, 4, None),
            (277, 3, 3), (278, 4, h), (279, 4, n * h), (284, 3, 1)]
    head = 8; ifd = head; nt = len(tags); bps_off = ifd + 2 + nt * 12 + 4; data_off = bps_off + 8
    with open(path, "wb") as f:
        f.write(b"II*\0" + struct.pack("<I", ifd) + struct.pack("<H", nt))
        for tag, typ, val in tags:
            if tag == 258: f.write(struct.pack("<HHII", tag, 3, 3, bps_off))
            elif tag == 273: f.write(struct.pack("<HHII", tag, 4, 1, data_off))
            elif typ == 3: f.write(struct.pack("<HHIHH", tag, 3, 1, val, 0))
            else: f.write(struct.pack("<HHII", tag, 4, 1, val))
        f.write(struct.pack("<I", 0) + struct.pack("<HHHH", 16, 16, 16, 0))
        for y in range(h):
            row[:, 1] = v[y]; f.write(row.tobytes())


def _lzw(data, need):
    """TIFF LZW decode (MSB first), stopping once `need` bytes are out."""
    out = bytearray(); table = [bytes([i]) for i in range(256)] + [b"", b""]
    bit, nbits, prev, n = 0, 9, None, len(data) * 8
    while bit + nbits <= n and len(out) < need:
        chunk = int.from_bytes(data[bit >> 3:(bit >> 3) + 4].ljust(4, b"\0"), "big")
        code = (chunk >> (32 - (bit & 7) - nbits)) & ((1 << nbits) - 1); bit += nbits
        if code == 256: table = table[:258]; nbits = 9; prev = None; continue
        if code == 257: break
        if prev is None: entry = table[code]
        elif code < len(table): entry = table[code]; table.append(prev + entry[:1])
        else: entry = prev + prev[:1]; table.append(entry)
        out += entry; prev = entry
        if len(table) + 1 >= (1 << nbits) and nbits < 12: nbits += 1
    return bytes(out[:need])


def read_render(path, W, H):
    """Photoshop's 16-bit RGBA TIFF (none/LZW, interleaved) -> float array HxWx4 with colour un-premultiplied."""
    b = open(path, "rb").read(); le = b[:2] == b"II"; E = "<" if le else ">"
    off = struct.unpack(E + "I", b[4:8])[0]; n = struct.unpack(E + "H", b[off:off + 2])[0]; t = {}
    for i in range(n):
        tag, typ, cnt = struct.unpack(E + "HHI", b[off + 2 + i * 12:off + 10 + i * 12]); raw = b[off + 10 + i * 12:off + 14 + i * 12]
        size = {3: 2, 4: 4}.get(typ, 1)
        vals = raw if cnt * size <= 4 else b[struct.unpack(E + "I", raw)[0]:struct.unpack(E + "I", raw)[0] + cnt * size]
        t[tag] = list(struct.unpack(E + ("H" if typ == 3 else "I") * cnt, vals[:cnt * size])) if typ in (3, 4) else vals
    w, h, comp, rps, spp = t[256][0], t[257][0], t[259][0], t.get(278, [t[257][0]])[0], t[277][0]
    assert spp == 4 and t[258][0] == 16 and t.get(284, [1])[0] == 1 and comp in (1, 5), "unexpected TIFF layout"
    assoc = t.get(338, [0])[0] == 1
    rows = []; need_w = min(W, w)
    for s, (o, c) in enumerate(zip(t[273], t[279])):
        if s * rps >= H: break
        k = min(rps, H - s * rps); seg = b[o:o + c]
        need = (w * 8 * (k - 1) + need_w * 8)
        raw = _lzw(seg, need) if comp == 5 else seg[:need]
        raw = raw.ljust(w * 8 * k, b"\0")
        rows.append(np.frombuffer(raw, E + "u2").reshape(k, w, 4)[:, :need_w])
    im = np.concatenate(rows)[:H].astype(np.float64)
    if assoc:
        a = np.maximum(im[..., 3:4], 1) / 65535.0
        im[..., :3] = np.where(im[..., 3:4] > 0, im[..., :3] / a, 0)
    return im


def apply_render(proof_dir, view_name, render_path):
    P = json.load(open(os.path.join(proof_dir, "proof.json")))
    v = next(x for x in P["views"] if x["name"] == view_name)
    cw, ch = P["contentW"], P["contentH"]
    im = read_render(render_path, P["w"], P["h"])
    x0, y0, w, h = v["bbox"]; im = im[y0:y0 + h, x0:x0 + w]
    ok = im[..., 3] > 32768
    if ok.any() and np.median(im[..., 2][ok]) > 1500:
        raise SystemExit(f"{render_path}: blue channel isn't 0 - a smart filter recoloured the code; hide it and re-bake")
    U = np.where(ok, np.clip(np.round(im[..., 0] / 65535 * (cw - 1)), 0, cw - 1), 65535).astype(np.int32)
    V = np.where(ok, np.clip(np.round(im[..., 1] / 65535 * (ch - 1)), 0, ch - 1), 65535).astype(np.int32)
    inter = np.stack([U, V], -1); d = inter.copy(); d[:, 1:] = inter[:, 1:] - inter[:, :-1]
    open(os.path.join(proof_dir, v["uv"]), "wb").write(zlib.compress(d.astype(np.int16).tobytes(), 9))
    v.pop("draft", None); v["exact"] = True
    json.dump(P, open(os.path.join(proof_dir, "proof.json"), "w"), indent=1)
    return float(ok.mean())


def prepare(draws, slug_of, out):
    """Coordinate images + jobs.txt for bake_uv.jsx. draws: 3Draw paths; slug_of(name)->proof id."""
    from .proof import _content_size
    from .psdread import parse
    os.makedirs(os.path.join(WORK, "out"), exist_ok=True)
    jobs = []
    for path in draws:
        _, _, L = parse(path, extract=False)
        cw, ch = _content_size(L)
        coord = os.path.join(WORK, f"coord_{cw}x{ch}.tif")
        if not os.path.exists(coord):
            write_coord_tiff(coord, cw, ch)
        prod = os.path.basename(path).replace("3Draw.psd", "")
        pdir = os.path.join(out, "proofs", slug_of(prod))
        jobs.append(f"{os.path.abspath(path)}|{prod}|{cw}|{ch}|{pdir}")
    open(os.path.join(WORK, "jobs.txt"), "w").write("\n".join(jobs) + "\n")
    return jobs


def apply_all():
    jobs = [l.split("|") for l in open(os.path.join(WORK, "jobs.txt")).read().splitlines() if l.strip()]
    for path, prod, cw, ch, pdir in jobs:
        for f in sorted(glob.glob(os.path.join(WORK, "out", f"{prod}__*__*.tif"))):
            name = os.path.basename(f)[:-4].split("__", 2)[2]
            cov = apply_render(pdir, name, f)
            print(f"{prod}: {name} -> exact ({cov:.0%} of the view shows the design)")
