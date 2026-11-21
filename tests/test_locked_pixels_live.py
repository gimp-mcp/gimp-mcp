#!/usr/bin/env python3
"""Live test for tools that change a layer's pixels: locked pixels and layer groups.

GIMP won't change a layer group's pixels, or a layer's whose pixels are
locked by its own "Lock pixels" or by a group it is in. The tools found out
only once GIMP refused, inside their undo group: the call failed with GIMP's
message ("cannot be modified", or no reason at all) and left the image marked
as changed. The GEGL filter tools (sharpen, blur, denoise, apply_gaussian_blur,
apply_pixelate, apply_emboss, apply_vignette, apply_noise) were worse: GIMP's
merge_filter() ignores the lock, so they changed locked pixels and reported
success, and on a group they changed nothing and reported success.
convert_color_mode RGBA/GRAYA converted the image, then failed to add alpha to
a locked layer ("Adding alpha to layer 'X' failed: success"). Checks that:
- every tool below refuses a layer with locked pixels, a layer in a group
  with locked pixels, and a layer group, with an error saying which, and
  changes nothing: pixels, dirty flag, layers;
- apply_drop_shadow refuses a layer group, and still works on a locked layer
  (it only reads it);
- convert_color_mode RGBA refuses a locked layer that needs alpha (its own
  lock or its group's) before converting, and changes nothing;
- the tools still change a layer inside a group that isn't locked.

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
import hashlib

def _lk_hash(d):
    b = d.get_buffer()
    return hashlib.sha1(bytes(b.get(b.get_extent(), 1.0, "R'G'B'A u8",
                                    Gegl.AbyssPolicy.NONE))).hexdigest()

def _lk_fill(img, d):
    # Two colors with an edge, so blurs and sharpening change something.
    # Pushed context keeps earlier settings: paint opaque, crisp, normal mode.
    Gimp.context_push()
    try:
        Gimp.context_set_opacity(100.0)
        Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)
        Gimp.context_set_feather(False)
        Gimp.context_set_foreground(Gegl.Color.new("#806040"))
        if not d.edit_fill(Gimp.FillType.FOREGROUND):
            raise RuntimeError("test setup: filling the test layer failed")
        Gimp.context_set_foreground(Gegl.Color.new("#ffffff"))
        img.select_rectangle(Gimp.ChannelOps.REPLACE, 16, 16, 32, 32)
        if not d.edit_fill(Gimp.FillType.FOREGROUND):
            raise RuntimeError("test setup: filling the test layer failed")
        Gimp.Selection.none(img)
    finally:
        Gimp.context_pop()

def _lk_make(case, base='RGB', alpha=True):
    """A 64x64 image whose selected layer is: 'own' a layer with locked pixels,
    'parent' a layer in a group with locked pixels, 'group' a group,
    'free' a layer in a group without locks."""
    global _LK
    img = Gimp.Image.new(64, 64, getattr(Gimp.ImageBaseType, base))
    kind = getattr(Gimp.ImageType, f"{base}{'A' if alpha else ''}_IMAGE")
    layer = Gimp.Layer.new(img, "L", 64, 64, kind, 100, Gimp.LayerMode.NORMAL)
    _LK = (img, layer)  # set now, so _lk_delete() also cleans up a failed setup
    if case == 'own':
        img.insert_layer(layer, None, 0)
        target = layer
    else:
        group = Gimp.GroupLayer.new(img, "G")
        img.insert_layer(group, None, 0)
        img.insert_layer(layer, group, 0)
        target = group if case == 'group' else layer
    # Fill before locking: a lock, the group's too, makes the fill fail.
    _lk_fill(img, layer)
    if case == 'own':
        layer.set_lock_content(True)
    elif case == 'parent':
        group.set_lock_content(True)
    img.set_selected_layers([target])
    img.clean_all()

def _lk_index():
    print(Gimp.get_images().index(_LK[0]))

def _lk_state():
    img, layer = _LK
    print(json.dumps([img.is_dirty(), _lk_hash(layer),
                      [l.get_name() for l in img.get_layers()]]))

def _lk_delete():
    if '_LK' in globals() and _LK[0].is_valid():
        _LK[0].delete()
'''

RECT = {'x': 5, 'y': 5, 'width': 40, 'height': 40, 'color': '#ff0000'}
TOOLS = [
    ('auto_levels', {}), ('adjust_curves', {'preset': 'lighten'}),
    ('adjust_brightness_contrast', {'brightness': 40}),
    ('adjust_hue_saturation', {'saturation': -60}), ('adjust_color_balance', {'cyan_red': 60}),
    ('sharpen', {}), ('blur', {}), ('denoise', {}), ('desaturate', {}), ('invert_colors', {}),
    ('fill_layer', {'color': '#ff0000'}), ('fill_selection', {'color': '#ff0000'}),
    ('draw_line', {'x1': 0, 'y1': 0, 'x2': 60, 'y2': 60, 'color': '#ff0000'}),
    ('draw_rectangle', RECT), ('draw_ellipse', RECT), ('fill_rectangle', RECT),
    ('fill_ellipse', RECT), ('gradient_fill', {}), ('apply_gaussian_blur', {'radius': 4}),
    ('apply_pixelate', {}), ('apply_emboss', {}), ('apply_vignette', {}), ('apply_noise', {}),
    ('warp_region', {'vectors': [{'x': 30, 'y': 30, 'dx': 10, 'dy': 0}]}),
]
EXPECTED = {
    'own': "layer 'L' has locked pixels",
    'parent': "layer 'L' is in group 'G', which has locked pixels",
    'group': "'G' is a layer group",
}


def make(case, tool=None):
    py(f"_lk_make({case!r})")
    if tool == 'fill_selection':
        # fill_selection refuses an empty selection first.
        py("_LK[0].select_rectangle(Gimp.ChannelOps.REPLACE, 4, 4, 20, 20)",
           "_LK[0].clean_all()")
    return int(py("_lk_index()"))


def state():
    return json.loads(py("_lk_state()"))


def main():
    py("import json", HELPERS)
    try:
        for tool, params in TOOLS:
            for case, expected in EXPECTED.items():
                index = make(case, tool)
                before = state()
                r = cmd(tool, dict(params, image_index=index))
                check(r.get('status') == 'error' and expected in r.get('error', ''),
                      f"{tool} on case {case!r} did not fail with {expected!r}", r)
                check(state() == before, f"{tool} on case {case!r} changed the image",
                      (before, state()))
                py("_lk_delete()")
        print("PASS: every tool refuses locked pixels (own or a group's) and layer groups,"
              " and changes nothing")

        index = make('group')
        before = state()
        r = cmd('apply_drop_shadow', {'image_index': index})
        check(r.get('status') == 'error' and "'G' is a layer group" in r.get('error', ''),
              "apply_drop_shadow on a layer group did not fail", r)
        check(state() == before, "a refused drop shadow changed the image", (before, state()))
        py("_lk_delete()")
        index = make('own')
        before = state()
        r = cmd('apply_drop_shadow', {'image_index': index})
        after = state()
        check(r.get('status') == 'success' and len(after[2]) == 2 and after[1] == before[1],
              "apply_drop_shadow on a locked layer did not add a shadow, or changed the layer",
              (r, before, after))
        py("_lk_delete()")
        print("PASS: apply_drop_shadow refuses a layer group, and shadows a locked layer")

        for start, case in (('GRAY', 'own'), ('GRAY', 'parent'), ('RGB', 'own')):
            py(f"_lk_make({case!r}, {start!r}, alpha=False)")
            index = int(py("_lk_index()"))
            before = state() + [py("print(_LK[0].get_base_type().value_nick)")]
            r = cmd('convert_color_mode', {'mode': 'RGBA', 'image_index': index})
            check(r.get('status') == 'error' and 'RGBA needs alpha' in r.get('error', '')
                  and EXPECTED[case] in r.get('error', ''),
                  f"convert_color_mode RGBA on a {start} image, case {case!r}, did not fail", r)
            after = state() + [py("print(_LK[0].get_base_type().value_nick)")]
            check(after == before and py("print(_LK[1].has_alpha())") == "False",
                  f"a refused convert_color_mode ({start}, {case!r}) changed the image",
                  (before, after))
            py("_lk_delete()")
        print("PASS: convert_color_mode RGBA refuses a locked layer without alpha before"
              " converting")

        for tool in ('invert_colors', 'apply_gaussian_blur', 'fill_rectangle'):
            index = make('free')
            before = state()
            r = cmd(tool, dict(dict(TOOLS)[tool], image_index=index))
            check(r.get('status') == 'success' and state()[1] != before[1],
                  f"{tool} on a layer in an unlocked group did not change it", r)
            py("_lk_delete()")
        print("PASS: a layer in a group without locks is still changed")
        return 0
    finally:
        try:
            py("_lk_delete()")
        except (SystemExit, OSError):
            print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
