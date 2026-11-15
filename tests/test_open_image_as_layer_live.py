#!/usr/bin/env python3
"""Live test for the open_image_as_layer tool.

Builds a 2-layer 100x80 XCF with a transparent area (a red square in one
layer, a blue square in a smaller, offset layer), and a 200x150 target image
with three layers. It then imports the XCF in several ways and checks the
new layer: transparency kept (with a lime background color in the context,
which a flatten would leak in), content kept where it sits on the source
canvas, size and offsets for each scale mode, stack position, the reported
values, no temporary image left open, and errors leaving the target unchanged.

Requires the GIMP MCP plugin running on localhost:9877 (GIMP_MCP_PORT
overrides the port). The XCF goes under ~/tmp, because a Snap GIMP has a
private /tmp. Exits 0 on pass, 1 on fail.
"""
import json
import os
import socket
import sys

HOST, PORT = '127.0.0.1', int(os.environ.get('GIMP_MCP_PORT', 9877))
SRC_DIR = os.path.join(os.path.expanduser('~'), 'tmp', 'gimp-mcp-test')
SRC = os.path.join(SRC_DIR, 'as_layer_src.xcf')


def send(msg, timeout=60):
    """Send one JSON message to the plugin socket and return the decoded reply."""
    with socket.create_connection((HOST, PORT), timeout=timeout) as s:
        s.sendall(json.dumps(msg).encode() + b'\n')
        buf = b''
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            try:
                return json.loads(buf.decode())
            except json.JSONDecodeError:
                continue
    return {'status': 'error', 'error': 'parse: ' + buf.decode()[:200]}


def cmd(t, params=None):
    """Send a {type, params} tool command."""
    return send({'type': t, 'params': params or {}})


def py(*lines):
    """Run Python lines in the plugin's persistent context; return the printed output."""
    r = send({'cmds': list(lines)})
    if r.get('status') != 'success':
        fail(f"exec failed: {r.get('error', r)}")
    return ''.join(r.get('results') or []).strip()


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def check(cond, msg, detail=None):
    if not cond:
        fail(f"{msg}: {detail}" if detail is not None else msg)


def image_count():
    return int(py("print(len(Gimp.get_images()))"))


def image_index():
    """Return the current index of the target image in Gimp.get_images()."""
    return int(py("print(next(i for i, im in enumerate(Gimp.get_images()) "
                  "if im.get_id() == _aslayer_img.get_id()))"))


def layer_names():
    return json.loads(py("print(json.dumps([l.get_name() for l in _aslayer_img.get_layers()]))"))


def pixel(layer_id, x, y):
    """Return [r, g, b, a] (0-255) of the layer at layer-local (x, y)."""
    return json.loads(py(
        f"_l = Gimp.Item.get_by_id({layer_id})",
        f"_c = _l.get_pixel({x}, {y})",
        "print(json.dumps([round(v * 255) for v in _c.get_rgba()]))",
    ))


def has_alpha(layer_id):
    return py(f"print(Gimp.Item.get_by_id({layer_id}).has_alpha())") == "True"


def import_layer(**params):
    r = cmd('open_image_as_layer', {'file_path': SRC, 'image_index': image_index(), **params})
    check(r.get('status') == 'success', f"open_image_as_layer {params} failed", r.get('error', r))
    return r['results']


def make_source():
    """Save a 100x80 two-layer XCF: red 20x20 at (10,10), blue 30x30 layer at (60,40)."""
    os.makedirs(SRC_DIR, exist_ok=True)
    py(
        "import json",
        "from gi.repository import Gimp, Gegl, Gio",
        "_src = Gimp.Image.new(100, 80, Gimp.ImageBaseType.RGB)",
        "_a = Gimp.Layer.new(_src, 'red', 100, 80, Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)",
        "_src.insert_layer(_a, None, 0)",
        "_b = Gimp.Layer.new(_src, 'blue', 30, 30, Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)",
        "_src.insert_layer(_b, None, 0)",
        "_b.set_offsets(60, 40)",
        "Gimp.context_push()",
        "Gimp.context_set_opacity(100.0)",
        "Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)",
        "_a.edit_fill(Gimp.FillType.TRANSPARENT)",
        "_src.select_rectangle(Gimp.ChannelOps.REPLACE, 10, 10, 20, 20)",
        "Gimp.context_set_foreground(Gegl.Color.new('rgb(1.0, 0.0, 0.0)'))",
        "_a.edit_fill(Gimp.FillType.FOREGROUND)",
        "Gimp.Selection.none(_src)",
        "Gimp.context_set_foreground(Gegl.Color.new('rgb(0.0, 0.0, 1.0)'))",
        "_b.edit_fill(Gimp.FillType.FOREGROUND)",
        "Gimp.context_pop()",
        f"_ok = Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, _src, Gio.File.new_for_path({SRC!r}), None)",
        "_src.delete()",
        "print(_ok)",
    )
    check(os.path.exists(SRC), "source XCF was not written", SRC)


