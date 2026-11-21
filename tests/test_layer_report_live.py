#!/usr/bin/env python3
"""Live test for list_layers' and get_image_metadata's blend_mode and offsets (#63).

blend_mode was GIMP's enum number as a string ("28" for normal), and
list_layers' offsets were get_offsets()'s (ok, x, y) tuple ([true, 3, 4]).
Checks that:
- every blend mode create_layer takes comes back from list_layers and
  get_image_metadata under the name it was created with, and that name sets
  the same mode again through set_layer_properties;
- a mode the tools don't take is named after GIMP (ADDITION, NORMAL_LEGACY);
- offsets are [x, y].

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

MODES = ['NORMAL', 'MULTIPLY', 'SCREEN', 'OVERLAY', 'DARKEN', 'LIGHTEN', 'DODGE', 'BURN',
         'HARD_LIGHT', 'SOFT_LIGHT', 'DIFFERENCE', 'HUE', 'SATURATION', 'COLOR', 'LUMINOSITY',
         'DISSOLVE']


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


def index():
    return int(py("print([i.get_id() for i in Gimp.get_images()].index(_LR.get_id()))"))


def list_layers():
    r = cmd('list_layers', {'image_index': index()})
    check(r.get('status') == 'success', "list_layers failed", r)
    return r['results']['layers']


def main():
    made = False
    try:
        py("_LR = Gimp.Image.new(40, 30, Gimp.ImageBaseType.RGB)")
        made = True
        for name in MODES:
            r = cmd('create_layer', {'name': name, 'blend_mode': name, 'image_index': index()})
            check(r.get('status') == 'success', f"create_layer with blend_mode {name} failed", r)
        layers = list_layers()
        got = {layer['name']: layer.get('blend_mode') for layer in layers}
        check(got == {name: name for name in MODES},
              "list_layers doesn't report the blend modes by the names they were set with", got)

        check(py("print(Gimp.get_images()[0].get_id() == _LR.get_id())") == "True",
              "the test image is not Gimp.get_images()[0]")
        r = cmd('get_image_metadata')
        check(r.get('status') == 'success', "get_image_metadata failed", r)
        got = {layer['name']: layer.get('blend_mode') for layer in r['results']['structure']['layers']}
        check(got == {name: name for name in MODES},
              "get_image_metadata doesn't report the blend modes by name", got)
        print("PASS: list_layers and get_image_metadata name every blend mode create_layer takes")

        # Each reported name sets the same mode back (set it to NORMAL first, then the name).
        for layer in layers:
            for mode in ('NORMAL', layer['blend_mode']):
                r = cmd('set_layer_properties', {'layer_name': layer['name'], 'blend_mode': mode,
                                                 'image_index': index()})
                check(r.get('status') == 'success',
                      f"set_layer_properties with blend_mode {mode} failed", r)
        got = {layer['name']: layer.get('blend_mode') for layer in list_layers()}
        check(got == {name: name for name in MODES},
              "a reported blend mode name doesn't set the same mode again", got)
        print("PASS: the reported names set the same modes through set_layer_properties")

        py("for _n, _m in (('add', Gimp.LayerMode.ADDITION),"
           " ('legacy', Gimp.LayerMode.NORMAL_LEGACY)):\n"
           "    _LR.insert_layer(Gimp.Layer.new(_LR, _n, 10, 10, Gimp.ImageType.RGBA_IMAGE, 100,"
           " _m), None, 0)",
           "_LR.get_layers()[0].set_offsets(3, -4)")
        layers = list_layers()
        got = {layer['name']: layer.get('blend_mode') for layer in layers[:2]}
        check(got == {'legacy': 'NORMAL_LEGACY', 'add': 'ADDITION'},
              "modes the tools don't take are not named after GIMP", got)
        print("PASS: other modes are named after GIMP")

        check(layers[0].get('offsets') == [3, -4], "offsets of a moved layer are not [x, y]",
              layers[0].get('offsets'))
        check(all(layer.get('offsets') == [0, 0] for layer in layers[1:]),
              "offsets of unmoved layers are not [0, 0]", [layer.get('offsets') for layer in layers])
        print("PASS: list_layers reports offsets as [x, y]")
        return 0
    finally:
        if made:
            try:
                py("_LR.delete() if _LR.is_valid() else None")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
