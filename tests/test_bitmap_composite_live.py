#!/usr/bin/env python3
"""Live regression test for issue #17: region snapshots show the visible composite.

get_image_bitmap with a region copied only the top layer, even a hidden one,
so region snapshots showed the wrong pixels. The test builds a 32x32 image
with an opaque red base layer, a layer that is blue in its top-left 16x16
square and transparent elsewhere, and a hidden opaque green layer on top. It
then opens a second, newer image, so the test image is not at index 0. A
region snapshot at (8, 8) of size 16x16 crosses the blue square's edge: it must
show blue and red where the full snapshot does, never green, and match the
same rectangle of a full snapshot. It also checks that the snapshot leaves the
image's selection as it was and leaves no temporary image open. Ported from
naveen42x's test in #66.

Requires the GIMP MCP plugin running on localhost:9877 (GIMP_MCP_PORT
overrides the port). The snapshot is written to a temporary directory; with a
Snap GIMP, which has a private /tmp, set TMPDIR to a directory GIMP can see
(e.g. ~/tmp). Run it directly, not with pytest. Exits 0 on pass, 1 on fail.
"""
import base64
import json
import os
import shutil
import socket
import sys
import tempfile

HOST, PORT = '127.0.0.1', int(os.environ.get('GIMP_MCP_PORT', 9877))
SIZE, CORNER = 32, 16
REGION = (8, 8, 16, 16)  # x, y, w, h: crosses the blue square's right and bottom edges
# Points in region coordinates and the colour each must have (image point = point + (8, 8))
EXPECTED = [((4, 4), 'blue'), ((12, 4), 'red'), ((4, 12), 'red'), ((12, 12), 'red')]
USER_SELECTION = (2, 3, 10, 8)  # x, y, w, h set on the image before the snapshot
# Run first inside each pushed context: the push keeps the user's settings,
# and a low paint opacity, another paint mode or feathering would change the
# fixture's pixels
RESET_CONTEXT = ("    Gimp.context_set_opacity(100.0)\n"
                 "    Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)\n"
                 "    Gimp.context_set_feather(False)\n")


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


def image_index(var):
    """Return the current index in Gimp.get_images() of the image held in `var`."""
    out = py(f"print(next(i for i, im in enumerate(Gimp.get_images()) "
             f"if im.get_id() == {var}.get_id()))")
    return int(out)


def image_count():
    return int(py("print(len(Gimp.get_images()))"))


def selection_bounds(var):
    return py(f"print(list(Gimp.Selection.bounds({var})))")


def sample(path, points):
    """Load `path` in GIMP once and return the RGBA (0-1, rounded) at each (x, y)."""
    out = py(
        "from gi.repository import Gio",
        f"_t17_load = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path({path!r}))",
        # One command: the plugin runs each command separately, so a
        # try/finally must not be split across them.
        "try:\n"
        "    _t17_px = _t17_load.get_layers()[0]\n"
        f"    print([[round(v, 3) for v in _t17_px.get_pixel(x, y).get_rgba()] for x, y in {points!r}])\n"
        "finally:\n"
        "    _t17_load.delete()",
    )
    return json.loads(out)


def check_primary(pixel, channel, where):
    """Fail unless an RGBA pixel is opaque and only `channel` is lit."""
    rgb = dict(zip(('red', 'green', 'blue'), pixel[:3]))
    lit = rgb[channel] > 0.9 and all(v < 0.1 for k, v in rgb.items() if k != channel)
    if not lit or pixel[3] < 0.9:
        fail(f"pixel at {where} is {pixel}, expected opaque {channel}")


def snapshot(tmpdir, name, index, width, height, **params):
    """Take a snapshot of image `index`, check its size, and write it to tmpdir/name."""
    r = cmd('get_image_bitmap', dict(params, image_index=index))
    if r.get('status') != 'success':
        fail(f"get_image_bitmap {params} failed: {r.get('error', r)}")
    res = r['results']
    if (res['width'], res['height']) != (width, height):
        fail(f"snapshot {params} is {res['width']}x{res['height']}, expected {width}x{height}")
    path = os.path.join(tmpdir, name)
    with open(path, 'wb') as f:
        f.write(base64.b64decode(res['image_data']))
    return path


