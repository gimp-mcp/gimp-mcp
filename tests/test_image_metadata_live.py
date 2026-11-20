#!/usr/bin/env python3
"""Live test for get_image_metadata's layer and resolution fields (#63).

They used to be wrong: is_group was true for every layer, layer_type was the
repr of a bound method, resolution_x/y were null (get_resolution() returns
(ok, x, y)), and a channel's color was a Gegl.Color repr. Checks them on an
image with a plain RGB layer, an RGBA layer, a layer group and a channel.

get_image_metadata reads Gimp.get_images()[0], the newest image, so the test
image must be the newest one open.

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


def main():
    made_image = False
    try:
        py("import json",
           "from gi.repository import Gegl",
           "_md_img = Gimp.Image.new(40, 30, Gimp.ImageBaseType.RGB)")
        # Owned from here on, so cleanup also runs if the rest of the setup fails.
        made_image = True
        py("_md_img.set_resolution(150.0, 200.0)",
           "_l = Gimp.Layer.new(_md_img, 'plain', 40, 30, Gimp.ImageType.RGB_IMAGE,"
           " 100, Gimp.LayerMode.NORMAL)",
           "_md_img.insert_layer(_l, None, 0)",
           "_l = Gimp.Layer.new(_md_img, 'alpha', 10, 10, Gimp.ImageType.RGBA_IMAGE,"
           " 100, Gimp.LayerMode.NORMAL)",
           "_md_img.insert_layer(_l, None, 0)",
           "_g = Gimp.GroupLayer.new(_md_img, 'grp')",
           "_md_img.insert_layer(_g, None, 0)",
           "_c = Gimp.Channel.new(_md_img, 'mask', 40, 30, 50.0, Gegl.Color.new('#ff8000'))",
           "_md_img.insert_channel(_c, None, 0)")
        check(py("print(Gimp.get_images()[0].get_id() == _md_img.get_id())") == "True",
              "the test image is not Gimp.get_images()[0]")

        r = cmd('get_image_metadata')
        check(r.get('status') == 'success', "get_image_metadata failed", r)
        md = r['results']
        basic = md['basic']
        check((basic['resolution_x'], basic['resolution_y']) == (150.0, 200.0),
              "wrong resolution", basic)
        print("PASS: resolution_x/y are reported")

        layers = {ly['name']: ly for ly in md['structure']['layers']}
        got = {n: (ly['is_group'], ly['layer_type']) for n, ly in layers.items()}
        want = {'grp': (True, 'RGBA'), 'alpha': (False, 'RGBA'), 'plain': (False, 'RGB')}
        check(got == want, "wrong is_group/layer_type", got)
        print("PASS: is_group and layer_type are right")

        channels = md['structure']['channels']
        check([c['color'] for c in channels] == ['#ff8000'], "wrong channel color", channels)
        print("PASS: a channel's color is reported as hex")
        return 0
    finally:
        if made_image:
            try:
                py("_md_img.delete()")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
