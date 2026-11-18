#!/usr/bin/env python3
import socket
import json
import os
import shutil
import sys
import tempfile

def cmd(t, params=None):
    s = socket.socket()
    s.settimeout(20)
    s.connect(('127.0.0.1', 9877))
    msg = {'type': t, 'params': params if params is not None else {}}
    s.send(json.dumps(msg).encode() + b'\n')
    r = b''
    while True:
        try:
            d = s.recv(8192)
            if not d:
                break
            r += d
            try:
                json.loads(r.decode())
                break
            except json.JSONDecodeError:
                continue
        except socket.timeout:
            break
    s.close()
    try:
        return json.loads(r.decode().strip())
    except json.JSONDecodeError:
        return {'status': 'error', 'error': 'parse error: ' + r.decode()[:80]}

passed = 0
failed = 0
failures = []

def t(name, r):
    global passed, failed
    ok = r.get('status') == 'success'
    if ok:
        passed += 1
    else:
        failed += 1
        failures.append((name, str(r.get('error', ''))[:90]))
    detail = str(r.get('results', '') if ok else r.get('error', ''))[:65]
    print(f"  {'PASS' if ok else 'FAIL'} {name}: {detail}")
    return r

def chk(name, ok, detail=''):
    """Assert an arbitrary boolean condition (beyond response status)."""
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
        failures.append((name, str(detail)[:90]))
    print(f"  {'PASS' if ok else 'FAIL'} {name}: {str(detail)[:65]}")
    return ok

# Setup
r = cmd('new_canvas', {'width': 200, 'height': 200, 'fill': 'white'})
img_id = r.get('image_id') or r.get('results', {}).get('image_id')
print(f"Setup: {r.get('status')} img_id={img_id}")
if r.get('status') != 'success':
    print(f"Setup failed: {r.get('error', '')}", file=sys.stderr)
    sys.exit(1)

print()
print("=== Cat 1: Info ===")
t('get_gimp_info',    cmd('get_gimp_info'))
t('list_images',      cmd('list_images', {}))
t('get_metadata',     cmd('get_image_metadata'))
t('list_layers',      cmd('list_layers', {'image_index': 0}))
t('pixel_color',      cmd('get_pixel_color', {'image_index': 0, 'x': 10, 'y': 10}))
_hist = t('get_histogram', cmd('get_histogram', {'image_index': 0, 'channel': 'value'}))
# Regression #47: the canvas is still a uniform white 200x200 fill here, so
# every statistic is known. Reading the PDB status slot as 'mean' (the
# off-by-one fixed in #48) gives mean=3 and shifts every other field.
_h = _hist.get('results', {})
_expected = {'mean': 255.0, 'std_dev': 0.0, 'median': 255.0, 'pixels': 40000.0, 'count': 40000.0}
chk('histogram_values',
    all(isinstance(_h.get(k), (int, float)) and abs(_h[k] - v) < 0.01 for k, v in _expected.items()),
    _h)
# A uniform fill has std_dev 0, the same as _safe()'s fallback, so also check
# a non-uniform image: columns 0-49 black, the rest white (1 in 4 black).
# mean = 0.75 * 255, std_dev = 255 * sqrt(0.25 * 0.75), median = 255.
cmd('fill_rectangle', {'image_index': 0, 'x': 0, 'y': 0, 'width': 50, 'height': 200, 'color': '#000000'})
_h = cmd('get_histogram', {'image_index': 0, 'channel': 'value'}).get('results', {})
_expected = {'mean': 191.25, 'std_dev': 110.4182, 'median': 255.0, 'pixels': 40000.0, 'count': 40000.0}
chk('histogram_values_mixed',
    all(isinstance(_h.get(k), (int, float)) and abs(_h[k] - v) < 0.01 for k, v in _expected.items()),
    _h)
