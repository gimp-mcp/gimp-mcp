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
  selection or extra channel;
- keeps the selected layers and path, so a later draw_rectangle strokes, and
  turns the selection mask exactly: the context's feather (set for every
  rotation here) must not reach it.
And that multiples of 90 (including negative and > 360) still rotate
losslessly, 0 changes nothing, and a non-finite angle is rejected.

On a second image, rotated 80 degrees (narrower than it was, so the canvas
shrinks in one direction) with GIMP's transform direction set to backward:
- a selection and a channel at the left edge survive and turn clockwise;
- a layer with locked position inside a group is refused, with the image
  unchanged;
- a rotation that fails part way (a layer or the selection mask, stubbed)
  restores the selection exactly, keeps the selected layer and leaves no
  extra channel.

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


def image_index():
    """Return the current index of the test image in Gimp.get_images()."""
    return int(py("print(next(i for i, im in enumerate(Gimp.get_images()) "
                  "if im.get_id() == _rot_img.get_id()))"))


def rotate(angle):
    """Rotate the test image with a context feather set, which must not reach the selection."""
    py("_rot_feather = (Gimp.context_get_feather(), Gimp.context_get_feather_radius()[1:])",
       "Gimp.context_set_feather(True)",
       "Gimp.context_set_feather_radius(20, 20)")
    try:
        return cmd('rotate_image', {'angle': angle, 'image_index': image_index()})
    finally:
        py("Gimp.context_set_feather(_rot_feather[0])",
           "Gimp.context_set_feather_radius(*_rot_feather[1])")


def selected():
    """Return the names of the test image's selected layers and paths."""
    return json.loads(py("print(json.dumps([[l.get_name() for l in _rot_img.get_selected_layers()],"
                         " [p.get_name() for p in _rot_img.get_selected_paths()]]))"))


def mask_row(y, x1, x2):
    """Return the selection mask's values on row y, from x1 up to x2, rounded."""
    return json.loads(py(f"print(json.dumps([round(_rot_img.get_selection().get_pixel(x, {y})"
                         f".get_rgba()[0], 3) for x in range({x1}, {x2})]))"))


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


def selection_bounds():
    """Return (non_empty, x1, y1, x2, y2) of the test image's selection."""
    return json.loads(py("_b = Gimp.Selection.bounds(_rot_img)",
                         "print(json.dumps([_b[1], _b[2], _b[3], _b[4], _b[5]]))"))


