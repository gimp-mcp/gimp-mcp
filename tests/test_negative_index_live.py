#!/usr/bin/env python3
"""Live test for bad image_index and layer_index values (#63).

Indexes went through int() and Python's indexing: image_index=-1 picked
another open image, and layer_index=-1 the bottom layer, so
rename_layer(layer_index=-1) renamed the bottom layer and reported success;
-0.5, 1.9 and true picked a nearby index the same way. Checks that:
- a negative, fractional or boolean layer_index is an error naming it, for
  every tool that takes one, also next to a layer name, and changes nothing;
- a negative image_index is an error naming it, and batch_export with one
  exports nothing (only tools that read are tried: on a plugin without the
  fix, -1 would act on some other open image);
- 0, the last valid index, a whole-number float and a digit string still work.

Requires the GIMP MCP plugin running on localhost:9877 (GIMP_MCP_PORT
overrides the port). Exits 0 on pass, 1 on fail.
"""
import json
import os
import shutil
import socket
import sys
import tempfile

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
    return int(py("print([i.get_id() for i in Gimp.get_images()].index(_NI.get_id()))"))


def state():
    """The test image's layers (name, opacity, visible) and dirty flag."""
    return json.loads(py(
        "print(__import__('json').dumps([[[l.get_name(), l.get_opacity(), l.get_visible()]"
        " for l in _NI.get_layers()], _NI.is_dirty()]))"))


def main():
    out_dir = tempfile.mkdtemp(prefix='gimp-mcp-negative-index-')
    made = False
    try:
        py("_NI = Gimp.Image.new(8, 8, Gimp.ImageBaseType.RGB)")
        made = True
        py("for _n in ('bottom', 'middle', 'top'):\n"
           "    _NI.insert_layer(Gimp.Layer.new(_NI, _n, 8, 8, Gimp.ImageType.RGB_IMAGE, 100,"
           " Gimp.LayerMode.NORMAL), None, 0)",
           "_NI.clean_all()")
        before = state()

        for tool, params in (('rename_layer', {'new_name': 'renamed'}),
                             ('delete_layer', {}),
                             ('set_layer_properties', {'opacity': 50}),
                             ('reorder_layer', {'new_position': 0})):
            for bad in (-1, -3, -0.5, 1.9, True, '1.5', 'x'):
                r = cmd(tool, {**params, 'layer_index': bad, 'image_index': index()})
                check(r.get('status') == 'error'
                      and f'layer_index must be a non-negative integer, got {bad!r}'
                      in r.get('error', ''),
                      f"{tool} with layer_index={bad!r} was not refused", r)
                check(state() == before, f"{tool} with layer_index={bad!r} changed something",
                      (before, state()))
        name_key = {'rename_layer': 'old_name'}
        for tool, params in (('rename_layer', {'new_name': 'renamed'}),
                             ('set_layer_properties', {'opacity': 50})):
            r = cmd(tool, {**params, name_key.get(tool, 'layer_name'): 'middle',
                           'layer_index': -1, 'image_index': index()})
            check(r.get('status') == 'error' and 'layer_index' in r.get('error', ''),
                  f"{tool} with a layer name ignored layer_index=-1", r)
            check(state() == before, f"{tool} with a name and layer_index=-1 changed something",
                  (before, state()))
        print("PASS: a negative, fractional or boolean layer_index is an error and changes"
              " nothing")

        for tool, params in (('list_layers', {}),
                             ('get_pixel_color', {'x': 0, 'y': 0}),
                             ('get_histogram', {}),
                             ('get_image_bitmap', {'max_width': 8})):
            r = cmd(tool, {**params, 'image_index': -1})
            check(r.get('status') == 'error' and 'image_index' in r.get('error', '')
                  and '-1' in r.get('error', ''),
                  f"{tool} with image_index=-1 was not refused", r)
        exports = os.path.join(out_dir, 'exports')
        r = cmd('batch_export', {'output_dir': exports, 'image_index': -1})
        check(r.get('status') == 'error'
              and 'image_index must be a non-negative integer, got -1' in r.get('error', ''),
              "batch_export with image_index=-1 was not refused", r)
        for bad in (-0.5, True):
            r = cmd('list_layers', {'image_index': bad})
            check(r.get('status') == 'error'
                  and f'image_index must be a non-negative integer, got {bad!r}'
                  in r.get('error', ''), f"list_layers with image_index={bad!r} was not refused", r)
        check(not os.path.exists(exports), "batch_export with image_index=-1 wrote files",
              os.listdir(exports) if os.path.exists(exports) else None)
        r = cmd('batch_export', {'output_dir': exports, 'image_index': 10 ** 6})
        check(r.get('status') == 'error'
              and 'image_index 1000000 out of range' in r.get('error', ''),
              "batch_export with an image_index past the end was not refused", r)
        check(state() == before, "a negative image_index changed the test image",
              (before, state()))
        print("PASS: a negative or fractional image_index is an error, and batch_export exports"
              " nothing")

        r = cmd('rename_layer', {'layer_index': 2, 'new_name': 'last', 'image_index': index()})
        check(r.get('status') == 'success', "layer_index 2 (the last) failed", r)
        r = cmd('rename_layer', {'layer_index': 0, 'new_name': 'first', 'image_index': index()})
        check(r.get('status') == 'success', "layer_index 0 failed", r)
        r = cmd('rename_layer', {'layer_index': 1.0, 'new_name': 'float', 'image_index': index()})
        check(r.get('status') == 'success', "layer_index 1.0 failed", r)
        r = cmd('set_layer_properties', {'layer_index': '1', 'opacity': 40,
                                         'image_index': str(index())})
        check(r.get('status') == 'success', "a digit-string layer_index / image_index failed", r)
        check([(n, round(o)) for n, o, _ in state()[0]] == [('first', 100), ('float', 40),
                                                            ('last', 100)],
              "valid indexes changed the wrong layers", state())
        r = cmd('rename_layer', {'layer_index': 3, 'new_name': 'x', 'image_index': index()})
        check(r.get('status') == 'error' and 'out of range' in r.get('error', ''),
              "layer_index past the end was not refused", r)
        r = cmd('batch_export', {'output_dir': exports, 'image_index': index()})
        check(r.get('status') == 'success' and r['results'].get('count') == 1,
              "batch_export of the test image by index failed", r)
        for good in (float(index()), str(index())):
            r = cmd('get_image_bitmap', {'max_width': 8, 'image_index': good})
            check(r.get('status') == 'success',
                  f"get_image_bitmap with image_index={good!r} failed", r)
        print("PASS: 0, the last index, whole-number floats and digit strings still work")
        return 0
    finally:
        if made:
            try:
                py("_NI.delete() if _NI.is_valid() else None")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)
        shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