cmd('fill_layer', {'image_index': 0, 'color': '#ffffff'})  # restore the white canvas for later tests
t('selection_bounds', cmd('get_selection_bounds', {'image_index': 0}))

print()
print("=== Cat 2: Adjustments ===")
t('auto_levels',   cmd('auto_levels', {'image_index': 0}))
t('adjust_curves', cmd('adjust_curves', {'image_index': 0, 'channel': 'value', 'points': [0,0,128,148,255,255]}))
t('brightness',    cmd('adjust_brightness_contrast', {'image_index': 0, 'brightness': 10, 'contrast': 5}))
t('hue_sat',       cmd('adjust_hue_saturation', {'image_index': 0, 'hue': 5, 'saturation': 10, 'lightness': 0}))
t('color_balance', cmd('adjust_color_balance', {'image_index': 0, 'cyan_red': 10, 'magenta_green': 0, 'yellow_blue': -10}))
t('sharpen',       cmd('sharpen',       {'image_index': 0, 'amount': 0.3}))
t('blur',          cmd('blur',          {'image_index': 0, 'radius_x': 1.0, 'radius_y': 1.0}))
t('denoise',       cmd('denoise',       {'image_index': 0}))
t('desaturate',    cmd('desaturate',    {'image_index': 0}))
t('invert',        cmd('invert_colors', {'image_index': 0}))

print()
print("=== Cat 3: Transform ===")
t('scale_image',   cmd('scale_image',   {'image_index': 0, 'width': 150, 'height': 150}))
t('scale_to_fit',  cmd('scale_to_fit',  {'image_index': 0, 'max_width': 120, 'max_height': 120}))
t('crop_to_rect',  cmd('crop_to_rect',  {'image_index': 0, 'x': 0, 'y': 0, 'width': 100, 'height': 100}))
t('rotate_image',  cmd('rotate_image',  {'image_index': 0, 'angle': 90}))
t('flip_image',    cmd('flip_image',    {'image_index': 0, 'direction': 'horizontal'}))
t('resize_canvas', cmd('resize_canvas', {'image_index': 0, 'width': 120, 'height': 120, 'offset_x': 0, 'offset_y': 0}))

print()
print("=== Cat 4: Selections ===")
t('select_rect',    cmd('select_rectangle', {'image_index': 0, 'x': 10, 'y': 10, 'width': 40, 'height': 40}))
t('select_ellipse', cmd('select_ellipse',   {'image_index': 0, 'x': 10, 'y': 10, 'width': 40, 'height': 40}))
# Regression #23: select_by_color must actually create a selection, not
# silently no-op while reporting success. Fill a known color and clear any
# prior selection first, so a leftover selection can't mask a no-op.
# Assert the setup itself succeeds so a broken fill can't skew the real check.
t('select_setup_fill', cmd('fill_layer',  {'image_index': 0, 'color': '#ffffff'}))
t('select_setup_none', cmd('select_none', {'image_index': 0}))
t('select_color',   cmd('select_by_color',  {'image_index': 0, 'color': '#ffffff'}))
_sel = cmd('get_selection_bounds', {'image_index': 0})
chk('select_color_nonempty', _sel.get('results', {}).get('has_selection') is True, _sel.get('results'))
t('select_all',     cmd('select_all',       {'image_index': 0}))
t('select_none',    cmd('select_none',      {'image_index': 0}))
t('invert_sel',     cmd('invert_selection', {'image_index': 0}))
t('modify_sel',     cmd('modify_selection', {'image_index': 0, 'operation': 'grow', 'amount': 3}))

