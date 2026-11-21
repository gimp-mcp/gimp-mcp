#!/usr/bin/env python3
"""Live test for warp_region (#63).

It used to feed the layer to gegl:warp as if that op made an image (it makes
a displacement map) and to pass the stroke as a Python list, which GEGL
silently dropped: the whole layer turned black and the tool reported success.
Its plug-in-iwarp fallback doesn't exist in GIMP 3.2. Checks, on a white image
with a black vertical line (x 30-33), that:
- a vector pushes the line in its direction near (x, y), and leaves the rest of
  the layer as it was;
- several vectors in one call each push their own spot;
- x/y are image coordinates, also for a layer with an offset;
- pixels pulled in from beyond the layer's edge repeat the edge (not black);
- on a layer with alpha, transparent pixels pulled into a red square leave no
  dark fringe;
- an active selection limits the change;
- bad input is an error that changes nothing: no vectors, a missing or
  non-numeric value, a zero push, a push longer than the layer's diagonal plus
  the radius (gegl:warp's time grows with it), amount or radius out of range
  (a radius below 0.5 would silently become 20), a vector that
  doesn't reach the layer (even with valid vectors before it), a layer group,
  a layer with locked pixels, a layer inside a group with locked pixels.

Requires the GIMP MCP plugin running on localhost:9877 (GIMP_MCP_PORT
overrides the port). Exits 0 on pass, 1 on fail.
"""
import json
import os
import socket
import sys

HOST, PORT = '127.0.0.1', int(os.environ.get('GIMP_MCP_PORT', 9877))


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


def crisp_rect(img, x, y, w, h):
    """Code that selects a rectangle with feather off, whatever the context's feather."""
    return (f"Gimp.context_push()\ntry:\n    Gimp.context_set_feather(False)\n"
            f"    {img}.select_rectangle(Gimp.ChannelOps.REPLACE, {x}, {y}, {w}, {h})\n"
            "finally:\n    Gimp.context_pop()")


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def check(cond, msg, detail=None):
    if not cond:
        fail(f"{msg}: {detail}" if detail is not None else msg)


HELPERS = r'''
from gi.repository import Gegl
import hashlib

def _wr_new(img_w=64, img_h=64, off=(0, 0)):
    """A white 64x64 RGB layer with a black line at x 30-33, in a new clean image."""
    global _wr_img, _wr_layer
    _wr_img = Gimp.Image.new(img_w, img_h, Gimp.ImageBaseType.RGB)
    _wr_layer = Gimp.Layer.new(_wr_img, 'bg', 64, 64, Gimp.ImageType.RGB_IMAGE,
                               100, Gimp.LayerMode.NORMAL)
    _wr_img.insert_layer(_wr_layer, None, 0)
    _wr_layer.set_offsets(*off)
    Gimp.context_push()
    try:
        Gimp.context_set_opacity(100.0)
        Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)
        Gimp.context_set_feather(False)
        Gimp.context_set_foreground(Gegl.Color.new('#ffffff'))
        _wr_layer.edit_fill(Gimp.FillType.FOREGROUND)
        _wr_img.select_rectangle(Gimp.ChannelOps.REPLACE, off[0] + 30, off[1], 4, 64)
        Gimp.context_set_foreground(Gegl.Color.new('#000000'))
        _wr_layer.edit_fill(Gimp.FillType.FOREGROUND)
    finally:
        Gimp.context_pop()
    Gimp.Selection.none(_wr_img)
    _wr_img.clean_all()

def _wr_raw():
    return bytes(_wr_layer.get_buffer().get(Gegl.Rectangle.new(0, 0, 64, 64), 1.0,
                                            "R'G'B' u8", Gegl.AbyssPolicy.NONE))

def _wr_row(y):
    """Row y of the layer: '#' for dark pixels, '.' for light ones."""
    b = _wr_raw()
    return ''.join('#' if b[3 * (64 * y + i)] < 128 else '.' for i in range(64))

def _wr_hash():
    return hashlib.sha1(_wr_raw()).hexdigest()

def _wr_changed_rows(before):
    """Rows that differ from the raw bytes in before."""
    now = _wr_raw()
    return [y for y in range(64) if now[192 * y:192 * (y + 1)] != before[192 * y:192 * (y + 1)]]
'''

LINE = '.' * 30 + '####' + '.' * 30


def image_index():
    """Index of the test image in Gimp.get_images(), as the tools count it."""
    return int(py("print(next(i for i, im in enumerate(Gimp.get_images()) "
                  "if im.get_id() == _wr_img.get_id()))"))


