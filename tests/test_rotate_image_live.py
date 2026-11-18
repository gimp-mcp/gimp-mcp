#!/usr/bin/env python3
"""Live test for rotate_image with angles that aren't multiples of 90 (#63).

These used to fail: the plugin looked up gimp-item-transform-rotate-default,
which GIMP 3.2 doesn't have. Checks that a 30-degree rotation:
- grows the canvas to the rotated image's bounding box and keeps every layer
  (nothing is flattened), with transparent corners;
- turns clockwise, like the 90-degree rotation, and moves the layers, a path
  and the selection about the image centre, rotating a layer group's children
  with it;
- rotates the whole layer, not just the selected pixels, leaves no floating
  selection or extra channel.
And that multiples of 90 (including negative and > 360) still rotate
losslessly, 0 changes nothing, and a non-finite angle is rejected.

Requires the GIMP MCP plugin running on localhost:9877 (GIMP_MCP_PORT
overrides the port). Exits 0 on pass, 1 on fail.
"""
import json
import math
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
    """Return the current index of the test image in Gimp.get_images()."""
    return int(py("print(next(i for i, im in enumerate(Gimp.get_images()) "
                  "if im.get_id() == _rot_img.get_id()))"))


def rotate(angle):
    return cmd('rotate_image', {'angle': angle, 'image_index': image_index()})


def size():
    return json.loads(py("print(json.dumps([_rot_img.get_width(), _rot_img.get_height()]))"))


def state():
    """Return the layers (name, size, offsets, alpha), channels and floating selection."""
    return json.loads(py(
        "print(json.dumps({"
        "'layers': [[l.get_name(), l.get_width(), l.get_height(), list(l.get_offsets()[1:]),"
        " l.has_alpha()] for l in _rot_img.get_layers()],"
        "'channels': [c.get_name() for c in _rot_img.get_channels()],"
        "'floating': _rot_img.get_floating_sel() is not None}))"))


def rotated(x, y, angle, w, h):
    """Where (x, y) of a w x h image lands after rotate_image(angle) grows the canvas."""
    rad = math.radians(angle)
    c, s = math.cos(rad), math.sin(rad)
    new_w = math.ceil(w * abs(c) + h * abs(s) - 1e-6)
    new_h = math.ceil(w * abs(s) + h * abs(c) - 1e-6)
    cx, cy = (new_w - w) // 2 + w / 2, (new_h - h) // 2 + h / 2
    dx, dy = x - w / 2, y - h / 2
    # Image y points down, so this matrix turns clockwise on screen.
    return cx + dx * c - dy * s, cy + dx * s + dy * c


def near(a, b, tol=2.0):
    return all(abs(p - q) <= tol for p, q in zip(a, b))