print()
print("=== Cat 5: Layers ===")
t('create_layer',    cmd('create_layer',     {'image_index': 0, 'name': 'TLayer', 'width': 80, 'height': 80}))
t('duplicate_layer', cmd('duplicate_layer',  {'image_index': 0}))
t('list_layers2',    cmd('list_layers',      {'image_index': 0}))
t('rename_layer',    cmd('rename_layer',     {'image_index': 0, 'new_name': 'Renamed'}))
t('set_layer_props', cmd('set_layer_properties', {'image_index': 0, 'opacity': 80}))
t('reorder_layer',   cmd('reorder_layer',    {'image_index': 0, 'layer_name': 'Renamed', 'new_position': 0}))
t('delete_layer',    cmd('delete_layer',     {'image_index': 0, 'layer_name': 'Renamed'}))
t('merge_visible',   cmd('merge_visible_layers', {'image_index': 0}))
t('flatten_image',   cmd('flatten_image',    {'image_index': 0}))

print()
print("=== Cat 6: Drawing ===")
t('fill_layer',     cmd('fill_layer',     {'image_index': 0, 'color': '#ff0000'}))
t('set_colors',     cmd('set_colors',     {'foreground': '#000000', 'background': '#ffffff'}))
# fill_layer cleared the selection, so create one for fill_selection.
t('select_for_fill', cmd('select_rectangle', {'image_index': 0, 'x': 5, 'y': 5, 'width': 20, 'height': 20}))
t('fill_selection', cmd('fill_selection', {'image_index': 0, 'fill_type': 'foreground'}))
# Clear it again so the tools below draw on the whole layer instead of
# being clipped to the 20x20 square.
cmd('select_none', {'image_index': 0})
t('draw_line',      cmd('draw_line',      {'image_index': 0, 'x1': 0, 'y1': 0, 'x2': 50, 'y2': 50, 'color': '#000000', 'width': 2}))
t('draw_rect',      cmd('draw_rectangle', {'image_index': 0, 'x': 10, 'y': 10, 'width': 30, 'height': 30, 'color': '#0000ff', 'line_width': 2.0}))
t('draw_ellipse',   cmd('draw_ellipse',   {'image_index': 0, 'x': 10, 'y': 10, 'width': 30, 'height': 30, 'color': '#00ff00', 'line_width': 2.0}))
t('fill_rectangle', cmd('fill_rectangle', {'image_index': 0, 'x': 5, 'y': 5, 'width': 20, 'height': 20, 'color': '#ffff00'}))
t('fill_ellipse',   cmd('fill_ellipse',   {'image_index': 0, 'x': 5, 'y': 5, 'width': 20, 'height': 20, 'color': '#ff00ff'}))
t('gradient_fill',  cmd('gradient_fill',  {'image_index': 0, 'x1': 0, 'y1': 0, 'x2': 80, 'y2': 80}))

print()
print("=== Cat 7: Text ===")
t('add_text',   cmd('add_text',   {'image_index': 0, 'text': 'Hello', 'x': 10, 'y': 10, 'size': 12, 'color': '#000000'}))
t('list_fonts', cmd('list_fonts', {}))

print()
print("=== Cat 7b: Paint tools ignore a leftover context ===")
# context_push() copies the current context, so an opacity or paint mode left
# by an earlier call_api used to blend into every fill and stroke. Leave
# opacity at 50% and paint mode at DIFFERENCE (black on white would come out
# white), then check each tool still paints solid black.
_r = cmd('new_canvas', {'width': 200, 'height': 200, 'fill': 'white'})
if _r.get('status') != 'success':  # otherwise the checks below would paint over the previous image
    print(f"Cat 7b canvas setup failed: {_r.get('error', '')}", file=sys.stderr)
    sys.exit(1)
_BLACK = [0, 0, 0]
_WHITE = [255, 255, 255]


def _read_context():
    r = cmd('exec', {'args': ['python-fu-exec', [
        'print(Gimp.context_get_opacity(), Gimp.context_get_paint_mode().value_nick)',
    ]]})
    try:
        opacity, mode = r['results'][0].split()
        return float(opacity), mode
    except (KeyError, IndexError, ValueError):
        return None