def make_target():
    """Create a display-less 200x150 RGB image with layers top/middle/bottom, middle selected."""
    py(
        "_aslayer_img = Gimp.Image.new(200, 150, Gimp.ImageBaseType.RGB)",
        "for _n in ('bottom', 'middle', 'top'):\n"
        "    _l = Gimp.Layer.new(_aslayer_img, _n, 200, 150, Gimp.ImageType.RGBA_IMAGE, 100, "
        "Gimp.LayerMode.NORMAL)\n"
        "    _aslayer_img.insert_layer(_l, None, 0)",
        "_aslayer_img.set_selected_layers([_aslayer_img.get_layers()[1]])",
    )


def main():
    made_target = False
    try:
        make_source()
        make_target()
        made_target = True
        # A flatten would fill transparent areas with this color.
        py("_aslayer_bg = Gimp.context_get_background()",
           "Gimp.context_set_background(Gegl.Color.new('lime'))")
        count = image_count()

        # Default: file's size, centered, on top (not above the selected layer).
        r = import_layer()
        check(image_count() == count, "a temporary image was left open", image_count())
        check(r['layer_name'] == 'as_layer_src', "default layer name", r)
        check((r['width'], r['height']) == (100, 80), "default size", r)
        check((r['source_width'], r['source_height']) == (100, 80), "source size", r)
        check((r['offset_x'], r['offset_y']) == (50, 35), "default placement is centered", r)
        check(r['position'] == 0 and layer_names()[0] == r['layer_name'],
              "default position is the top", (r, layer_names()))
        lid = r['layer_id']
        check(has_alpha(lid), "imported layer lost its alpha channel")
        check(pixel(lid, 0, 0)[3] == 0, "transparent area is not transparent", pixel(lid, 0, 0))
        check(pixel(lid, 15, 15) == [255, 0, 0, 255], "red square moved or changed", pixel(lid, 15, 15))
        check(pixel(lid, 70, 50) == [0, 0, 255, 255], "blue layer lost its offset", pixel(lid, 70, 50))
        print("PASS: default import keeps alpha and source placement, centered, on top, no leak")

        r = import_layer(scale='fit', layer_name='fit')
        check((r['width'], r['height']) == (188, 150), "fit size", r)
        check((r['offset_x'], r['offset_y']) == (6, 0), "fit placement is centered", r)
        print("PASS: scale='fit'")

        r = import_layer(scale='canvas', layer_name='canvas', offset_x=0, offset_y=0)
        check((r['width'], r['height']) == (200, 150), "canvas size", r)
        check((r['offset_x'], r['offset_y']) == (0, 0), "canvas offsets", r)
        print("PASS: scale='canvas'")

        r = import_layer(layer_name='placed', offset_x=5, offset_y=7)
        check((r['offset_x'], r['offset_y']) == (5, 7), "explicit offsets", r)
        r = import_layer(layer_name='third', position=2)
        check(r['position'] == 2 and layer_names()[2] == 'third', "position=2", (r, layer_names()))
        r = import_layer(layer_name='last', position=99)
        names = layer_names()
        check(r['position'] == len(names) - 1 and names[-1] == 'last', "position past the bottom",
              (r, names))
        print("PASS: offsets and positions")

        before = layer_names()
        for params in ({'file_path': os.path.join(SRC_DIR, 'missing.png')},
                       {'position': -1}, {'scale': 'stretch'}):
            r = cmd('open_image_as_layer', {'file_path': SRC, 'image_index': image_index(), **params})
            check(r.get('status') == 'error', f"{params} did not fail", r)
            check(layer_names() == before, f"{params} changed the target", layer_names())
            check(image_count() == count, f"{params} left an image open", image_count())
        print("PASS: bad file, position and scale -> error, target unchanged")
        return 0
    finally:
        try:
            py("Gimp.context_set_background(_aslayer_bg)")
        except SystemExit:
            pass
        if made_target:
            try:
                py("_aslayer_img.delete()")
            except SystemExit:
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
