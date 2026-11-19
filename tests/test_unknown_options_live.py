#!/usr/bin/env python3
"""Live test for unknown option names (#63).

Tools used to replace an option name they didn't know with a default and go
on: operation="bogus" replaced the selection, an unknown channel or range
adjusted everything, gradient_type="bogus" drew a linear gradient,
new_canvas made an RGB image and reported color_mode "BOGUS", and
export_social_media_kit skipped unknown platforms. scale_image and
scale_to_fit also ignored interpolation altogether. Checks that:
- every option of every tool listed below rejects an unknown name with an
  error naming it, and changes nothing: pixels, size, selection, dirty flag,
  open images, files;
- option names are case-insensitive;
- scale_image's interpolation is used ("none" keeps hard edges, "linear"
  blends them).

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


HELPERS = r'''
from gi.repository import Gegl
import hashlib

def _uo_new(w, h, key):
    """A clean RGB image with a red square and a rectangle selection."""
    im = Gimp.Image.new(w, h, Gimp.ImageBaseType.RGB)
    ly = Gimp.Layer.new(im, 'bg', w, h, Gimp.ImageType.RGB_IMAGE, 100, Gimp.LayerMode.NORMAL)
    im.insert_layer(ly, None, 0)
    Gimp.context_push()
    try:
        Gimp.context_set_opacity(100.0)
        Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)
        Gimp.context_set_feather(False)
        Gimp.context_set_foreground(Gegl.Color.new('#3080c0'))
        ly.edit_fill(Gimp.FillType.FOREGROUND)
        im.select_rectangle(Gimp.ChannelOps.REPLACE, 4, 4, 12, 12)
        Gimp.context_set_foreground(Gegl.Color.new('#ff0000'))
        ly.edit_fill(Gimp.FillType.FOREGROUND)
        im.select_rectangle(Gimp.ChannelOps.REPLACE, 2, 3, 10, 8)
    finally:
        Gimp.context_pop()
    im.clean_all()
    _UO[key] = im
    return im

def _uo_state(key):
    """Everything a refused call must leave alone, as one comparable value."""
    im = _UO[key]
    ly = im.get_layers()[0]
    raw = bytes(ly.get_buffer().get(Gegl.Rectangle.new(0, 0, ly.get_width(), ly.get_height()),
                                    1.0, "R'G'B' u8", Gegl.AbyssPolicy.NONE))
    b = Gimp.Selection.bounds(im)
    return [im.get_width(), im.get_height(), len(im.get_layers()), hashlib.sha1(raw).hexdigest(),
            list(b[1:]), im.is_dirty(), len(Gimp.get_images())]

def _uo_index(key):
    return next(i for i, im in enumerate(Gimp.get_images()) if im.get_id() == _UO[key].get_id())
'''


def state(key='main'):
    return json.loads(py(f"print(__import__('json').dumps(_uo_state('{key}')))"))


def index(key='main'):
    return int(py(f"print(_uo_index('{key}'))"))


def main():
    out_dir = tempfile.mkdtemp(prefix='gimp-mcp-unknown-options-')
    made = False
    try:
        py(HELPERS, "_UO = {}")
        made = True
        py("_uo_new(32, 24, 'main')")
        before = state()

        def refused(tool, name, params, files=()):
            p = dict(params)
            p.setdefault('image_index', index())
            r = cmd(tool, p)
            check(r.get('status') == 'error'
                  and f"Unknown {name} " in r.get('error', '') and 'bogus' in r.get('error', ''),
                  f"{tool} with an unknown {name} did not fail naming it", r)
            check(state() == before, f"{tool} with an unknown {name} changed something",
                  (before, state()))
            for f in files:
                check(not os.path.exists(f), f"{tool} with an unknown {name} wrote {f}")

        bogus = 'bogus'
        for tool in ('select_rectangle', 'select_ellipse'):
            refused(tool, 'operation', {'x': 0, 'y': 0, 'width': 5, 'height': 5,
                                        'operation': bogus})
        refused('select_by_color', 'operation', {'color': '#ff0000', 'operation': bogus})
        refused('scale_image', 'interpolation', {'width': 10, 'height': 10,
                                                 'interpolation': bogus})
        refused('scale_to_fit', 'interpolation', {'max_width': 10, 'max_height': 10,
                                                  'interpolation': bogus})
        refused('resize_canvas', 'anchor', {'width': 40, 'height': 40, 'anchor': bogus})
        refused('flip_image', 'direction', {'direction': bogus})
        refused('desaturate', 'mode', {'mode': bogus})
        refused('adjust_hue_saturation', 'color_range', {'saturation': -50,
                                                         'color_range': bogus})
        refused('adjust_color_balance', 'range', {'cyan_red': 50, 'range': bogus})
        refused('adjust_curves', 'channel', {'preset': 'lighten', 'channel': bogus})
        refused('adjust_curves', 'preset', {'preset': bogus})
        refused('get_histogram', 'channel', {'channel': bogus})
        refused('gradient_fill', 'gradient_type', {'gradient_type': bogus})
        refused('draw_line', 'tool', {'x1': 0, 'y1': 0, 'x2': 20, 'y2': 20, 'tool': bogus})
        refused('new_canvas', 'color_mode', {'width': 8, 'height': 8, 'color_mode': bogus})
        icons = os.path.join(out_dir, 'icons')
        refused('export_icon_sizes', 'platform', {'output_dir': icons, 'platform': bogus,
                                                  'source_image_index': index()}, [icons])
        sheet = os.path.join(out_dir, 'sheet.png')
        refused('export_sprite_sheet', 'source', {'output_path': sheet, 'source': bogus},
                [sheet])
        social = os.path.join(out_dir, 'social')
        refused('export_social_media_kit', 'platform',
                {'output_dir': social, 'platforms': ['instagram_square', bogus]}, [social])
        print("PASS: every unknown option name is an error naming it, and changes nothing")

        r = cmd('select_rectangle', {'x': 20, 'y': 10, 'width': 4, 'height': 4,
                                     'operation': ' ADD ', 'image_index': index()})
        check(r.get('status') == 'success', "operation ' ADD ' was not accepted", r)
        sel = state()[4]
        check(sel == [True, 2, 3, 24, 14], "' ADD ' did not add to the selection", sel)
        r = cmd('flip_image', {'direction': 'Vertical', 'image_index': index()})
        check(r.get('status') == 'success' and r['results'].get('direction') == 'vertical',
              "direction 'Vertical' was not accepted", r)
        print("PASS: option names are case-insensitive and may have spaces around them")

        # A 2x2 black/white checker scaled to 8x8: "none" keeps only black and
        # white, "linear" blends them.
        values = {}
        for interp in ('none', 'linear'):
            py("_im = Gimp.Image.new(2, 2, Gimp.ImageBaseType.RGB)",
               "_ly = Gimp.Layer.new(_im, 'c', 2, 2, Gimp.ImageType.RGB_IMAGE, 100,"
               " Gimp.LayerMode.NORMAL)",
               "_im.insert_layer(_ly, None, 0)",
               "_ly.get_buffer().set(Gegl.Rectangle.new(0, 0, 2, 2), \"R'G'B' u8\","
               " bytes([0, 0, 0, 255, 255, 255, 255, 255, 255, 0, 0, 0]))",
               f"_UO['{interp}'] = _im")
            r = cmd('scale_image', {'width': 8, 'height': 8, 'interpolation': interp,
                                    'image_index': index(interp)})
            check(r.get('status') == 'success', f"scale_image interpolation={interp} failed", r)
            values[interp] = json.loads(py(
                f"_ly = _UO['{interp}'].get_layers()[0]",
                "_b = _ly.get_buffer().get(Gegl.Rectangle.new(0, 0, 8, 8), 1.0, \"R'G'B' u8\","
                " Gegl.AbyssPolicy.NONE)",
                "print(sorted(set(_b[0::3])))"))
        check(values['none'] == [0, 255], "interpolation 'none' blended the pixels", values)
        check(len(values['linear']) > 2, "interpolation 'linear' did not blend the pixels",
              values)
        print("PASS: scale_image uses its interpolation")
        return 0
    finally:
        if made:
            try:
                py("for _im in _UO.values():\n"
                   "    if _im.is_valid():\n"
                   "        _im.delete()")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test images", file=sys.stderr)
        shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