def second_image():
    """80 degrees: one canvas side shrinks. Also locks and a failed rotation."""
    W, H = 200, 100
    py("_rot_img.delete()",
       f"_rot_img = Gimp.Image.new({W}, {H}, Gimp.ImageBaseType.RGB)",
       f"_l = Gimp.Layer.new(_rot_img, 'bg', {W}, {H}, Gimp.ImageType.RGB_IMAGE,"
       " 100, Gimp.LayerMode.NORMAL)",
       "_rot_img.insert_layer(_l, None, 0)",
       "_l.fill(Gimp.FillType.WHITE)",
       "_c = Gimp.Channel.new(_rot_img, 'mask', 200, 100, 50.0, Gegl.Color.new('#00ff00'))",
       "_rot_img.insert_channel(_c, None, 0)",
       # A 20x20 square at (0, 40), centre (10, 50), in the channel and the selection.
       crisp_rect("_rot_img", 0, 40, 20, 20),
       "Gimp.Drawable.edit_fill(_c, Gimp.FillType.WHITE)",
       "Gimp.context_set_transform_direction(Gimp.TransformDirection.BACKWARD)")

    r = rotate(80)
    check(r.get('status') == 'success', "rotate_image(80) failed", r)
    rad = math.radians(80)
    new_w = math.ceil(W * abs(math.cos(rad)) + H * abs(math.sin(rad)) - 1e-6)
    new_h = math.ceil(W * abs(math.sin(rad)) + H * abs(math.cos(rad)) - 1e-6)
    check(size() == [new_w, new_h], f"expected a {new_w}x{new_h} canvas", size())
    want = rotated(10, 50, 80, W, H)
    b = selection_bounds()
    check(b[0], "the selection at the left edge was lost")
    check(near(((b[1] + b[3]) / 2, (b[2] + b[4]) / 2), want),
          "the selection did not turn clockwise about the centre", f"{b}, want centre {want}")
    st = state()
    check(st['channels'] == ['mask'], "a channel was lost or left behind", st)
    # select_item uses the context's feather too: read the channel back crisp.
    b = json.loads(py("Gimp.context_push()\ntry:\n    Gimp.context_set_feather(False)\n"
                      "    _rot_img.select_item(Gimp.ChannelOps.REPLACE, _c)\n"
                      "finally:\n    Gimp.context_pop()",
                      "_b = Gimp.Selection.bounds(_rot_img)",
                      "print(json.dumps([_b[1], _b[2], _b[3], _b[4], _b[5]]))"))
    check(b[0] and near(((b[1] + b[3]) / 2, (b[2] + b[4]) / 2), want),
          "the channel's content was lost or misplaced", f"{b}, want centre {want}")
    print("PASS: 80 degrees keeps a selection and a channel at the edge, clockwise "
          "despite a backward transform direction")

    # A locked layer inside a group: refused before anything changes.
    py("_g = Gimp.GroupLayer.new(_rot_img, 'grp')",
       "_rot_img.insert_layer(_g, None, 0)",
       "_k = Gimp.Layer.new(_rot_img, 'locked', 10, 10, Gimp.ImageType.RGBA_IMAGE,"
       " 100, Gimp.LayerMode.NORMAL)",
       "_rot_img.insert_layer(_k, _g, 0)",
       "_k.set_lock_position(True)",
       crisp_rect("_rot_img", 5, 5, 10, 10),
       "_rot_img.clean_all()")
    before = (size(), state(), selection_bounds())
    r = rotate(30)
    check(r.get('status') == 'error' and "'locked' has its position and size locked"
          in r.get('error', ''), "a locked layer did not refuse the rotation", r)
    check((size(), state(), selection_bounds()) == before,
          "the refused rotation changed the image", (size(), state(), selection_bounds()))
    check(py("print(_rot_img.is_dirty())") == "False", "the refused rotation dirtied the image")
    py("_k.set_lock_position(False)")
    print("PASS: a locked layer in a group is refused, the image unchanged")

    # A rotation that fails part way restores the selection exactly (shifted by
    # the grown canvas), keeps the selected layer and drops the saved channel:
    # whether a layer fails to turn, or the selection mask itself.
    py("_rot_img.set_selected_layers([_l])")
    stubs = (('Gimp.Layer', "None if self.get_name() == 'bg' else _rot_orig_tr(self, *a)",
              "Rotating 'bg'"),
             ('Gimp.Selection', "None", "Rotating the selection"))
    for cls, stub, error in stubs:
        sel_before = selected()
        size_before = size()
        b_before = selection_bounds()
        row_before = mask_row(b_before[2] + 5, b_before[1] - 3, b_before[3] + 3)
        py(f"_rot_stub_cls = {cls}",
           "_rot_orig_tr = _rot_stub_cls.transform_rotate",
           f"_rot_stub_cls.transform_rotate = lambda self, *a: {stub}")
        try:
            r = rotate(30)
        finally:
            py("_rot_stub_cls.transform_rotate = _rot_orig_tr")
        check(r.get('status') == 'error' and error in r.get('error', ''),
              f"the stubbed {cls} failure did not fail the rotation", r)
        dx, dy = [(a - b) // 2 for a, b in zip(size(), size_before)]
        b = selection_bounds()
        check(b == [True, b_before[1] + dx, b_before[2] + dy, b_before[3] + dx, b_before[4] + dy],
              f"the failed rotation ({cls}) did not restore the selection",
              f"{b}, was {b_before} + {(dx, dy)}")
        row = mask_row(b_before[2] + 5 + dy, b_before[1] - 3 + dx, b_before[3] + 3 + dx)
        check(row == row_before, f"the failed rotation ({cls}) changed the selection mask",
              (row, row_before))
        check(selected() == sel_before, f"the failed rotation ({cls}) changed the selected layer",
              (selected(), sel_before))
        check(state()['channels'] == ['mask'],
              f"the failed rotation ({cls}) left a channel behind", state())
    print("PASS: a failed rotation (of a layer or of the selection) restores the selection "
          "exactly, keeps the selected layer and leaves no extra channel")

def main():
    made_image = False
    # second_image() sets BACKWARD; cleanup puts back what the context had.
    py("_rot_direction = Gimp.context_get_transform_direction()")
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
           crisp_rect("_rot_img", 0, 0, 20, 20),
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
        py("_rot_img.set_selected_layers([next(l for l in _rot_img.get_layers()"
           " if l.get_name() == 'red')])")
        sel_before = selected()
        check(sel_before == [['red'], ['path']], "setup: wrong selected layer or path", sel_before)
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
        # A 20x20 square turned 30 degrees spans 10 * (cos 30 + sin 30) either side of
        # its centre; a feathered one would reach about 10 px further, with a soft centre.
        half = 10 * (math.cos(math.radians(30)) + 0.5)
        sx, sy = rotated(10, 10, 30, W, H)
        check(near(b[1:], (sx - half, sy - half, sx + half, sy + half)),
              "the selection's extent changed (feathered?)", f"{b}, centre {(sx, sy)}")
        row = mask_row(int(sy), b[1], b[3])
        check(max(row) == 1.0 and len(set(row)) <= 6,
              "the rotated selection is not crisp (feathered?)", row)
        print("PASS: layers, a group's child, the path and the selection turn clockwise")
        check(selected() == sel_before, "the selected layer or path changed", selected())
        print("PASS: the selected layer and path are kept, and the selection is not feathered")

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

        # A stroke right after the rotation needs a selected layer.
        r = cmd('draw_rectangle', {'image_index': image_index(), 'x': 1, 'y': 1,
                                   'width': 10, 'height': 10, 'color': '#000000'})
        check(r.get('status') == 'success', "draw_rectangle after the rotation failed", r)
        print("PASS: draw_rectangle works after the rotation")

        # Multiples of 90 rotate losslessly, whatever their sign.
        for angle, want in ((-90, [new_h, new_w]), (90, [new_w, new_h]), (450, [new_h, new_w]),
                            (-180, [new_h, new_w])):
            r = rotate(angle)
            check(r.get('status') == 'success', f"rotate_image({angle}) failed", r)
            check(size() == want, f"rotate_image({angle}) gave the wrong size", size())
            res = r.get('results', {})
            check([res.get('width'), res.get('height')] == want,
                  f"rotate_image({angle}) reported the wrong size", res)
        print("PASS: -90, 90, 450 and -180 rotate by right angles")

        for bad in ('nan', 'inf'):
            r = rotate(float(bad))
            check(r.get('status') == 'error' and 'finite' in r.get('error', ''),
                  f"angle {bad} was not rejected", r)
        print("PASS: non-finite angles are rejected")

        second_image()
        return 0
    finally:
        try:
            py("Gimp.context_set_transform_direction(_rot_direction)",
               "if '_rot_orig_tr' in globals(): _rot_stub_cls.transform_rotate = _rot_orig_tr")
        except (SystemExit, OSError):
            print("  (cleanup) could not restore the transform direction or "
                  "transform_rotate: restart the MCP server in GIMP", file=sys.stderr)
        if made_image:
            try:
                py("_rot_img.delete()")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
