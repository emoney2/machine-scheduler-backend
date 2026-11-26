# headcover_tools

PSD pipeline for the **Headcover Studio** page (`/headcover-studio`). Runs on a PC, not on Render.
Everything it writes goes into `frontend/public/headcover-studio/`, which the studio loads as plain files.

```
pip install -r backend/requirements-tools.txt        # numpy + pillow only
cd backend
python -m headcover_tools add-design "G:\My Drive\Design\Design Templates\CamoTest.psd" --product Driver
python -m headcover_tools add-proof  "G:\My Drive\Design\Design Templates\3D Models\Driver3Draw.psd" --name Driver
python -m headcover_tools build-swatches "...\Page 23 - Materials 1.psd" "...\Page 24 - Materials 2.psd" "...\Page 26 - Materials 4.psd"
```

| Command | Reads | Writes |
|---|---|---|
| `add-design` | design PSD: `BASE` (frame), `FUR`, `Logo*`, body group, a group of pattern pieces (one child layer per material slot). Optional marker layers `JRCO_PRODUCT:Driver`, `JRCO_FUR_NAME:...` | `templates/<id>/template.json`, `frame.png`, `slot-N.png`, `fur.png` |
| `add-proof` | `<Product>3Draw.psd`: clipped smart objects with custom/quilt warps over grey shading bases, `FUR`, label layers | `proofs/<id>/proof.json`, `view-N.png` (shading), `view-N.uv.bin` (zlib, row-delta int16 u,v in 6300x5252 layout px; 65535 = no design), `fur.png`, `overlay.png` |
| `build-swatches` | catalog materials pages (swatch tags + labels + row headers) | `swatches/swatches.json` + one seamless 256 px tile per material, and `swatch_contact_sheet.png` in the current folder for review |

Templates and proofs are matched by product name (`template.product == proof.name`). Both commands register themselves in `catalog.json`.

Swatch label corrections live in `swatch_fixes.json`, keyed by page and tag position (the `source` field in `swatches.json`).

Colorway rules (enforced in the browser, `frontend/src/headcoverStudio/engine.js`): back panel = lightest colour in that colorway's camo; camo uses 3 of the 4 picks: A = lightest + picks 1 & 2, B = lightest + picks 2 & 3. Customers see the 3D proof; the flat layout is in the lightbox only.

## Exact 3D side views (Photoshop bake, one time per 3Draw)

Quilted and custom-warped side views can't be decoded from the PSD reliably, so their UV maps are baked by Photoshop itself.
This is an offline step on the PC; the live app only ever loads the resulting `.uv.bin` files.

```
python -m headcover_tools add-proof    "...\3D Models\Driver3Draw.psd" --name Driver     # as before
python -m headcover_tools bake-prepare "...\3D Models\Driver3Draw.psd"
#   Photoshop: File > Scripts > Browse... > backend/headcover_tools/photoshop/bake_uv.jsx
python -m headcover_tools bake-apply
```

`bake_uv.jsx` opens each 3Draw, duplicates it and closes the original untouched, embeds the linked master layout,
swaps its pixels for a 16-bit coordinate image and renders each view alone (smart filters and layer styles hidden).
It never saves the 3Draw or the master layout. Working files go to `headcover_tools/photoshop/work/` (git-ignored).
