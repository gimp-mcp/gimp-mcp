#!/usr/bin/env python3
"""Live test for get_histogram's luminance channel and documented results (#63).

GIMP 3.2 has a luminance histogram channel that get_histogram didn't offer.
Checks, on a layer that is pure blue on the left and white on the right, that:
- channel="luminance" works and gives a lower mean than "value" (which is
  the brightest of red, green and blue: 255 for pure blue);
- mean, median and std_dev are on a 0-255 scale;
- with a selection, only the selected pixels count;
- the 0-255 scale holds at every precision: GIMP itself gives 0-1 for
  anything but 8-bit, so a white 16-bit layer's mean was 1.0;
- a channel the layer doesn't have (alpha without an alpha channel; red,
  green, blue or luminance on a grayscale layer) is an error saying so; it
  failed with just "gimp-drawable-histogram did not succeed". The channels
  the layer has still work, also on an indexed layer.

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


def histogram(channel, var='_HG'):
    index = int(py(f"print([i.get_id() for i in Gimp.get_images()].index({var}.get_id()))"))
    r = send({'type': 'get_histogram', 'params': {'channel': channel, 'image_index': index}})
    check(r.get('status') == 'success', f"get_histogram channel={channel} failed", r)
    return r['results']


def main():
    made = False
    try:
        py("_HG = Gimp.Image.new(20, 10, Gimp.ImageBaseType.RGB)")
        made = True
        py("from gi.repository import Gegl",
           "_l = Gimp.Layer.new(_HG, 'bg', 20, 10, Gimp.ImageType.RGB_IMAGE, 100,"
           " Gimp.LayerMode.NORMAL)",
           "_HG.insert_layer(_l, None, 0)",
           "_l.fill(Gimp.FillType.WHITE)",
           "Gimp.context_push()\n"
           "try:\n"
           "    Gimp.context_set_feather(False)\n"
           "    Gimp.context_set_opacity(100.0)\n"
           "    Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)\n"
           "    Gimp.context_set_foreground(Gegl.Color.new('#0000ff'))\n"
           "    _HG.select_rectangle(Gimp.ChannelOps.REPLACE, 0, 0, 10, 10)\n"
           "    _l.edit_fill(Gimp.FillType.FOREGROUND)\n"
           "finally:\n"
           "    Gimp.context_pop()")

        # Left half (blue) selected.
        value, luminance = histogram('value'), histogram('luminance')
        check(value['pixels'] == 100 and luminance['pixels'] == 100,
              "with half the layer selected, not just its 100 pixels counted", (value, luminance))
        check(abs(value['mean'] - 255) < 0.5 and value['std_dev'] < 0.5,
              "pure blue's value mean isn't 255 on a 0-255 scale", value)
        check(0 < luminance['mean'] < 128,
              "pure blue's luminance isn't well below its value", luminance)
        r = send({'type': 'get_histogram', 'params': {'channel': 'Luminance ', 'image_index': int(
            py("print([i.get_id() for i in Gimp.get_images()].index(_HG.get_id()))"))}})
        check(r.get('status') == 'success', "channel names aren't case-insensitive", r)
        print("PASS: the luminance channel works, on a 0-255 scale, counting the selection")

        py("Gimp.Selection.none(_HG)")
        value = histogram('value')
        check(value['pixels'] == 200 and abs(value['mean'] - 255) < 0.5,
              "without a selection, not the whole layer counted", value)
        blue = luminance['mean']
        luminance = histogram('luminance')
        check(luminance['pixels'] == 200 and luminance['mean'] > blue,
              "the white half doesn't raise the luminance", (blue, luminance))
        print("PASS: without a selection the whole layer counts")

        for precision in ('U8_LINEAR', 'U8_PERCEPTUAL', 'U16_NON_LINEAR', 'HALF_LINEAR',
                          'FLOAT_NON_LINEAR', 'DOUBLE_PERCEPTUAL'):
            py(f"_HP = Gimp.Image.new_with_precision(10, 10, Gimp.ImageBaseType.RGB,"
               f" Gimp.Precision.{precision})")
            try:
                py("_p = Gimp.Layer.new(_HP, 'w', 10, 10, Gimp.ImageType.RGB_IMAGE, 100,"
                   " Gimp.LayerMode.NORMAL)",
                   "_HP.insert_layer(_p, None, 0)",
                   "_p.fill(Gimp.FillType.WHITE)")
                value = histogram('value', '_HP')
                check(abs(value['mean'] - 255) < 0.5 and abs(value['median'] - 255) < 1.5,
                      f"a white layer's mean at {precision} isn't 255", value)
                # Half black, half white: std_dev is 127.5, so its scaling is checked too.
                py("Gimp.context_push()\n"
                   "try:\n"
                   "    Gimp.context_set_feather(False)\n"
                   "    Gimp.context_set_opacity(100.0)\n"
                   "    Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)\n"
                   "    Gimp.context_set_foreground(Gegl.Color.new('#000000'))\n"
                   "    _HP.select_rectangle(Gimp.ChannelOps.REPLACE, 0, 0, 5, 10)\n"
                   "    _p.edit_fill(Gimp.FillType.FOREGROUND)\n"
                   "    Gimp.Selection.none(_HP)\n"
                   "finally:\n"
                   "    Gimp.context_pop()")
                value = histogram('value', '_HP')
                check(abs(value['mean'] - 127.5) < 1.5 and abs(value['std_dev'] - 127.5) < 1.5,
                      f"a half black, half white layer at {precision} doesn't have mean and"
                      " std_dev 127.5", value)
            finally:
                py("_HP.delete() if _HP.is_valid() else None")
        print("PASS: the values are on a 0-255 scale at every precision")

        for base, alpha, channel, words in (
                ('RGB', False, 'alpha', 'no alpha channel'),
                ('GRAY', False, 'red', "grayscale and has no 'red'"),
                ('GRAY', True, 'green', "grayscale and has no 'green'"),
                ('GRAY', False, 'luminance', "grayscale and has no 'luminance'"),
                ('GRAY', True, 'alpha', None), ('GRAY', False, 'value', None),
                ('INDEXED', False, 'luminance', None), ('INDEXED', True, 'alpha', None)):
            gimp_base = 'GRAY' if base == 'GRAY' else 'RGB'
            py(f"_HC = Gimp.Image.new(10, 10, Gimp.ImageBaseType.{gimp_base})")
            try:
                py(f"_c = Gimp.Layer.new(_HC, 'c', 10, 10, Gimp.ImageType.{gimp_base}"
                   f"{'A' if alpha else ''}_IMAGE, 100, Gimp.LayerMode.NORMAL)",
                   "_HC.insert_layer(_c, None, 0)",
                   "_c.fill(Gimp.FillType.WHITE)",
                   "_HC.convert_indexed(Gimp.ConvertDitherType.NONE, "
                   "Gimp.ConvertPaletteType.GENERATE, 16, False, False, '')"
                   if base == 'INDEXED' else "pass")
                what = f"channel {channel!r} on a {base}{'A' if alpha else ''} layer"
                if words is None:
                    result = histogram(channel, '_HC')
                    check(result['pixels'] == 100, f"{what} counted the wrong pixels", result)
                    continue
                index = int(py("print([i.get_id() for i in Gimp.get_images()]"
                               ".index(_HC.get_id()))"))
                r = send({'type': 'get_histogram',
                          'params': {'channel': channel, 'image_index': index}})
                check(r.get('status') == 'error' and words in r.get('error', ''),
                      f"{what} did not fail with {words!r}", r)
            finally:
                py("_HC.delete() if _HC.is_valid() else None")
        print("PASS: a channel the layer doesn't have is an error saying so; the others work")
        return 0
    finally:
        if made:
            try:
                py("_HG.delete() if _HG.is_valid() else None")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