def warp(vectors, **params):
    return cmd('warp_region', dict(vectors=vectors, image_index=image_index(), **params))


def row(y):
    return py(f"print(_wr_row({y}))")


def line_span(y):
    r = row(y)
    return (r.index('#'), r.rindex('#')) if '#' in r else None


def new_image(**kw):
    py("_wr_img.delete() if _wr_img is not None and _wr_img.is_valid() else None",
       f"_wr_new(**{kw!r})", "_wr_before = _wr_raw()")


def main():
    made_image = False
    try:
        py(HELPERS, "_wr_img = None")
        made_image = True

        new_image()
        r = warp([{'x': 32, 'y': 32, 'dx': 12, 'dy': 0, 'radius': 16, 'amount': 1}])
        check(r.get('status') == 'success', "warp_region failed", r)
        check(r['results'] == {'warped_vectors': 1, 'layer': 'bg'}, "wrong result", r)
        span = line_span(32)
        check(span is not None and span[0] >= 38, "the line was not pushed right at y=32",
              row(32))
        changed = py("print(_wr_changed_rows(_wr_before))")
        rows = json.loads(changed)
        # The cubic resampling may reach a pixel or two past the radius.
        check(rows and min(rows) >= 32 - 18 and max(rows) <= 32 + 18,
              "rows outside the radius changed", rows)
        check(row(0) == LINE and row(63) == LINE, "the rest of the layer changed",
              [row(0), row(63)])
        print("PASS: a vector pushes the line in its direction, only within its radius")

        new_image()
        r = warp([{'x': 32, 'y': 14, 'dx': 10, 'dy': 0, 'radius': 10, 'amount': 1},
                  {'x': 32, 'y': 50, 'dx': -10, 'dy': 0, 'radius': 10, 'amount': 1}])
        check(r.get('status') == 'success' and r['results']['warped_vectors'] == 2,
              "two vectors failed", r)
        right, left = line_span(14), line_span(50)
        check(right and right[0] >= 34, "the first vector did not push right", row(14))
        check(left and left[1] <= 29, "the second vector did not push left", row(50))
        check(row(32) == LINE, "the line between the vectors changed", row(32))
        print("PASS: several vectors each push their own spot")

        new_image(img_w=200, img_h=150, off=(100, 50))
        r = warp([{'x': 132, 'y': 82, 'dx': 12, 'dy': 0, 'radius': 16, 'amount': 1}])
        check(r.get('status') == 'success', "warp on an offset layer failed", r)
        span = line_span(32)
        check(span is not None and span[0] >= 38,
              "x/y were not read as image coordinates on an offset layer", row(32))
        print("PASS: x/y are image coordinates on a layer with an offset")

        new_image()
        r = warp([{'x': 2, 'y': 32, 'dx': 14, 'dy': 0, 'radius': 16, 'amount': 1}])
        check(r.get('status') == 'success', "warp at the edge failed", r)
        check(row(32)[:20] == '.' * 20, "pixels pulled from beyond the edge are not white",
              row(32))
        print("PASS: pixels pulled in from beyond the layer's edge repeat the edge")

        # A transparent RGBA layer with an opaque red square (16-47); push the
        # square's right edge left, pulling transparency in over the red.
        py("_wr_img.delete()",
           "_wr_new()",
           "_wr_a = Gimp.Layer.new(_wr_img, 'alpha', 64, 64, Gimp.ImageType.RGBA_IMAGE,"
           " 100, Gimp.LayerMode.NORMAL)",
           "_wr_img.insert_layer(_wr_a, None, 0)",
           "Gimp.context_push()\n"
           "try:\n"
           "    Gimp.context_set_opacity(100.0)\n"
           "    Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)\n"
           "    Gimp.context_set_feather(False)\n"
           "    _wr_img.select_rectangle(Gimp.ChannelOps.REPLACE, 16, 16, 32, 32)\n"
           "    Gimp.context_set_foreground(Gegl.Color.new('#ff0000'))\n"
           "    _wr_a.edit_fill(Gimp.FillType.FOREGROUND)\n"
           "finally:\n"
           "    Gimp.context_pop()",
           "Gimp.Selection.none(_wr_img)")
        r = warp([{'x': 52, 'y': 32, 'dx': -12, 'dy': 0, 'radius': 16, 'amount': 1}],
                 layer_name='alpha')
        check(r.get('status') == 'success', "warp on a layer with alpha failed", r)
        px = json.loads(py(
            "_b = bytes(_wr_a.get_buffer().get(Gegl.Rectangle.new(0, 0, 64, 64), 1.0,"
            " \"R'G'B'A u8\", Gegl.AbyssPolicy.NONE))",
            "print([list(_b[i:i + 4]) for i in range(0, len(_b), 4)])"))
        edge = [p for p in px[32 * 64 + 30:32 * 64 + 48]]
        check(any(p[3] == 0 for p in edge), "no transparency was pulled into the square",
              edge)
        fringe = [p for p in px if p[3] > 0 and (p[0] < 200 or p[1] > 40 or p[2] > 40)]
        check(not fringe, "partly transparent pixels are not red (dark fringe)", fringe[:5])
        print("PASS: on a layer with alpha, pulled-in transparency leaves no dark fringe")

        new_image()
        py(crisp_rect("_wr_img", 0, 0, 64, 32))
        r = warp([{'x': 32, 'y': 32, 'dx': 12, 'dy': 0, 'radius': 16, 'amount': 1}])
        check(r.get('status') == 'success', "warp with a selection failed", r)
        rows = json.loads(py("print(_wr_changed_rows(_wr_before))"))
        check(rows and max(rows) < 32, "pixels outside the selection changed", rows)
        print("PASS: an active selection limits the change")

        new_image()
        good = {'x': 32, 'y': 32, 'dx': 12, 'dy': 0}
        bad_inputs = [
            ([], 'non-empty'),
            ([{'x': 32, 'y': 32, 'dx': 12}], 'missing dy'),
            ([{'dx': 12, 'dy': 0}], 'missing x, y'),
            ([dict(good, dx='left')], 'must be numbers'),
            ([dict(good, dx=0, dy=0)], 'nothing to push'),
            ([dict(good, amount=0)], 'amount'),
            ([dict(good, amount=1.5)], 'amount'),
            ([dict(good, radius=0)], 'radius'),
            ([dict(good, radius=0.3)], 'radius'),
            ([dict(good, dx=1e9)], 'longer than'),
            ([dict(good, radius=6000)], 'radius'),
            ([good, dict(good, x=500)], "doesn't reach"),
        ]
        for vectors, words in bad_inputs:
            r = warp(vectors)
            check(r.get('status') == 'error' and words in r.get('error', ''),
                  f"bad input {vectors} did not fail with '{words}'", r)
        check(py("print(_wr_hash() == __import__('hashlib').sha1(_wr_before).hexdigest())")
              == "True", "a refused warp changed the layer")
        check(py("print(_wr_img.is_dirty())") == "False", "a refused warp marked the image dirty")

        py("_wr_grp = Gimp.GroupLayer.new(_wr_img, 'grp')",
           "_wr_img.insert_layer(_wr_grp, None, 0)",
           "_wr_img.set_selected_layers([_wr_grp])")
        r = warp([good])
        check(r.get('status') == 'error' and 'layer group' in r.get('error', ''),
              "warping a layer group did not fail", r)
        py("_wr_img.remove_layer(_wr_grp)", "_wr_layer.set_lock_content(True)")
        r = warp([good], layer_name='bg')
        check(r.get('status') == 'error' and 'locked' in r.get('error', ''),
              "warping a layer with locked pixels did not fail", r)
        py("_wr_layer.set_lock_content(False)")
        # A group's pixel lock covers the layers inside it, though the layer's
        # own flag is off; GIMP then refuses the write, which used to be ignored.
        py("_wr_grp = Gimp.GroupLayer.new(_wr_img, 'grp')",
           "_wr_img.insert_layer(_wr_grp, None, 0)",
           "_wr_img.reorder_item(_wr_layer, _wr_grp, 0)",
           "_wr_grp.set_lock_content(True)", "_wr_img.set_selected_layers([_wr_layer])",
           "_wr_img.clean_all()")
        # layer_name finds only top-level layers, so select the layer instead.
        r = warp([good])
        check(r.get('status') == 'error' and "group 'grp'" in r.get('error', '')
              and 'locked' in r.get('error', ''),
              "warping a layer in a group with locked pixels did not fail", r)
        check(py("print(_wr_img.is_dirty())") == "False",
              "a refused warp in a locked group marked the image dirty")
        check(py("print(_wr_hash() == __import__('hashlib').sha1(_wr_before).hexdigest())")
              == "True", "a refused warp changed the layer")
        print("PASS: bad input, a layer group and locked pixels (its own or its group's) "
              "are errors that change nothing")
        return 0
    finally:
        if made_image:
            try:
                py("_wr_img.delete() if _wr_img is not None and _wr_img.is_valid() else None")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