def main():
    made_image = False
    try:
        W, H = 200, 100
        py("import json",
           "from gi.repository import Gegl",
           f"_rot_img = Gimp.Image.new({W}, {H}, Gimp.ImageBaseType.RGB)",
           f"_l = Gimp.Layer.new(_rot_img, 'bg', {W}, {H}, Gimp.ImageType.RGB_IMAGE,"
           " 100, Gimp.LayerMode.NORMAL)",
           "_rot_img.insert_layer(_l, None, 0)",
           "_l.fill(Gimp.FillType.WHITE)",
           # A red 40x20 layer at (150, 10): centre (170, 20).
           "_l = Gimp.Layer.new(_rot_img, 'red', 40, 20, Gimp.ImageType.RGBA_IMAGE,"
           " 100, Gimp.LayerMode.NORMAL)",
           "_rot_img.insert_layer(_l, None, 0)",
           "_l.set_offsets(150, 10)",
           "Gimp.context_push()",
           "Gimp.context_set_foreground(Gegl.Color.new('#ff0000'))",
           "_l.fill(Gimp.FillType.FOREGROUND)",
           "Gimp.context_pop()",
           # A group holding a 20x20 layer at (20, 60): centre (30, 70).
           "_g = Gimp.GroupLayer.new(_rot_img, 'grp')",
           "_rot_img.insert_layer(_g, None, 0)",
           "_l = Gimp.Layer.new(_rot_img, 'child', 20, 20, Gimp.ImageType.RGBA_IMAGE,"
           " 100, Gimp.LayerMode.NORMAL)",
           "_rot_img.insert_layer(_l, _g, 0)",
           "_l.set_offsets(20, 60)",
           "_l.fill(Gimp.FillType.WHITE)",
           "_c = Gimp.Channel.new(_rot_img, 'mask', 200, 100, 50.0, Gegl.Color.new('#00ff00'))",
           "_rot_img.insert_channel(_c, None, 0)",
           "_p = Gimp.Path.new(_rot_img, 'path')",
           "_rot_img.insert_path(_p, None, 0)",
           "_sid = _p.bezier_stroke_new_moveto(10, 10)",
           "_p.bezier_stroke_lineto(_sid, 190, 90)",
           # The selection: a 20x20 square at (0, 0), centre (10, 10).
           "_rot_img.select_rectangle(Gimp.ChannelOps.REPLACE, 0, 0, 20, 20)",
           "_rot_img.clean_all()")
        made_image = True
        check(size() == [W, H], "setup failed", size())

        # 0 and 360 change nothing.
        for angle in (0, 360):
            r = rotate(angle)
            check(r.get('status') == 'success', f"rotate_image({angle}) failed", r)
            check(size() == [W, H], f"rotate_image({angle}) changed the size", size())
        check(py("print(_rot_img.is_dirty())") == "False", "rotate_image(0) changed the image")
        print("PASS: 0 and 360 change nothing")

        # 30 degrees: the canvas grows to the rotated bounding box.
        r = rotate(30)
        check(r.get('status') == 'success', "rotate_image(30) failed", r)
        new_w = math.ceil(W * math.cos(math.radians(30)) + H * 0.5 - 1e-6)
        new_h = math.ceil(W * 0.5 + H * math.cos(math.radians(30)) - 1e-6)
        check(size() == [new_w, new_h], f"expected a {new_w}x{new_h} canvas", size())
        res = r.get('results', {})
        check([res.get('width'), res.get('height')] == [new_w, new_h],
              "the reported size is wrong", res)
        st = state()
        check([ly[0] for ly in st['layers']] == ['grp', 'red', 'bg'],
              "the layers were not kept", st)
        check(all(ly[4] for ly in st['layers']), "a rotated layer has no alpha", st)
        check(st['channels'] == ['mask'], "a channel was lost or left behind", st)
        check(not st['floating'], "the rotation left a floating selection", st)
        print("PASS: 30 degrees grows the canvas and keeps layers, channels, no floating selection")

        # Centres moved clockwise about the image centre.
        def centre(name, parent=None):
            where = "_g.get_children()" if parent else "_rot_img.get_layers()"
            x, y, w, h = json.loads(py(
                f"_l = next(l for l in {where} if l.get_name() == '{name}')",
                "print(json.dumps(list(_l.get_offsets()[1:]) + [_l.get_width(), _l.get_height()]))"))
            return x + w / 2, y + h / 2

        for name, parent, orig in (('red', None, (170, 20)), ('child', 'grp', (30, 70))):
            want = rotated(*orig, 30, W, H)
            got = centre(name, parent)
            check(near(got, want), f"layer {name!r} is not where a clockwise turn puts it",
                  f"got {got}, want {want}")
        pt = json.loads(py("print(json.dumps(list(_p.stroke_get_points(_p.get_strokes()[0])[1][:2])))"))
        check(near(pt, rotated(10, 10, 30, W, H)), "the path was not rotated", pt)
        b = json.loads(py("_b = Gimp.Selection.bounds(_rot_img)",
                          "print(json.dumps([_b[1], _b[2], _b[3], _b[4], _b[5]]))"))
        check(b[0], "the selection was lost")
        check(near(((b[1] + b[3]) / 2, (b[2] + b[4]) / 2), rotated(10, 10, 30, W, H)),
              "the selection was not rotated", b)
        print("PASS: layers, a group's child, the path and the selection turn clockwise")

        # The background layer turned as a whole: its old centre is still white and
        # opaque, the canvas corner is transparent.
        px = json.loads(py(
            "_b = next(l for l in _rot_img.get_layers() if l.get_name() == 'bg')",
            "_ox, _oy = _b.get_offsets()[1:]",
            "print(json.dumps([list(_b.get_pixel(int(x) - _ox, int(y) - _oy).get_rgba())"
            f" for x, y in (({new_w} / 2, {new_h} / 2), (1, 1))]))"))
        check(px[0] == [1.0, 1.0, 1.0, 1.0], "the image centre is not white", px[0])
        check(px[1][3] == 0.0, "the canvas corner is not transparent", px[1])
        print("PASS: the whole layer turned; the corners are transparent")

        # Multiples of 90 rotate losslessly, whatever their sign.
        for angle, want in ((-90, [new_h, new_w]), (450, [new_w, new_h]), (-180, [new_w, new_h])):
            r = rotate(angle)
            check(r.get('status') == 'success', f"rotate_image({angle}) failed", r)
            check(size() == want, f"rotate_image({angle}) gave the wrong size", size())
        print("PASS: -90, 450 and -180 rotate by right angles")

        for bad in ('nan', 'inf'):
            r = rotate(float(bad))
            check(r.get('status') == 'error' and 'finite' in r.get('error', ''),
                  f"angle {bad} was not rejected", r)
        print("PASS: non-finite angles are rejected")
        return 0
    finally:
        if made_image:
            try:
                py("_rot_img.delete()")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