def _context(opacity, mode):
    """Set opacity and paint mode (and a solid line), and confirm they took.

    If the setup silently failed, every check below would run in a plain
    context and pass without testing anything, so stop the suite instead.
    """
    r = cmd('exec', {'args': ['python-fu-exec', [
        f'Gimp.context_set_opacity({opacity})',
        f'Gimp.context_set_paint_mode(Gimp.LayerMode.{mode})',
        'Gimp.context_set_line_dash_pattern([])',  # a leftover dash could leave a gap at a sampled stroke pixel
    ]]})
    got = _read_context()
    if r.get('status') != 'success' or got != (opacity, mode.lower()):
        print(f"Context setup failed: wanted {(opacity, mode.lower())}, got {got}: {r.get('error', '')}",
              file=sys.stderr)
        sys.exit(1)


def _paint_check(name, tool, params, x, y, layer_name=None, select=None):
    _context(100.0, 'NORMAL')
    cmd('select_none', {'image_index': 0})
    cmd('fill_layer', {'image_index': 0, 'color': '#ffffff'})  # so a black pixel can only come from the tool
    q = {'image_index': 0, 'x': x, 'y': y}
    _before = cmd('get_pixel_color', q).get('results', {}).get('color_rgb')
    chk(name + '_starts_white', _before == _WHITE, _before)  # a failed reset could leave the last check's black
    if select:
        t(name + '_select', cmd('select_rectangle', dict(select, image_index=0)))
    _context(50.0, 'DIFFERENCE')
    t(name, cmd(tool, dict(params, image_index=0)))
    _after = _read_context()
    chk(name + '_restores_context', _after == (50.0, 'difference'), _after)
    _context(100.0, 'NORMAL')
    if layer_name:
        q['layer_name'] = layer_name
    p = cmd('get_pixel_color', q).get('results', {})
    chk(name + '_solid', p.get('color_rgb') == _BLACK and p.get('alpha') == 255, p)


_paint_check('ctx_fill_layer',     'fill_layer',     {'color': '#000000'}, 100, 100)
_paint_check('ctx_fill_rectangle', 'fill_rectangle', {'x': 50, 'y': 50, 'width': 100, 'height': 100, 'color': '#000000'}, 100, 100)
_paint_check('ctx_fill_ellipse',   'fill_ellipse',   {'x': 50, 'y': 50, 'width': 100, 'height': 100, 'color': '#000000'}, 100, 100)
_paint_check('ctx_fill_selection', 'fill_selection', {'fill_type': 'foreground', 'color': '#000000'}, 100, 100,
             select={'x': 50, 'y': 50, 'width': 100, 'height': 100})
cmd('select_none', {'image_index': 0})
_paint_check('ctx_draw_line',     'draw_line',      {'x1': 10, 'y1': 100, 'x2': 190, 'y2': 100, 'color': '#000000', 'width': 6}, 100, 100)
_paint_check('ctx_draw_rectangle', 'draw_rectangle', {'x': 50, 'y': 50, 'width': 100, 'height': 100, 'color': '#000000', 'line_width': 6.0}, 50, 100)
_paint_check('ctx_draw_ellipse',   'draw_ellipse',   {'x': 50, 'y': 50, 'width': 100, 'height': 100, 'color': '#000000', 'line_width': 6.0}, 50, 100)
_paint_check('ctx_create_layer',   'create_layer',   {'name': 'CtxLayer', 'fill': '#000000'}, 100, 100, layer_name='CtxLayer')
cmd('delete_layer', {'image_index': 0, 'layer_name': 'CtxLayer'})

print()
print("=== Cat 8: Filters ===")
t('gaussian_blur', cmd('apply_gaussian_blur', {'image_index': 0, 'radius': 2.0}))
t('pixelate',      cmd('apply_pixelate',      {'image_index': 0, 'block_size': 5}))
t('emboss',        cmd('apply_emboss',        {'image_index': 0}))
t('vignette',      cmd('apply_vignette',      {'image_index': 0}))
t('noise',         cmd('apply_noise',         {'image_index': 0}))
t('drop_shadow',   cmd('apply_drop_shadow',   {'image_index': 0}))

