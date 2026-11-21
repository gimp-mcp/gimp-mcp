#!/usr/bin/env python3
"""Live test for crop_to_selection (#63).

With no selection it used to report success, change nothing and still mark the
image dirty, and it never returned the x, y, width and height its docs promise.
Checks that:
- with no selection it returns an error, and the image is unchanged and clean;
- with a selection it crops to the selection's bounds, returns them, and the
  selection stays;
- autocrop returns the image's new width and height.

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
                  "if im.get_id() == _crop_img.get_id()))"))


def new_test_image():
    """A 100x80 white image with a red 30x25 rectangle at (20, 10), clean, no selection."""
    py("_crop_img = Gimp.Image.new(100, 80, Gimp.ImageBaseType.RGB)",
       "_l = Gimp.Layer.new(_crop_img, 'bg', 100, 80, Gimp.ImageType.RGB_IMAGE,"
       " 100, Gimp.LayerMode.NORMAL)",
       "_crop_img.insert_layer(_l, None, 0)",
       # One block, so the context is popped even if a fill fails. Earlier
       # call_api code may have changed opacity, paint mode or feathering.
       "Gimp.context_push()\n"
       "try:\n"
       "    Gimp.context_set_opacity(100.0)\n"
       "    Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)\n"
       "    Gimp.context_set_feather(False)\n"
       "    Gimp.context_set_foreground(Gegl.Color.new('#ffffff'))\n"
       "    _l.edit_fill(Gimp.FillType.FOREGROUND)\n"
       "    _crop_img.select_rectangle(Gimp.ChannelOps.REPLACE, 20, 10, 30, 25)\n"
       "    Gimp.context_set_foreground(Gegl.Color.new('#ff0000'))\n"
       "    _l.edit_fill(Gimp.FillType.FOREGROUND)\n"
       "finally:\n"
       "    Gimp.context_pop()",
       "Gimp.Selection.none(_crop_img)",
       "_crop_img.clean_all()")


def size():
    return py("print(_crop_img.get_width(), _crop_img.get_height())")


def main():
    made_image = False
    try:
        py("from gi.repository import Gegl")
        new_test_image()
        made_image = True

        r = cmd('crop_to_selection', {'image_index': image_index()})
        check(r.get('status') == 'error' and 'no selection' in r.get('error', ''),
              "crop_to_selection with no selection did not fail", r)
        check(size() == "100 80", "the image changed", size())
        check(py("print(_crop_img.is_dirty())") == "False", "the image was marked dirty")
        print("PASS: with no selection it returns an error and changes nothing")

        # Without feathering, so the selection's bounds are exactly the ellipse's.
        py("Gimp.context_push()\n"
           "try:\n"
           "    Gimp.context_set_feather(False)\n"
           "    _crop_img.select_ellipse(Gimp.ChannelOps.REPLACE, 20, 10, 30, 25)\n"
           "finally:\n"
           "    Gimp.context_pop()")
        r = cmd('crop_to_selection', {'image_index': image_index()})
        check(r.get('status') == 'success', "crop_to_selection failed", r)
        got = {k: r['results'].get(k) for k in ('x', 'y', 'width', 'height')}
        check(got == {'x': 20, 'y': 10, 'width': 30, 'height': 25}, "wrong crop region", r)
        check(size() == "30 25", "the image was not cropped to the selection", size())
        bounds = py("_b = Gimp.Selection.bounds(_crop_img)", "print(_b[1], _b[4], _b[5])")
        check(bounds == "True 30 25", "the selection did not stay", bounds)
        print("PASS: it crops to the selection's bounds, returns them and keeps the selection")

        py("_crop_img.delete()")
        made_image = False
        new_test_image()
        made_image = True
        r = cmd('crop_to_selection', {'autocrop': True, 'image_index': image_index()})
        check(r.get('status') == 'success', "autocrop failed", r)
        check(r['results'].get('width') == 30 and r['results'].get('height') == 25,
              "autocrop did not return the new size", r)
        check(size() == "30 25", "autocrop did not crop to the red rectangle", size())
        print("PASS: autocrop returns the image's new size")
        return 0
    finally:
        if made_image:
            try:
                py("_crop_img.delete()")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
