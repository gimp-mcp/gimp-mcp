#!/usr/bin/env python3
"""Live test for get_histogram's luminance channel and documented results (#63).

GIMP 3.2 has a luminance histogram channel that get_histogram didn't offer.
Checks, on a layer that is pure blue on the left and white on the right, that:
- channel="luminance" works and gives a lower mean than "value" (which is
  the brightest of red, green and blue: 255 for pure blue);
- mean, median and std_dev are on a 0-255 scale;
- with a selection, only the selected pixels count.

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


def histogram(channel):
    index = int(py("print([i.get_id() for i in Gimp.get_images()].index(_HG.get_id()))"))
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
        return 0
    finally:
        if made:
            try:
                py("_HG.delete() if _HG.is_valid() else None")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