print()
print("=== Cat 9: Filters change pixels ===")
# The filter tools above only check status, and _apply_gegl_filter used to
# report success without applying anything. Each check here starts from a
# known image and asserts a change that only the filter itself can make.
cmd('new_canvas', {'width': 200, 'height': 200, 'fill': 'white'})


def px(x, y):
    return cmd('get_pixel_color', {'image_index': 0, 'x': x, 'y': y}).get('results', {}).get('color_rgb')


def std_dev():
    return cmd('get_histogram', {'image_index': 0, 'channel': 'value'}).get('results', {}).get('std_dev')


def half_black():
    """White canvas with columns 0-149 black."""
    cmd('select_none', {'image_index': 0})
    cmd('fill_layer', {'image_index': 0, 'color': '#ffffff'})
    cmd('fill_rectangle', {'image_index': 0, 'x': 0, 'y': 0, 'width': 150, 'height': 200, 'color': '#000000'})


# Pixelate: the 100px block over columns 100-199 is half black, half white.
half_black()
t('f_pixelate', cmd('apply_pixelate', {'image_index': 0, 'block_size': 100}))
_p = px(120, 50)
chk('f_pixelate_blends', _p is not None and 0 < _p[0] < 255, _p)

# Gaussian blur: 5px left of the black/white edge picks up some white.
half_black()
t('f_gaussian_blur', cmd('apply_gaussian_blur', {'image_index': 0, 'radius': 10.0}))
_p = px(145, 50)
chk('f_gaussian_blur_spreads', _p is not None and _p[0] > 0, _p)

# Emboss: a flat white area is shaded to a gray, not left white.
cmd('fill_layer', {'image_index': 0, 'color': '#ffffff'})
t('f_emboss', cmd('apply_emboss', {'image_index': 0}))
_p = px(100, 100)
chk('f_emboss_shades', _p is not None and _p[0] < 255, _p)

# Vignette: corners darken while the center stays white.
cmd('fill_layer', {'image_index': 0, 'color': '#ffffff'})
t('f_vignette', cmd('apply_vignette', {'image_index': 0}))
_c, _m = px(0, 0), px(100, 100)
chk('f_vignette_darkens_corner', _c is not None and _m is not None and _c[0] < _m[0], {'corner': _c, 'center': _m})

# Noise, then denoise: a flat gray gains variation, then loses some of it.
cmd('fill_layer', {'image_index': 0, 'color': '#808080'})
t('f_noise', cmd('apply_noise', {'image_index': 0, 'amount': 0.5}))
_noisy = std_dev()
chk('f_noise_adds_variation', isinstance(_noisy, (int, float)) and _noisy > 1.0, _noisy)
t('f_denoise', cmd('denoise', {'image_index': 0, 'strength': 100}))
_clean = std_dev()
chk('f_denoise_reduces_variation',
    isinstance(_clean, (int, float)) and isinstance(_noisy, (int, float)) and _clean < _noisy,
    {'before': _noisy, 'after': _clean})

# Bad arguments must now fail instead of reporting success.
_r = cmd('apply_vignette', {'image_index': 0, 'softness': 3.0})
chk('f_vignette_rejects_out_of_range', _r.get('status') == 'error', _r.get('error'))
_r = cmd('apply_vignette', {'image_index': 0, 'shape': 'hexagon'})
chk('f_vignette_rejects_bad_shape', _r.get('status') == 'error', _r.get('error'))