def create_image():
    py("from gi.repository import Gimp, Gegl")
    py(
        f"_t17_img = Gimp.Image.new({SIZE}, {SIZE}, Gimp.ImageBaseType.RGB)",
        f"_t17_base = Gimp.Layer.new(_t17_img, 'Red base', {SIZE}, {SIZE}, "
        "Gimp.ImageType.RGB_IMAGE, 100, Gimp.LayerMode.NORMAL)",
        "_t17_img.insert_layer(_t17_base, None, 0)",
        f"_t17_top = Gimp.Layer.new(_t17_img, 'Blue corner', {SIZE}, {SIZE}, "
        "Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)",
        "_t17_img.insert_layer(_t17_top, None, 0)",
        "Gimp.context_push()\n"
        "try:\n"
        + RESET_CONTEXT +
        "    Gimp.context_set_foreground(Gegl.Color.new('#ff0000'))\n"
        "    Gimp.Drawable.edit_fill(_t17_base, Gimp.FillType.FOREGROUND)\n"
        "    Gimp.Drawable.edit_fill(_t17_top, Gimp.FillType.TRANSPARENT)\n"
        f"    _t17_img.select_rectangle(Gimp.ChannelOps.REPLACE, 0, 0, {CORNER}, {CORNER})\n"
        "    Gimp.context_set_foreground(Gegl.Color.new('#0000ff'))\n"
        "    Gimp.Drawable.edit_fill(_t17_top, Gimp.FillType.FOREGROUND)\n"
        "finally:\n"
        "    Gimp.context_pop()",
        # The case from #17: an opaque top layer that is hidden
        f"_t17_hidden = Gimp.Layer.new(_t17_img, 'Hidden green', {SIZE}, {SIZE}, "
        "Gimp.ImageType.RGB_IMAGE, 100, Gimp.LayerMode.NORMAL)",
        "_t17_img.insert_layer(_t17_hidden, None, 0)",
        "Gimp.context_push()\n"
        "try:\n"
        + RESET_CONTEXT +
        "    Gimp.context_set_foreground(Gegl.Color.new('#00ff00'))\n"
        "    Gimp.Selection.none(_t17_img)\n"
        "    Gimp.Drawable.edit_fill(_t17_hidden, Gimp.FillType.FOREGROUND)\n"
        "finally:\n"
        "    Gimp.context_pop()",
        "_t17_hidden.set_visible(False)",
        "_t17_img.select_rectangle(Gimp.ChannelOps.REPLACE, %d, %d, %d, %d)" % USER_SELECTION,
        # A newer image goes first in Gimp.get_images(), so the test image isn't index 0
        "_t17_other = Gimp.Image.new(8, 8, Gimp.ImageBaseType.RGB)",
    )


def check_region_composite(tmpdir):
    create_image()
    selection_before = selection_bounds('_t17_img')
    count_before = image_count()

    index = image_index('_t17_img')
    if index == 0:
        fail("the test image is at index 0, so image_index isn't exercised")
    x, y, w, h = REGION
    region_png = snapshot(tmpdir, 'region.png', index, w, h,
                          region={'origin_x': x, 'origin_y': y, 'width': w, 'height': h})
    full_png = snapshot(tmpdir, 'full.png', index, SIZE, SIZE)

    points = [p for p, _ in EXPECTED]
    in_region = sample(region_png, points)
    in_full = sample(full_png, [(px + x, py_ + y) for px, py_ in points])
    for (point, colour), pixel in zip(EXPECTED, in_region):
        check_primary(pixel, colour, f"region {point}")
    print("PASS: region snapshot shows the visible composite, not the hidden top layer")
    if in_region != in_full:
        fail(f"region pixels {in_region} differ from the full snapshot's {in_full}")
    print("PASS: region snapshot matches the same rectangle of a full snapshot")

    selection_after = selection_bounds('_t17_img')
    if selection_after != selection_before:
        fail(f"selection changed from {selection_before} to {selection_after}")
    print("PASS: snapshots left the image's selection unchanged")

    count_after = image_count()
    if count_after != count_before:
        fail(f"{count_after} images open after the snapshot, expected {count_before}")
    print("PASS: region snapshot left no temporary image open")


def main():
    tmpdir = tempfile.mkdtemp(prefix='gimp-mcp-17-')
    try:
        check_region_composite(tmpdir)
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        for var in ('_t17_img', '_t17_other', '_t17_load'):
            try:
                py(f"if '{var}' in globals() and {var}.is_valid(): {var}.delete()")
            except SystemExit:
                print(f"  (cleanup) could not delete {var}", file=sys.stderr)
        # The plugin's Python namespace is shared and persistent, so drop the
        # test's names too, not only the images they pointed at.
        try:
            py("[globals().pop(n) for n in [n for n in globals() if n.startswith('_t17_')]]")
        except SystemExit:
            print("  (cleanup) could not remove the test's _t17_* names", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
