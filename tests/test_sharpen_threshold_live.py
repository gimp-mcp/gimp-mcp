#!/usr/bin/env python3
"""Live test for sharpen's threshold.

A threshold outside 0-255 failed only inside the undo group (the image was
marked as changed) with GEGL's range error on its 0-1 scale, and int() cut
1.9 (and True) to 1. Checks that:
- a threshold that isn't a whole number 0-255 is an error naming threshold,
  and the image stays clean with its pixels unchanged;
- 0, 255, a whole-number float and a digit string work.

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


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def check(cond, msg, detail=None):
    if not cond:
        fail(f"{msg}: {detail}" if detail is not None else msg)


def image_index():
    """Index of the test image in Gimp.get_images(), as the tools count it."""
    return int(py("print(next(i for i, im in enumerate(Gimp.get_images()) "
                  "if im.get_id() == _shp_img.get_id()))"))


def sharpen(threshold):
    return cmd('sharpen', {'image_index': image_index(), 'threshold': threshold})


def pixels():
    """A digest of the layer's pixels, to see whether anything changed."""
    return py("_l = _shp_img.get_layers()[0]",
              "_b = _l.get_buffer()",
              "_r = _b.get_extent()",
              "print(hash(bytes(_b.get(_r, 1.0, 'R\\'G\\'B\\' u8', Gegl.AbyssPolicy.NONE))))")


def main():
    made_image = False
    try:
        py("from gi.repository import Gegl", "_shp_img = None")
        # Owned from here on, so cleanup also runs if the setup fails part way.
        made_image = True
        # 40x40 gray with a white square: an edge, so sharpening changes pixels.
        py("_shp_img = Gimp.Image.new(40, 40, Gimp.ImageBaseType.RGB)",
           "_l = Gimp.Layer.new(_shp_img, 'bg', 40, 40, Gimp.ImageType.RGB_IMAGE,"
           " 100, Gimp.LayerMode.NORMAL)",
           "_shp_img.insert_layer(_l, None, 0)",
           # One block, so the context is popped even if a fill fails. Earlier
           # call_api code may have changed opacity, paint mode or feathering.
           "Gimp.context_push()\n"
           "try:\n"
           "    Gimp.context_set_opacity(100.0)\n"
           "    Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)\n"
           "    Gimp.context_set_feather(False)\n"
           "    Gimp.context_set_foreground(Gegl.Color.new('#808080'))\n"
           "    _l.edit_fill(Gimp.FillType.FOREGROUND)\n"
           "    _shp_img.select_rectangle(Gimp.ChannelOps.REPLACE, 10, 10, 20, 20)\n"
           "    Gimp.context_set_foreground(Gegl.Color.new('#ffffff'))\n"
           "    _l.edit_fill(Gimp.FillType.FOREGROUND)\n"
           "finally:\n"
           "    Gimp.context_pop()",
           "Gimp.Selection.none(_shp_img)",
           "_shp_img.clean_all()")
        before = pixels()

        for bad in (300, -5, 256, 1.9, True, 'abc', None):
            r = sharpen(bad)
            check(r.get('status') == 'error' and 'threshold' in r.get('error', '')
                  and 'gegl' not in r.get('error', ''),
                  f"threshold={bad!r} did not fail with the tool's own error", r)
            check(py("print(_shp_img.is_dirty())") == "False",
                  f"threshold={bad!r} marked the image dirty")
            check(pixels() == before, f"threshold={bad!r} changed the pixels")
        print("PASS: a threshold that isn't a whole number 0-255 is an error; image unchanged")

        for good in (0, 255, 20.0, '20'):
            r = sharpen(good)
            check(r.get('status') == 'success', f"threshold={good!r} failed", r)
        check(pixels() != before, "sharpening changed no pixels")
        print("PASS: 0, 255, a whole-number float and a digit string work")
        return 0
    finally:
        if made_image:
            try:
                py("if _shp_img is not None and _shp_img.is_valid(): _shp_img.delete()")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