print()
print("=== Cat 10: Fill safety ===")
# With no selection, GIMP applies Edit > Clear / Fill to the ENTIRE layer.
# fill_selection used to report success while destroying the image; it must
# now refuse, leaving the layer untouched.
cmd('new_canvas', {'width': 64, 'height': 64, 'fill': '#ff0000'})
cmd('select_none', {'image_index': 0})
_r = cmd('fill_selection', {'image_index': 0, 'fill_type': 'transparent'})
chk('fill_selection_refuses_no_selection', _r.get('status') == 'error', _r.get('error'))
_r = cmd('fill_selection', {'image_index': 0, 'color': '#00ff00'})
chk('fill_selection_refuses_no_selection_color', _r.get('status') == 'error', _r.get('error'))
_p = cmd('get_pixel_color', {'image_index': 0, 'x': 32, 'y': 32}).get('results', {})
chk('fill_selection_no_selection_layer_intact',
    _p.get('color_rgb') == [255, 0, 0] and _p.get('alpha') == 255, _p)
# The guard must not block a real selection.
cmd('select_rectangle', {'image_index': 0, 'x': 10, 'y': 10, 'width': 10, 'height': 10})
_r = cmd('fill_selection', {'image_index': 0, 'fill_type': 'transparent'})
chk('fill_selection_with_selection_succeeds', _r.get('status') == 'success', _r.get('error'))
cmd('select_none', {'image_index': 0})

print()
print("=== Cat 11: Blur, sharpen and drop shadow change pixels ===")
# Based on the pixel checks in #62 by gr8monk3ys.


def canvas(width=200, height=200, fill='white'):
    """Open a fresh image at index 0, or stop: the next steps would edit an older image."""
    _r = cmd('new_canvas', {'width': width, 'height': height, 'fill': fill})
    if _r.get('status') != 'success':
        print(f"new_canvas failed: {_r.get('error', '')}", file=sys.stderr)
        sys.exit(1)
    return _r.get('results', {}).get('image_id')


# blur: 5px left of the black/white edge picks up some white.
canvas()
half_black()
_p = px(145, 50)
chk('f_blur_starts_black', _p == [0, 0, 0], _p)
t('f_blur', cmd('blur', {'image_index': 0, 'radius_x': 10.0, 'radius_y': 10.0}))
_p = px(145, 50)
chk('f_blur_spreads', _p is not None and _p[0] > 0, _p)

# sharpen: next to a gray edge, the light side gets lighter.
cmd('fill_layer', {'image_index': 0, 'color': '#a0a0a0'})
cmd('fill_rectangle', {'image_index': 0, 'x': 0, 'y': 0, 'width': 150, 'height': 200, 'color': '#606060'})
_p = px(151, 50)
chk('f_sharpen_starts_gray', _p == [0xa0, 0xa0, 0xa0], _p)
t('f_sharpen', cmd('sharpen', {'image_index': 0, 'amount': 100, 'radius': 3.0}))
_p = px(151, 50)
chk('f_sharpen_raises_edge_contrast', _p is not None and _p[0] > 0xa0, _p)


def layers():
    """The image's layers, or None if list_layers failed or couldn't read a layer."""
    _r = cmd('list_layers', {'image_index': 0})
    _ls = _r.get('results', {}).get('layers') if _r.get('status') == 'success' else None
    if _ls is None or any('name' not in lyr or 'id' not in lyr for lyr in _ls):
        print(f"  list_layers failed: {_r.get('error') or _ls}", file=sys.stderr)
        return None
    return _ls


def shadow_layer():
    return next((lyr for lyr in layers() or [] if lyr['name'] == 'Drop Shadow'), None)


def gimp_exec(*lines):
    """Run Python in the plug-in; return its printed output, or None on error."""
    _r = cmd('exec', {'args': ['python-fu-exec', list(lines)]})
    return ''.join(_r.get('results') or []).strip() if _r.get('status') == 'success' else None


def shadow_alpha(shadow, x, y):
    """Alpha of the Drop Shadow layer at image coordinates (x, y)."""
    _, ox, oy = shadow['offsets']
    return cmd('get_pixel_color', {'image_index': 0, 'layer_name': 'Drop Shadow',
                                   'x': x - ox, 'y': y - oy}).get('results', {}).get('alpha')


