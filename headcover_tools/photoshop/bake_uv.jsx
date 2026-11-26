#target photoshop
// Headcover Studio: one-time UV bake (see headcover_tools/bake.py). Run AFTER `python -m headcover_tools bake-prepare`.
// For each 3Draw in work/jobs.txt: open, duplicate, close the original untouched; embed the linked master layout;
// replace its pixels with the coordinate image; render each view alone to work/out/<Product>__<i>__<layer>.tif.
// Nothing is ever saved over the 3Draw or the master layout.
app.displayDialogs = DialogModes.NO;
var ROOT = (new File($.fileName)).parent.fsName.replace(/\\/g, "/") + "/work/";
var LOG = new File(ROOT + "bake_log.txt");
function log(m){ LOG.open("a"); LOG.writeln(new Date().toTimeString().substr(0,8) + " " + m); LOG.close(); }
function s(x){ return stringIDToTypeID(x); }
function isLinked(){ var r = new ActionReference(); r.putEnumerated(s("layer"), s("ordinal"), s("targetEnum")); var d = executeActionGet(r);
  if (!d.hasKey(s("smartObject"))) return false; var so = d.getObjectValue(s("smartObject")); return so.hasKey(s("linked")) && so.getBoolean(s("linked")); }
function collect(c, out){ for (var i = 0; i < c.layers.length; i++){ var L = c.layers[i];
  if (L.typename == "LayerSet") collect(L, out); else if (L.kind == LayerKind.SMARTOBJECT && L.grouped) out.push(L); } }
function safeName(n){ return n.replace(/[^A-Za-z0-9 _-]/g, "_"); }
function closeTemp(){ for (var q = app.documents.length - 1; q >= 0; q--){ var dn = app.documents[q].name;
  if (dn.indexOf("_UVBAKE") >= 0 || dn == "uvview" || dn.indexOf("coord_") == 0) app.documents[q].close(SaveOptions.DONOTSAVECHANGES); } }

function bake(path, product, cw, ch){
  var orig = app.open(new File(path));
  var dup = orig.duplicate(product + "_UVBAKE", false);
  orig.close(SaveOptions.DONOTSAVECHANGES);
  app.activeDocument = dup; dup.bitsPerChannel = BitsPerChannelType.SIXTEEN;
  var sos = []; collect(dup, sos); log(product + ": " + sos.length + " views");
  for (var i = 0; i < sos.length; i++){
    app.activeDocument = dup; dup.activeLayer = sos[i];
    if (isLinked()) executeAction(s("placedLayerConvertToEmbedded"), undefined, DialogModes.NO);
    if (isLinked()) throw new Error("still linked after embedding " + sos[i].name + " - stopped so the master layout is never edited");
    executeAction(s("placedLayerEditContents"), new ActionDescriptor(), DialogModes.NO);
    var cd = app.activeDocument; if (cd == dup) throw new Error("couldn't open the contents of " + sos[i].name);
    var done = false; for (var k = 0; k < cd.layers.length; k++) if (cd.layers[k].name == "__UVCODE") done = true;
    if (done){ cd.close(SaveOptions.DONOTSAVECHANGES); continue; }
    if (Math.round(cd.width.as("px")) != cw || Math.round(cd.height.as("px")) != ch) throw new Error("layout size " + cd.width + "x" + cd.height + " != " + cw + "x" + ch);
    cd.bitsPerChannel = BitsPerChannelType.SIXTEEN;
    var ct = app.open(new File(ROOT + "coord_" + cw + "x" + ch + ".tif"));
    try { ct.colorProfileName = cd.colorProfileName; } catch(e) {}
    ct.selection.selectAll(); ct.selection.copy(); ct.close(SaveOptions.DONOTSAVECHANGES);
    app.activeDocument = cd; cd.activeLayer = cd.layers[0]; cd.paste().name = "__UVCODE";
    cd.close(SaveOptions.SAVECHANGES);   // saves only the embedded copy inside the duplicate
  }
  var W = UnitValue(Math.round(dup.width.as("px")), "px"), H = UnitValue(Math.round(dup.height.as("px")), "px");
  for (var i = 0; i < sos.length; i++){
    var nd = app.documents.add(W, H, dup.resolution, "uvview", NewDocumentMode.RGB, DocumentFill.TRANSPARENT, 1, BitsPerChannelType.SIXTEEN);
    try { nd.colorProfileName = dup.colorProfileName; } catch(e) {}
    app.activeDocument = dup; sos[i].duplicate(nd, ElementPlacement.PLACEATBEGINNING);
    app.activeDocument = nd; var L = nd.layers[0]; nd.activeLayer = L;
    L.grouped = false; L.blendMode = BlendMode.NORMAL; L.opacity = 100; L.fillOpacity = 100; L.visible = true;
    try { executeAction(s("disableLayerStyle"), undefined, DialogModes.NO); } catch(e) {}
    try { var hd = new ActionDescriptor(), hl = new ActionList(), hr = new ActionReference();   // smart filters would recolour the code
      hr.putClass(s("filterFX")); hr.putEnumerated(s("layer"), s("ordinal"), s("targetEnum")); hl.putReference(hr);
      hd.putList(s("null"), hl); executeAction(s("hide"), hd, DialogModes.NO); } catch(e) {}
    var o = new TiffSaveOptions(); o.imageCompression = TIFFEncoding.TIFFLZW; o.transparency = true; o.layers = false; o.alphaChannels = false; o.byteOrder = ByteOrder.IBM;
    nd.saveAs(new File(ROOT + "out/" + product + "__" + i + "__" + safeName(sos[i].name) + ".tif"), o, true);
    nd.close(SaveOptions.DONOTSAVECHANGES); log("  rendered " + sos[i].name);
  }
  dup.close(SaveOptions.DONOTSAVECHANGES);
}

var units = app.preferences.rulerUnits; app.preferences.rulerUnits = Units.PIXELS;
new Folder(ROOT + "out").create();
var jf = new File(ROOT + "jobs.txt"); jf.open("r"); var lines = jf.read().split(/\r?\n/); jf.close();
for (var j = 0; j < lines.length; j++){
  var p = lines[j].split("|"); if (p.length < 4) continue;
  try { bake(p[0], p[1], parseInt(p[2], 10), parseInt(p[3], 10)); } catch(e){ log("ERROR " + p[1] + ": " + e); closeTemp(); }
}
app.preferences.rulerUnits = units; log("ALL DONE");
alert("UV bake finished - now run: python -m headcover_tools bake-apply");
