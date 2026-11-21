#!/usr/bin/env python3
"""Live test for get_pixel_color outside the layer and with non-whole x/y (#63).

x and y are relative to the layer. GIMP's get_pixel() returns None outside
it, and the tool failed with "'NoneType' object is not subscriptable". x and
y also went through int(), so 1.9 read pixel 1 and true pixel 1. Checks that:
- a point outside the layer, also one inside the image but outside an offset
  layer, is an error giving the layer's size and offsets;
- a fractional, boolean or non-numeric x or y is an error naming it;
- the layer's corners, whole-number floats and digit strings still work,
  and so does a layer group.

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


def pixel(x, y, layer='small'):
    index = int(py("print([i.get_id() for i in Gimp.get_images()].index(_PX.get_id()))"))
    return cmd('get_pixel_color', {'x': x, 'y': y, 'layer_name': layer, 'image_index': index})


def main():
    made = False
    try:
        py("_PX = Gimp.Image.new(100, 80, Gimp.ImageBaseType.RGB)")
        made = True
        py("_l = Gimp.Layer.new(_PX, 'small', 20, 10, Gimp.ImageType.RGBA_IMAGE, 100,"
           " Gimp.LayerMode.NORMAL)",
           "_PX.insert_layer(_l, None, 0)",
           "_l.set_offsets(30, 40)",
           "_l.fill(Gimp.FillType.WHITE)",
           "_g = Gimp.GroupLayer.new(_PX, 'grp')",
           "_PX.insert_layer(_g, None, 0)")

        for x, y in ((20, 0), (0, 10), (-1, 0), (0, -1), (35, 45), (10 ** 6, 0)):
            r = pixel(x, y)
            check(r.get('status') == 'error'
                  and f"({x}, {y}) is outside layer 'small' (20x10)" in r.get('error', '')
                  and '(30, 40)' in r.get('error', ''),
                  f"({x}, {y}) outside the layer was not refused clearly", r)
        print("PASS: a point outside the layer is an error giving its size and offsets")

        for x, y, bad in ((1.9, 0, 'x'), (0, -0.5, 'y'), (True, 0, 'x'), (0, 'a', 'y'),
                          (None, 0, 'x')):
            r = pixel(x, y)
            check(r.get('status') == 'error'
                  and f"{bad} must be a whole number" in r.get('error', ''),
                  f"x={x!r}, y={y!r} was not refused", r)
        print("PASS: a fractional, boolean or non-numeric x or y is an error")

        for x, y in ((0, 0), (19, 9), (2.0, '3'), (' 5', 4.0)):
            r = pixel(x, y)
            check(r.get('status') == 'success' and r['results'].get('color_hex') == '#ffffff'
                  and r['results'].get('alpha') == 255, f"({x!r}, {y!r}) inside the layer failed", r)
        r = pixel(0, 0, layer='grp')
        check(r.get('status') == 'success', "reading a layer group failed", r)
        print("PASS: corners, whole-number floats, digit strings and groups still work")
        return 0
    finally:
        if made:
            try:
                py("_PX.delete() if _PX.is_valid() else None")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
