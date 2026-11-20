#!/usr/bin/env python3
"""Live test for fill_layer keeping the caller's selection (#63).

fill_layer selects all to fill the whole layer, and then cleared the
selection, so a selection made before it was lost. Checks that:
- the whole layer is filled, also outside the selection, and also a
  layer's parts outside the canvas (select all, which fill_layer used,
  stops at the canvas edge);
- a feathered selection comes back value for value, no helper channel is
  left in the image, and the selected layer stays selected;
- with no selection, there is still none afterwards;
- a layer with locked alpha keeps its transparent pixels;
- when the fill fails (content-locked layer), the selection still comes back.

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


def fill(layer, color='red'):
    index = int(py("print([i.get_id() for i in Gimp.get_images()].index(_FL.get_id()))"))
    return send({'type': 'fill_layer',
                 'params': {'layer_name': layer, 'color': color, 'image_index': index}})


def selection():
    """The selection's value at every 4th pixel, the number of channels and the
    selected layers (a channel added and removed again used to leave none)."""
    return json.loads(py(
        "print(__import__('json').dumps([[round(Gimp.Selection.value(_FL, x, y), 3)"
        " for x in range(0, 40, 4) for y in range(0, 40, 4)], len(_FL.get_channels()),"
        " [l.get_name() for l in _FL.get_selected_layers()]]))"))


def pixel(layer, x, y):
    return json.loads(py(
        f"_c = [l for l in _FL.get_layers() if l.get_name() == {layer!r}][0].get_pixel({x}, {y})",
        "print(__import__('json').dumps([round(v, 3) for v in _c.get_rgba()]))"))


def feathered_selection():
    py("Gimp.context_push()\n"
       "try:\n"
       "    Gimp.context_set_feather(True)\n"
       "    Gimp.context_set_feather_radius(6, 6)\n"
       "    _FL.select_ellipse(Gimp.ChannelOps.REPLACE, 8, 8, 20, 20)\n"
       "finally:\n"
       "    Gimp.context_pop()")


def main():
    made = False
    try:
        py("_FL = Gimp.Image.new(40, 40, Gimp.ImageBaseType.RGB)")
        made = True
        py("for _n in ('locked', 'alpha', 'plain'):\n"
           "    _FL.insert_layer(Gimp.Layer.new(_FL, _n, 40, 40, Gimp.ImageType.RGBA_IMAGE, 100,"
           " Gimp.LayerMode.NORMAL), None, 0)")

        py("_FL.set_selected_layers([l for l in _FL.get_layers() if l.get_name() == 'alpha'])")
        feathered_selection()
        before = selection()
        check(any(0 < v < 255 for v in before[0]), "the test selection isn't feathered", before)
        check(before[2] == ['alpha'], "the test didn't select layer 'alpha'", before[2])
        r = fill('plain')
        check(r.get('status') == 'success', "fill_layer failed", r)
        check(selection() == before, "fill_layer didn't keep the feathered selection",
              (before, selection()))
        check(pixel('plain', 0, 0) == [1.0, 0.0, 0.0, 1.0]
              and pixel('plain', 39, 39) == [1.0, 0.0, 0.0, 1.0],
              "fill_layer didn't fill outside the selection",
              (pixel('plain', 0, 0), pixel('plain', 39, 39)))
        print("PASS: the whole layer is filled and the feathered selection comes back")

        py("Gimp.Selection.none(_FL)")
        r = fill('plain', 'blue')
        check(r.get('status') == 'success', "fill_layer without a selection failed", r)
        check(py("print(Gimp.Selection.is_empty(_FL))") == 'True' and selection()[1] == 0,
              "fill_layer without a selection left one, or a channel", selection())
        print("PASS: with no selection there is none afterwards")

        # A layer larger than the canvas, reaching past it on every side.
        py("_b = Gimp.Layer.new(_FL, 'big', 80, 80, Gimp.ImageType.RGBA_IMAGE, 100,"
           " Gimp.LayerMode.NORMAL)",
           "_FL.insert_layer(_b, None, 0)", "_b.set_offsets(-20, -20)",
           "_b.fill(Gimp.FillType.WHITE)")
        for selected in (True, False):
            if selected:
                feathered_selection()
            else:
                py("Gimp.Selection.none(_FL)")
            before = selection()
            r = fill('big', 'red' if selected else 'blue')
            want = [1.0, 0.0, 0.0, 1.0] if selected else [0.0, 0.0, 1.0, 1.0]
            corners = [pixel('big', x, y) for x, y in ((0, 0), (79, 0), (0, 79), (79, 79))]
            check(r.get('status') == 'success' and all(c == want for c in corners)
                  and pixel('big', 40, 40) == want,
                  f"fill_layer {'with' if selected else 'without'} a selection didn't fill the"
                  f" layer outside the canvas", (r, corners))
            check(selection() == before, "fill_layer changed the selection of a big layer",
                  (before, selection()))
        print("PASS: the parts of a layer outside the canvas are filled too")

        # Opaque on the left half only, then alpha-locked.
        # The context's opacity, paint mode and feather are set, so the
        # fixture is opaque and sharp whatever an earlier call left there.
        py("from gi.repository import Gegl",
           "_l = [l for l in _FL.get_layers() if l.get_name() == 'alpha'][0]",
           "Gimp.context_push()\n"
           "try:\n"
           "    Gimp.context_set_opacity(100.0)\n"
           "    Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)\n"
           "    Gimp.context_set_feather(False)\n"
           "    _FL.select_rectangle(Gimp.ChannelOps.REPLACE, 0, 0, 20, 40)\n"
           "    Gimp.context_set_foreground(Gegl.Color.new('#00ff00'))\n"
           "    _l.edit_fill(Gimp.FillType.FOREGROUND)\n"
           "finally:\n"
           "    Gimp.context_pop()",
           "Gimp.Selection.none(_FL)",
           "_l.set_lock_alpha(True)")
        r = fill('alpha')
        check(r.get('status') == 'success', "fill_layer on an alpha-locked layer failed", r)
        check(pixel('alpha', 5, 5) == [1.0, 0.0, 0.0, 1.0] and pixel('alpha', 30, 5)[3] == 0.0,
              "fill_layer didn't respect the alpha lock",
              (pixel('alpha', 5, 5), pixel('alpha', 30, 5)))
        print("PASS: a layer with locked alpha keeps its transparent pixels")

        py("[l for l in _FL.get_layers() if l.get_name() == 'locked'][0].set_lock_content(True)")
        feathered_selection()
        before = selection()
        r = fill('locked')
        check(r.get('status') == 'error', "fill_layer on a content-locked layer succeeded", r)
        check(selection() == before, "a failed fill_layer didn't restore the selection",
              (before, selection()))
        print("PASS: a failed fill still restores the selection")
        return 0
    finally:
        if made:
            try:
                py("_FL.delete() if _FL.is_valid() else None")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
