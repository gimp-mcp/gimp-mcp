#!/usr/bin/env python3
"""Live test for edit_text on non-text layers and its reported layer name (#63).

On a layer that isn't a text layer, edit_text failed with GIMP's "could not
convert ... to type 'GimpTextLayer'" and marked the image changed (the
refusal came inside the undo group). And new text renames a text layer that
still has GIMP's automatic name (its text), which edit_text didn't report.
Checks that:
- edit_text on a plain layer or a layer group is an error naming the layer,
  also with no edits asked for, and leaves the image clean;
- edit_text returns layer_name, the layer's name after the edit: the new text
  for an automatically named layer, the given name for a renamed one.

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


def index():
    return int(py("print([i.get_id() for i in Gimp.get_images()].index(_ET.get_id()))"))


def names():
    return json.loads(py("print(__import__('json').dumps([l.get_name() for l in _ET.get_layers()]))"))


def dirty():
    return py("print(_ET.is_dirty())") == "True"


def main():
    made = False
    try:
        py("_ET = Gimp.Image.new(200, 100, Gimp.ImageBaseType.RGB)")
        made = True
        py("_ET.insert_layer(Gimp.Layer.new(_ET, 'plain', 200, 100, Gimp.ImageType.RGB_IMAGE, 100,"
           " Gimp.LayerMode.NORMAL), None, 0)",
           "_g = Gimp.GroupLayer.new(_ET, 'group')",
           "_ET.insert_layer(_g, None, 0)",
           "_ET.clean_all()")
        before = names()
        for layer, params in (('plain', {'text': 'hi'}),
                              ('plain', {'size': 20, 'color': 'red'}),
                              ('plain', {}),
                              ('group', {'text': 'hi'})):
            r = cmd('edit_text', {'layer_name': layer, **params, 'image_index': index()})
            check(r.get('status') == 'error'
                  and f"'{layer}' is not a text layer" in r.get('error', ''),
                  f"edit_text on {layer} with {params} was not refused clearly", r)
            check(not dirty(), f"edit_text on {layer} with {params} marked the image changed")
            check(names() == before, f"edit_text on {layer} changed the layers", names())
        print("PASS: edit_text on a layer that isn't a text layer is an error and changes nothing")

        r = cmd('add_text', {'text': 'Hello', 'x': 5, 'y': 5, 'image_index': index()})
        check(r.get('status') == 'success', "add_text failed", r)
        auto = r['results']['layer_name']
        r = cmd('edit_text', {'layer_name': auto, 'text': 'Goodbye', 'image_index': index()})
        check(r.get('status') == 'success', "edit_text on a text layer failed", r)
        check(names()[0] == 'Goodbye', "new text didn't rename the auto-named layer", names())
        check(r['results'].get('layer_name') == 'Goodbye',
              "edit_text didn't report the layer's new name", r['results'])

        r = cmd('rename_layer', {'old_name': 'Goodbye', 'new_name': 'Title', 'image_index': index()})
        check(r.get('status') == 'success', "rename_layer failed", r)
        for params in ({'text': 'Again'}, {'size': 30}, {}):
            r = cmd('edit_text', {'layer_name': 'Title', **params, 'image_index': index()})
            check(r.get('status') == 'success' and r['results'].get('layer_name') == 'Title',
                  f"edit_text with {params} on a renamed layer didn't report its name", r)
        check(names()[0] == 'Title', "new text renamed a renamed layer", names())
        check(py("print(_ET.get_layers()[0].get_text())") == 'Again', "the text didn't change")
        print("PASS: edit_text reports the layer's name after the edit")
        return 0
    finally:
        if made:
            try:
                py("_ET.delete() if _ET.is_valid() else None")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