# A shadow of an 80x80 box keeps the opacity asked for and has a soft edge.
# The box's position is locked: the shadow copy must still move and be padded.
canvas()
t('ds_box_layer', cmd('create_layer', {'image_index': 0, 'name': 'Box'}))
t('ds_box_fill', cmd('fill_rectangle', {'image_index': 0, 'layer_name': 'Box', 'x': 60, 'y': 60,
                                        'width': 80, 'height': 80, 'color': '#ff0000'}))
chk('ds_box_lock', gimp_exec(
    "_ds_box = [l for l in Gimp.get_images()[0].get_layers() if l.get_name() == 'Box'][0]",
    "print(_ds_box.set_lock_position(True))") == 'True')
t('ds_set_foreground', cmd('set_colors', {'foreground': '#00ff00'}))
t('ds_box', cmd('apply_drop_shadow', {'image_index': 0, 'layer_name': 'Box', 'offset_x': 8,
                                      'offset_y': 8, 'blur_radius': 4, 'opacity': 50}))
# The shadow is black, but the user's foreground color stays green.
_fg = cmd('get_context_state', {}).get('results', {}).get('foreground_color', {}).get('rgba')
chk('ds_keeps_foreground', _fg is not None and [round(v, 3) for v in _fg] == [0.0, 1.0, 0.0, 1.0], _fg)
_s = shadow_layer()
chk('ds_keeps_opacity', _s is not None and round(_s['opacity']) == 50, _s and _s['opacity'])
# The shadow's left edge is at x = 60 + 8.
_a = shadow_alpha(_s, 68, 100) if _s else None
chk('ds_edge_is_soft', _a is not None and 0 < _a < 255, f'edge alpha={_a}')
chk('ds_source_stays_locked', gimp_exec("print(_ds_box.get_lock_position())") == 'True')

# A shadow of a layer that fills the canvas, moved by (20, 20), spreads past
# the layer's left edge at x = 20, which needs the shadow layer to be padded.
canvas()
t('ds_full', cmd('apply_drop_shadow', {'image_index': 0, 'offset_x': 20, 'offset_y': 20,
                                       'blur_radius': 4, 'opacity': 100}))
_s = shadow_layer()
_a = shadow_alpha(_s, 17, 100) if _s else None
chk('ds_spreads_past_layer_edge', _a is not None and _a > 0, {'layer': _s, 'alpha': _a})

# The same for a layer with a white mask: the mask copied into the shadow
# must not hide the padding.
canvas()
t('ds_masked_layer', cmd('create_layer', {'image_index': 0, 'name': 'Masked', 'fill': '#ff0000'}))
chk('ds_add_mask', gimp_exec(
    "_ds_m = [l for l in Gimp.get_images()[0].get_layers() if l.get_name() == 'Masked'][0]",
    "print(_ds_m.add_mask(_ds_m.create_mask(Gimp.AddMaskType.WHITE)))") == 'True')
t('ds_masked', cmd('apply_drop_shadow', {'image_index': 0, 'layer_name': 'Masked', 'offset_x': 20,
                                         'offset_y': 20, 'blur_radius': 4, 'opacity': 100}))
# The alpha that shows: the layer's alpha times its mask, if it has one.
_shown = gimp_exec(
    "_ds_s = [l for l in Gimp.get_images()[0].get_layers() if l.get_name() == 'Drop Shadow'][0]",
    "_ds_x, _ds_y = 17 - _ds_s.get_offsets().offset_x, 100 - _ds_s.get_offsets().offset_y",
    "_ds_k = _ds_s.get_mask()",
    "print(_ds_s.get_pixel(_ds_x, _ds_y).get_rgba().alpha"
    " * (_ds_k.get_pixel(_ds_x, _ds_y).get_rgba().red if _ds_k else 1.0))")
chk('ds_masked_spreads_past_layer_edge', _shown is not None and float(_shown) > 0, _shown)

