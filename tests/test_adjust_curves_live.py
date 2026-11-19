#!/usr/bin/env python3
"""Live test for adjust_curves' custom points (#63).

points went to GIMP unchecked: a NaN value crashed GIMP, values outside 0-255
were clipped (so [300, 255] made a curve that did nothing, reported as
success), an odd count or a single point failed with GIMP's bare "execution
error" after the undo group had marked the image changed, a non-number failed
with a Python error, [input, output, extra] triples were read as other pairs,
and of two points with the same input the last one silently won. Checks that:
- each of those is an error saying what's wrong, and leaves the pixels and
  the dirty flag alone;
- valid points apply the curve: as pairs or as a flat list, unsorted, with
  fractional values, with only two points; presets still work;
- a channel the layer doesn't have (alpha without an alpha channel, red,
  green or blue on a grayscale layer) and an indexed layer are errors that
  change nothing (GIMP refused inside the undo group, or for indexed changed
  nothing and reported success); alpha on a GRAYA layer works.

The NaN case runs after the out-of-range ones, so an older plugin fails the
test before it gets the NaN (which would crash GIMP).

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


HELPERS = r'''
from gi.repository import Gegl

def _ac_fill(v):
    """Make every pixel of the test layer gray v, and the image clean."""
    _ac_layer.get_buffer().set(Gegl.Rectangle.new(0, 0, 4, 4), "R'G'B' u8", bytes([v]) * 48)
    _ac_img.clean_all()

def _ac_value():
    """The red value of pixel (0, 0), and whether the image is dirty."""
    b = _ac_layer.get_buffer().get(Gegl.Rectangle.new(0, 0, 1, 1), 1.0, "R'G'B' u8",
                                   Gegl.AbyssPolicy.NONE)
    return [b[0], _ac_img.is_dirty()]
'''


def image_index():
    """Index of the test image in Gimp.get_images(), as the tools count it."""
    return int(py("print(next(i for i, im in enumerate(Gimp.get_images()) "
                  "if im.get_id() == _ac_img.get_id()))"))


def curves(**params):
    return cmd('adjust_curves', dict(image_index=image_index(), **params))


def value():
    return json.loads(py("print(__import__('json').dumps(_ac_value()))"))


def main():
    made_image = False
    try:
        py(HELPERS,
           "_ac_img = Gimp.Image.new(4, 4, Gimp.ImageBaseType.RGB)",
           "_ac_layer = Gimp.Layer.new(_ac_img, 'bg', 4, 4, Gimp.ImageType.RGB_IMAGE,"
           " 100, Gimp.LayerMode.NORMAL)",
           "_ac_img.insert_layer(_ac_layer, None, 0)")
        made_image = True
        py("_ac_fill(128)")
        check(value() == [128, False], "test setup: the layer is not clean gray 128", value())

        bad_inputs = [
            ([[0, 0], [300, 255], [255, 255]], '0-255'),
            ([[0, -10], [255, 255]], '0-255'),
            ([0, 0, 128, 200, 255], 'even number'),
            ([[0, 0], [128], [255, 255]], 'not an [input, output] pair'),
            ([[0, 0, 0], [255, 255, 255]], 'not an [input, output] pair'),
            ([[0, 0], 5, 6, [255, 255]], 'not a mix'),
            ([[128, 200]], '2-1024 points'),
            ([], '2-1024 points'),
            ([[i % 256, 0] for i in range(1025)], '2-1024 points'),
            ([[0, 0], ['a', 5], [255, 255]], 'numbers 0-255'),
            ([[0, 0], [True, 5], [255, 255]], 'numbers 0-255'),
            ('abc', 'list of [input, output] pairs'),
            ({'a': 1}, 'list of [input, output] pairs'),
            ([[0, 0], [128, 50], [128, 200], [255, 255]], 'same input 128'),
            # Last: these crashed GIMP before points were checked.
            ([[0, 0], [float('nan'), 5], [255, 255]], 'numbers 0-255'),
            ([[0, 0], [128, float('inf')], [255, 255]], 'numbers 0-255'),
        ]
        for points, words in bad_inputs:
            r = curves(points=points)
            shown = str(points)[:60]
            check(r.get('status') == 'error' and words in r.get('error', ''),
                  f"points={shown} did not fail with '{words}'", r)
            check(value() == [128, False], f"points={shown} changed the image", value())
        print("PASS: bad points are errors that leave the pixels and the dirty flag alone")

        for points in ([[0, 0], [128, 200], [255, 255]],
                       [[255, 255], [0, 0], [128, 200]],
                       [0, 0, 128, 200, 255, 255],
                       [[0.0, 0], [128, 200.0], [255, 255]]):
            py("_ac_fill(128)")
            r = curves(points=points)
            check(r.get('status') == 'success', f"points={points} failed", r)
            v = value()
            check(abs(v[0] - 200) <= 1 and v[1], f"points={points} did not map 128 to 200", v)
        py("_ac_fill(100)")
        r = curves(points=[[0, 255], [255, 0]])
        check(r.get('status') == 'success' and abs(value()[0] - 155) <= 1,
              "two points [[0, 255], [255, 0]] did not invert", (r, value()))
        print("PASS: pairs, a flat list, unsorted and fractional points, two points work")

        py("_ac_fill(128)")
        r = curves(preset='lighten')
        check(r.get('status') == 'success' and value()[0] > 150,
              "preset 'lighten' did not lighten", (r, value()))
        print("PASS: presets still work")

        # A channel the layer doesn't have, or an indexed layer: GIMP refused
        # inside the undo group ("execution error", image marked changed), or
        # for indexed reported success and changed nothing.
        py("_ac_fill(128)")
        r = curves(preset='lighten', channel='alpha')
        check(r.get('status') == 'error' and 'no alpha channel' in r.get('error', ''),
              "channel 'alpha' on a layer without alpha did not fail", r)
        check(value() == [128, False], "channel 'alpha' without alpha changed the image", value())
        for kind, conversion, channel, words in (
                ('GRAY', "_ac_img.convert_grayscale()", 'red', "grayscale and has no 'red'"),
                ('GRAYA', "_ac_img.convert_grayscale()", 'blue', "grayscale and has no 'blue'"),
                ('INDEXED', "_ac_img.convert_indexed(Gimp.ConvertDitherType.NONE, "
                 "Gimp.ConvertPaletteType.GENERATE, 16, False, False, '')", 'value', 'indexed')):
            py("_ac_img.undo_disable()", conversion,
               "_ac_layer.add_alpha()" if kind == 'GRAYA' else "pass",
               "_ac_img.undo_enable()", "_ac_img.clean_all()")
            before = value()
            r = curves(preset='lighten', channel=channel)
            check(r.get('status') == 'error' and words in r.get('error', ''),
                  f"channel {channel!r} on a {kind} layer did not fail with {words!r}", r)
            check(value() == before and not before[1],
                  f"channel {channel!r} on a {kind} layer changed the image", (before, value()))
            if kind == 'GRAYA':
                r = curves(preset='lighten', channel='alpha')
                check(r.get('status') == 'success', "channel 'alpha' on a GRAYA layer failed", r)
            py("_ac_img.undo_disable()", "_ac_img.convert_rgb()", "_ac_img.undo_enable()")
        print("PASS: a channel the layer doesn't have, or an indexed layer, is an error that"
              " changes nothing")
        return 0
    finally:
        if made_image:
            try:
                py("_ac_img.delete()")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