# Bad arguments fail before the shadow layer is added, so the layer stack is
# unchanged. (tests/test_pdb_status.py covers a blur that fails after it.)
canvas()
for _bad in ({'blur_radius': 'inf'}, {'blur_radius': 'nan'}, {'blur_radius': -1},
             {'blur_radius': 2000}, {'opacity': 200}):
    _before = layers()
    _r = cmd('apply_drop_shadow', dict(_bad, image_index=0))
    _after = layers()
    _ids = [[lyr['id'] for lyr in ls] if ls is not None else None for ls in (_before, _after)]
    chk('ds_rejects_' + '_'.join(f'{k}_{v}' for k, v in _bad.items()),
        _r.get('status') == 'error' and _ids[0] is not None and _ids[0] == _ids[1],
        {'status': _r.get('status'), 'layer_ids': _ids, 'error': _r.get('error')})

print()
print("=== Cat 12: export_web_optimized file names ===")
# Based on #62 by gr8monk3ys. The files used to be named after the export
# copy, which has no file, so every export went to image.jpg / image.png.
# The snap GIMP has a private /tmp, so use a folder under the home directory.
_home_tmp = os.path.expanduser('~/tmp')
os.makedirs(_home_tmp, exist_ok=True)
_out = tempfile.mkdtemp(prefix='gimp_mcp_web_', dir=_home_tmp)


def read_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


try:
    _src = os.path.join(_out, 'photo.png')
    canvas(64, 64, '#3366cc')
    t('web_save_source', cmd('export_image', {'image_index': 0, 'file_path': _src, 'format': 'png'}))
    _src_bytes = read_bytes(_src) if os.path.exists(_src) else None
    _o = t('web_open_source', cmd('open_image', {'file_path': _src}))
    _id = _o.get('results', {}).get('image_id') if _o.get('status') == 'success' else None
    _idx = next((i['index'] for i in cmd('list_images', {}).get('results', {}).get('images', [])
                 if _id is not None and i['image_id'] == _id), None)
    chk('web_source_open', _idx is not None, {'image_id': _id, 'index': _idx})
    if _idx is not None:
        _w = t('web_export', cmd('export_web_optimized', {'image_index': _idx, 'output_dir': _out}))
        _res = _w.get('results', {})
        # photo.png would overwrite the source, so both files get a suffix.
        chk('web_named_after_source',
            [os.path.basename(_res.get(k, '')) for k in ('jpeg_path', 'png_path')]
            == ['photo_web.jpg', 'photo_web.png'], _res)
        chk('web_source_untouched', _src_bytes is not None and read_bytes(_src) == _src_bytes)
        # Through a symlinked folder the paths differ, but it's the same file.
        _link = os.path.join(_home_tmp, os.path.basename(_out) + '_link')
        os.symlink(_out, _link)
        try:
            _res = cmd('export_web_optimized', {'image_index': _idx, 'output_dir': _link}).get('results', {})
            chk('web_symlink_named_after_source',
                os.path.basename(_res.get('png_path', '')) == 'photo_web.png', _res)
            chk('web_symlink_source_untouched', _src_bytes is not None and read_bytes(_src) == _src_bytes)
        finally:
            os.remove(_link)
        cmd('close_image', {'image_index': _idx})
    # An image without a file is named after its index.
    canvas(64, 64, '#3366cc')
    _res = cmd('export_web_optimized', {'image_index': 0, 'output_dir': _out}).get('results', {})
    chk('web_untitled_name', os.path.basename(_res.get('jpeg_path', '')) == 'image_0.jpg', _res)
finally:
    shutil.rmtree(_out, ignore_errors=True)

print()
print(f"=== TOTAL: {passed}/{passed+failed} PASSED ===")
if failures:
    print("Failures:")
    for n, e in failures:
        print(f"  {n}: {e}")

sys.exit(1 if failures else 0)
