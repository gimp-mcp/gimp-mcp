#!/usr/bin/env python3
"""Live regression test for issue #64, items 1 and 2.

1. export_sprite_sheet used Gimp.ImageBaseType.RGBA, which GIMP 3.2 doesn't
   have, so it always failed. It also flattened the sheet on export, which
   filled the transparent background. The test builds a 4-frame image (one
   frame hidden and visibility-locked), exports a 2x2 sheet with padding, and
   checks the PNG's size, that it has an alpha channel, that each frame is in
   its cell and that the horizontal and vertical padding is transparent. It
   also checks that the export leaves the context background color and the
   source image's selection as they were. A second sheet checks that a
   text-layer frame and a group frame with a locked child export, and that a
   locked frame larger than its cell is clipped to it. A third sheet checks
   that a CLIP_TO_BACKDROP frame stays visible, that a live drop shadow is
   drawn and cropped to its cell, that a mask shown for editing isn't drawn,
   that a mask stays aligned with a frame whose filter reaches outside it,
   and that a group child's mask shown for editing isn't drawn either.
   Another sheet, exported first while no other images are open, checks
   source="images".
2. edit_text passed the font name as a string, but gimp-text-layer-set-font
   takes a Gimp.Font, so changing the font always failed. The test changes a
   text layer's font and checks the reported and the applied font.

Requires the GIMP MCP plugin running on localhost:9877 (GIMP_MCP_PORT
overrides the port). The sheet is written to a temporary directory; with a
Snap GIMP, which has a private /tmp, set TMPDIR to a directory GIMP can see
(e.g. ~/tmp). Run it directly, not with pytest. Exits 0 on pass, 1 on fail.
"""
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import textwrap

HOST, PORT = '127.0.0.1', int(os.environ.get('GIMP_MCP_PORT', 9877))
FRAME_W, FRAME_H, PADDING, COLUMNS = 30, 20, 4, 2
# Frame colors as (name, RGB). Frame 2 is hidden in the source image.
FRAMES = [('red', (1, 0, 0)), ('lime', (0, 1, 0)), ('blue', (0, 0, 1)), ('yellow', (1, 1, 0))]
HIDDEN_FRAME = 2
USER_SELECTION = (2, 3, 10, 8)  # x, y, w, h set on the source before exporting


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


def py_in_context(*lines):
    """Run lines between Gimp.context_push() and context_pop() as one command.

    One command with try/finally pops the context even if a line fails; split
    across commands, a failing line would leave the context pushed. A line may
    be a multi-line block.
    """
    body = ''.join(textwrap.indent(line, '    ') + '\n' for line in lines)
    return py(f"Gimp.context_push()\ntry:\n{body}finally:\n    Gimp.context_pop()")


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def image_index(var):
    """Return the current index in Gimp.get_images() of the image held in `var`."""
    out = py(f"print(next(i for i, im in enumerate(Gimp.get_images()) "
             f"if im.get_id() == {var}.get_id()))")
    return int(out)


def png_header(path):
    """Return (width, height, color_type) from a PNG file's IHDR chunk."""
    with open(path, 'rb') as f:
        head = f.read(26)
    if head[:8] != b'\x89PNG\r\n\x1a\n':
        fail(f"{path} is not a PNG file")
    width, height = struct.unpack('>II', head[16:24])
    return width, height, head[25]


def sample(path, points):
    """Load `path` in GIMP once and return the RGBA (0-1, rounded) at each (x, y)."""
    out = py(
        "from gi.repository import Gio",
        f"_t64_load = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path({path!r}))",
        # One command: the plugin runs each command separately, so a
        # try/finally must not be split across them.
        "try:\n"
        "    _t64_px = _t64_load.get_layers()[0]\n"
        f"    print([[round(v, 3) for v in _t64_px.get_pixel(x, y).get_rgba()] for x, y in {points!r}])\n"
        "finally:\n"
        "    _t64_load.delete()",
    )
    return json.loads(out)


def context_background():
    return py("print([round(v, 3) for v in Gimp.context_get_background().get_rgba()])")


def selection_bounds(var):
    return py(f"print(list(Gimp.Selection.bounds({var})))")


def check_sprite_sheet(tmpdir):
    py("from gi.repository import Gimp, Gegl")
    lines = [
        f"_t64_frames = Gimp.Image.new({FRAME_W}, {FRAME_H}, Gimp.ImageBaseType.RGB)",
        "Gimp.context_set_opacity(100.0)",
        "Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)",
    ]
    # Insert in reverse so the layer stack (top first) is frame0, frame1, ...
    for i, (name, _) in reversed(list(enumerate(FRAMES))):
        lines += [
            f"_t64_l = Gimp.Layer.new(_t64_frames, 'frame{i}', {FRAME_W}, {FRAME_H}, "
            "Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)",
            "_t64_frames.insert_layer(_t64_l, None, 0)",
            f"Gimp.context_set_foreground(Gegl.Color.new('{name}'))",
            "_t64_l.edit_fill(Gimp.FillType.FOREGROUND)",
        ]
        if i == HIDDEN_FRAME:
            # A visibility lock would make the export's set_visible() a no-op.
            lines += ["_t64_l.set_visible(False)", "_t64_l.set_lock_visibility(True)"]
    lines.append(
        # A user's selection the export must not touch.
        "_t64_frames.select_rectangle(Gimp.ChannelOps.REPLACE, %d, %d, %d, %d)" % USER_SELECTION)
    py_in_context(*lines)

    # Set a user's background color the export must not touch, in a context
    # that's popped afterwards so the plugin's own background is restored.
    py("Gimp.context_push()")
    try:
        py("Gimp.context_set_background(Gegl.Color.new('rgb(1.0, 0.5, 0.0)'))")
        export_and_check_sheet(tmpdir)
    finally:
        py("Gimp.context_pop()")


def export_and_check_sheet(tmpdir):
    bg_before = context_background()
    sel_before = selection_bounds('_t64_frames')

    out_path = os.path.join(tmpdir, 'sheet.png')
    r = cmd('export_sprite_sheet', {
        'output_path': out_path, 'columns': COLUMNS, 'padding': PADDING,
        'source': 'layers', 'image_index': image_index('_t64_frames'),
    })
    print(f"export_sprite_sheet: {json.dumps(r)[:300]}")
    if r.get('status') != 'success':
        fail(f"export_sprite_sheet failed: {r.get('error', r)}")
    if not os.path.isfile(out_path):
        fail(f"export_sprite_sheet reported success but wrote no file at {out_path}")

    rows = -(-len(FRAMES) // COLUMNS)
    want = (COLUMNS * FRAME_W + (COLUMNS - 1) * PADDING, rows * FRAME_H + (rows - 1) * PADDING)
    width, height, color_type = png_header(out_path)
    if (width, height) != want:
        fail(f"sheet is {width}x{height}, expected {want[0]}x{want[1]}")
    if color_type != 6:  # 6 = RGBA
        fail(f"sheet PNG has color type {color_type}, expected 6 (RGBA): background was flattened")

    centers = [((i % COLUMNS) * (FRAME_W + PADDING) + FRAME_W // 2,
                (i // COLUMNS) * (FRAME_H + PADDING) + FRAME_H // 2) for i in range(len(FRAMES))]
    gaps = [(FRAME_W + PADDING // 2, FRAME_H // 2),   # between the columns
            (FRAME_W // 2, FRAME_H + PADDING // 2)]   # between the rows
    pixels = sample(out_path, centers + gaps)
    for i, ((name, rgb), px) in enumerate(zip(FRAMES, pixels)):
        if px != [*map(float, rgb), 1.0]:
            fail(f"cell {i} should be opaque {name}, got RGBA {px}")
    for (x, y), px in zip(gaps, pixels[len(FRAMES):]):
        if px[3] != 0.0:
            fail(f"padding pixel at ({x}, {y}) is not transparent: RGBA {px}")
    print(f"PASS: sheet is {width}x{height} RGBA, each frame in its cell "
          "(hidden frame included), padding transparent")

    if context_background() != bg_before:
        fail(f"background color changed from {bg_before} to {context_background()}")
    if selection_bounds('_t64_frames') != sel_before:
        fail(f"source selection changed from {sel_before} to {selection_bounds('_t64_frames')}")
    print("PASS: background color and source selection unchanged")


def check_sprite_sheet_mixed_frames(tmpdir):
    """Text, group and locked frames must export; a large frame is clipped.

    Frames, top layer first:
    - a red 30x20 layer, which sets the cell size;
    - a blue 60x40 layer with content and position locks. It would spill past
      its cell into the padding, and it's in the middle so a spill would land
      on the canvas. The copy inherits the locks, which would block moving it
      to the frame's origin.
    - a text layer, a layer subclass with its own methods (it once broke the
      export through TextLayer.resize()).
    - a group whose lime child has content and position locks. Unlocking the
      group copy doesn't unlock the child, which would block moving the group.
    """
    # Each layer is inserted at the top, so insert the bottom frame first.
    py("from gi.repository import Gimp, Gegl")
    py_in_context(
        f"_t64_mixed = Gimp.Image.new({FRAME_W}, {FRAME_H}, Gimp.ImageBaseType.RGB)",
        "Gimp.context_set_opacity(100.0)",
        "Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)",
        "_t64_group = Gimp.GroupLayer.new(_t64_mixed, 'group')",
        "_t64_mixed.insert_layer(_t64_group, None, 0)",
        f"_t64_l = Gimp.Layer.new(_t64_mixed, 'child', {FRAME_W}, {FRAME_H}, "
        "Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)",
        "_t64_mixed.insert_layer(_t64_l, _t64_group, 0)",
        "Gimp.context_set_foreground(Gegl.Color.new('lime'))",
        "_t64_l.edit_fill(Gimp.FillType.FOREGROUND)",
        "_t64_l.set_lock_content(True)",
        "_t64_l.set_lock_position(True)",
        "_t64_l = Gimp.TextLayer.new(_t64_mixed, '7', Gimp.Font.get_by_name('Sans-serif'), "
        "14.0, Gimp.Unit.pixel())",
        "_t64_mixed.insert_layer(_t64_l, None, 0)",
        f"_t64_l = Gimp.Layer.new(_t64_mixed, 'big', {FRAME_W * 2}, {FRAME_H * 2}, "
        "Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)",
        "_t64_mixed.insert_layer(_t64_l, None, 0)",
        "Gimp.context_set_foreground(Gegl.Color.new('blue'))",
        "_t64_l.edit_fill(Gimp.FillType.FOREGROUND)",
        # The copy inherits these locks; they must not block clipping it.
        "_t64_big = _t64_l",
        "_t64_big.set_lock_content(True)",
        "_t64_big.set_lock_position(True)",
        f"_t64_l = Gimp.Layer.new(_t64_mixed, 'red', {FRAME_W}, {FRAME_H}, "
        "Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)",
        "_t64_mixed.insert_layer(_t64_l, None, 0)",
        "Gimp.context_set_foreground(Gegl.Color.new('red'))",
        "_t64_l.edit_fill(Gimp.FillType.FOREGROUND)",
    )
    out_path = os.path.join(tmpdir, 'mixed.png')
    r = cmd('export_sprite_sheet', {
        'output_path': out_path, 'columns': 4, 'padding': PADDING,
        'source': 'layers', 'image_index': image_index('_t64_mixed'),
    })
    print(f"export_sprite_sheet (text, group and locked frames): {json.dumps(r)[:300]}")
    if r.get('status') != 'success':
        fail(f"export_sprite_sheet with text, group and locked frames failed: {r.get('error', r)}")

    want = (4 * FRAME_W + 3 * PADDING, FRAME_H)
    width, height, _ = png_header(out_path)
    if (width, height) != want:
        fail(f"sheet is {width}x{height}, expected {want[0]}x{want[1]}")
    big_x = FRAME_W + PADDING  # the large frame is frame 1
    points = [(big_x + FRAME_W - 1, FRAME_H - 1),          # last pixel of its cell
              (big_x + FRAME_W + PADDING // 2, 5),          # gap after it
              (3 * (FRAME_W + PADDING) + FRAME_W // 2, FRAME_H // 2)]  # group frame
    corner, gap, group = sample(out_path, points)
    if corner != [0.0, 0.0, 1.0, 1.0]:
        fail(f"large frame's cell should be filled blue up to its edge, got RGBA {corner}")
    if gap[3] != 0.0:
        fail(f"large frame spilled into the padding after its cell: RGBA {gap}")
    if group != [0.0, 1.0, 0.0, 1.0]:
        fail(f"group frame's cell should be lime, got RGBA {group}")
    locks = py("print(_t64_big.get_lock_content(), _t64_big.get_lock_position())")
    if locks != "True True":
        fail(f"source frame's content/position locks changed to {locks}")
    print(f"PASS: text and group frames exported, locked {FRAME_W * 2}x{FRAME_H * 2} frame "
          "clipped to its cell, source locks unchanged")


def drop_shadow(var, dx):
    """Lines that add a live, hard-edged drop shadow dx px to the side of layer `var`."""
    return [
        f"_t64_sf = Gimp.DrawableFilter.new({var}, 'gegl:dropshadow', 'shadow')",
        "_t64_cfg = _t64_sf.get_config()",
        f"_t64_cfg.set_property('x', {float(dx)})",
        "_t64_cfg.set_property('y', 0.0)",
        "_t64_cfg.set_property('radius', 0.0)",
        "_t64_cfg.set_property('opacity', 2.0)",
        "_t64_sf.update()",
        f"{var}.append_filter(_t64_sf)",
    ]


def fill_rect(var, x, y, w, h, color):
    """Lines that fill a rectangle of drawable `var` in image _t64_fx with a color."""
    return [
        f"_t64_fx.select_rectangle(Gimp.ChannelOps.REPLACE, {x}, {y}, {w}, {h})",
        f"Gimp.context_set_foreground(Gegl.Color.new('{color}'))",
        f"{var}.edit_fill(Gimp.FillType.FOREGROUND)",
        "Gimp.Selection.none(_t64_fx)",
    ]


SHADOW_DX = -12   # the drop shadow's offset, to the left
SHADOW_FILL_X = 8  # the shadowed frame is blue from here to its right edge


def check_sprite_sheet_frame_effects(tmpdir):
    """Composite mode and live filters must not leak from a source frame.

    Frames, top layer first:
    - a red 30x20 layer, which sets the cell size. It has non-AUTO blend and
      composite spaces, which the copy resets; on a transparent sheet they
      don't change the pixels, so this only checks the reset doesn't fail.
    - a lime layer in NORMAL mode with composite mode CLIP_TO_BACKDROP. Resetting
      the mode to NORMAL keeps that composite mode, which would make the frame
      invisible on the transparent sheet.
    - a layer that is blue from x=8 to its right edge, with a live drop shadow
      12 px to the left. The shadow must be drawn in the empty part of its
      cell (x=0-7), so a dropped filter fails, and must not spill into the
      padding before the cell. The blue must start at x=8, so the frame
      doesn't move in its cell.
    - a yellow layer whose white mask is shown for editing (Alt-click). The
      copy keeps that setting, which would draw the mask instead of the frame.
    - a blue layer whose mask is white on its left half, with the same left
      shadow. The mask must stay aligned with the pixels: the shadow reaches
      outside the layer, but only the left half may be visible.
    - a group whose yellow child has a white mask shown for editing. Hiding
      the mask only on the group copy would still draw the child's mask.
    """
    py("from gi.repository import Gimp, Gegl")
    py_in_context(
        f"_t64_fx = Gimp.Image.new({FRAME_W}, {FRAME_H}, Gimp.ImageBaseType.RGB)",
        "Gimp.context_set_opacity(100.0)",
        "Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)",
        "_t64_mgroup = Gimp.GroupLayer.new(_t64_fx, 'mask group')",
        "_t64_fx.insert_layer(_t64_mgroup, None, 0)",
        f"_t64_mchild = Gimp.Layer.new(_t64_fx, 'mask child', {FRAME_W}, {FRAME_H}, "
        "Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)",
        "_t64_fx.insert_layer(_t64_mchild, _t64_mgroup, 0)",
        "Gimp.context_set_foreground(Gegl.Color.new('yellow'))",
        "_t64_mchild.edit_fill(Gimp.FillType.FOREGROUND)",
        "_t64_mchild.add_mask(_t64_mchild.create_mask(Gimp.AddMaskType.WHITE))",
        "_t64_mchild.set_show_mask(True)",
        *[line for name, color in (('halfmask', 'blue'), ('masked', 'yellow'),
                                   ('shadowed', 'blue'), ('clipped', 'lime'), ('red', 'red'))
          for line in (
              f"_t64_l = Gimp.Layer.new(_t64_fx, '{name}', {FRAME_W}, {FRAME_H}, "
              "Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)",
              "_t64_fx.insert_layer(_t64_l, None, 0)",
              f"Gimp.context_set_foreground(Gegl.Color.new('{color}'))",
              "_t64_l.edit_fill(Gimp.FillType.FOREGROUND)",
              f"_t64_{name} = _t64_l",
          )],
        "_t64_clipped.set_composite_mode(Gimp.LayerCompositeMode.CLIP_TO_BACKDROP)",
        "_t64_masked.add_mask(_t64_masked.create_mask(Gimp.AddMaskType.WHITE))",
        "_t64_masked.set_show_mask(True)",
        "_t64_red.set_blend_space(Gimp.LayerColorSpace.RGB_PERCEPTUAL)",
        "_t64_red.set_composite_space(Gimp.LayerColorSpace.RGB_LINEAR)",
        # Leave x=0-7 of the shadowed frame empty for its shadow to land on.
        "_t64_shadowed.edit_fill(Gimp.FillType.TRANSPARENT)",
        *fill_rect('_t64_shadowed', SHADOW_FILL_X, 0, FRAME_W - SHADOW_FILL_X, FRAME_H, 'blue'),
        *drop_shadow('_t64_shadowed', SHADOW_DX),
        "_t64_hm = _t64_halfmask.create_mask(Gimp.AddMaskType.BLACK)",
        "_t64_halfmask.add_mask(_t64_hm)",
        *fill_rect('_t64_hm', 0, 0, FRAME_W // 2, FRAME_H, 'white'),
        *drop_shadow('_t64_halfmask', SHADOW_DX),
    )
    out_path = os.path.join(tmpdir, 'effects.png')
    r = cmd('export_sprite_sheet', {
        'output_path': out_path, 'columns': 6, 'padding': PADDING,
        'source': 'layers', 'image_index': image_index('_t64_fx'),
    })
    print(f"export_sprite_sheet (composite mode and live filter): {json.dumps(r)[:300]}")
    if r.get('status') != 'success':
        fail(f"export_sprite_sheet with a filtered frame failed: {r.get('error', r)}")

    cell = FRAME_W + PADDING
    y = FRAME_H // 2
    shadowed_x, halfmask_x = 2 * cell, 4 * cell
    # The half-masked frame's visible half ends at FRAME_W // 2.
    halfmask_cols = [1, 5, 10, FRAME_W // 2 - 1, FRAME_W // 2 + 1, FRAME_W - 5]
    points = [(FRAME_W // 2, y),                               # frame with non-AUTO spaces
              (cell + FRAME_W // 2, y),                        # clip-to-backdrop frame
              (shadowed_x - PADDING // 2, y),                  # gap before the shadowed frame
              (shadowed_x + SHADOW_FILL_X // 2, y),            # its shadow, inside its cell
              (shadowed_x + SHADOW_FILL_X, y),                 # where its blue starts
              (shadowed_x + FRAME_W - 1, y),                   # its right edge
              (3 * cell + FRAME_W // 2, y),                    # frame with a shown mask
              (5 * cell + FRAME_W // 2, y),                    # group child with a shown mask
              *[(halfmask_x + c, y) for c in halfmask_cols]]   # half-masked frame
    red, clipped, gap, shadow, left, right, masked, child_masked, *halfmask = sample(out_path, points)
    if red != [1.0, 0.0, 0.0, 1.0]:
        fail(f"frame with non-AUTO blend/composite spaces should be red, got RGBA {red}")
    if clipped != [0.0, 1.0, 0.0, 1.0]:
        fail(f"CLIP_TO_BACKDROP frame should be opaque lime, got RGBA {clipped}")
    if gap[3] != 0.0:
        fail(f"drop shadow leaked into the padding before its cell: RGBA {gap}")
    if shadow[3] == 0.0 or shadow[:3] != [0.0, 0.0, 0.0]:
        fail(f"drop shadow should be drawn inside its cell, got RGBA {shadow} (filter dropped?)")
    if left != [0.0, 0.0, 1.0, 1.0] or right != [0.0, 0.0, 1.0, 1.0]:
        fail(f"shadowed frame moved in its cell: blue at x={SHADOW_FILL_X} {left}, "
             f"right edge {right}")
    if masked != [1.0, 1.0, 0.0, 1.0]:
        fail(f"frame with a shown mask should be yellow, got RGBA {masked} (the mask was drawn)")
    if child_masked != [1.0, 1.0, 0.0, 1.0]:
        fail(f"group whose child shows its mask should be yellow, got RGBA {child_masked} "
             "(the child's mask was drawn)")
    alphas = [px[3] for px in halfmask]
    if alphas != [1.0, 1.0, 1.0, 1.0, 0.0, 0.0]:
        fail(f"half-masked frame should be visible left of x={FRAME_W // 2} and transparent "
             f"right of it; alpha at x={halfmask_cols}: {alphas} (mask shifted?)")
    print("PASS: CLIP_TO_BACKDROP frame visible, live drop shadow drawn and cropped to its "
          "cell, shown masks not drawn (layer and group child), mask stays aligned with a "
          "filtered frame")


def check_sprite_sheet_from_images(tmpdir):
    """With source="images", each image's top layer keeps its place on its canvas.

    source="images" uses every open image, so this runs only when GIMP has no
    other images open; otherwise it is skipped. Images, newest (first frame)
    first:
    - a 30x20 image whose 50x50 blue layer sits at (10, 5): the frame shows the
      layer from (10, 5) on, clipped to the cell;
    - a 30x20 image filled red by an RGB layer without alpha.
    """
    if py("print(len(Gimp.get_images()))") != "0":
        print("SKIP: source='images' check needs GIMP with no images open")
        return
    py("from gi.repository import Gimp, Gegl")
    py_in_context(
        "Gimp.context_set_opacity(100.0)",
        "Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)",
        f"_t64_img_red = Gimp.Image.new({FRAME_W}, {FRAME_H}, Gimp.ImageBaseType.RGB)",
        f"_t64_l = Gimp.Layer.new(_t64_img_red, 'red', {FRAME_W}, {FRAME_H}, "
        "Gimp.ImageType.RGB_IMAGE, 100, Gimp.LayerMode.NORMAL)",
        "_t64_img_red.insert_layer(_t64_l, None, 0)",
        "Gimp.context_set_foreground(Gegl.Color.new('red'))",
        "_t64_l.edit_fill(Gimp.FillType.FOREGROUND)",
        f"_t64_img_off = Gimp.Image.new({FRAME_W}, {FRAME_H}, Gimp.ImageBaseType.RGB)",
        "_t64_l = Gimp.Layer.new(_t64_img_off, 'offset', 50, 50, "
        "Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)",
        "_t64_img_off.insert_layer(_t64_l, None, 0)",
        "_t64_l.set_offsets(10, 5)",
        "Gimp.context_set_foreground(Gegl.Color.new('blue'))",
        "_t64_l.edit_fill(Gimp.FillType.FOREGROUND)",
    )
    out_path = os.path.join(tmpdir, 'images.png')
    r = cmd('export_sprite_sheet', {'output_path': out_path, 'columns': 2,
                                    'padding': PADDING, 'source': 'images'})
    print(f"export_sprite_sheet (source=images): {json.dumps(r)[:300]}")
    if r.get('status') != 'success':
        fail(f"export_sprite_sheet with source='images' failed: {r.get('error', r)}")
    if r['results'].get('count') != 2:
        fail(f"expected 2 frames from the 2 open images, got {r['results'].get('count')}")
    cell = FRAME_W + PADDING
    points = [(5, 2),                               # offset frame, left of and above its layer
              (12, 7),                              # inside its layer
              (FRAME_W - 1, FRAME_H - 1),           # its last pixel, still inside the layer
              (FRAME_W + PADDING // 2, FRAME_H - 1),  # gap after it: the layer is clipped
              (cell + FRAME_W // 2, FRAME_H // 2)]  # red frame
    outside, inside, corner, gap, red = sample(out_path, points)
    if outside[3] != 0.0:
        fail(f"offset frame lost its position: (5, 2) should be transparent, got RGBA {outside}")
    if inside != [0.0, 0.0, 1.0, 1.0] or corner != [0.0, 0.0, 1.0, 1.0]:
        fail(f"offset frame should be blue from (10, 5) to its cell's edge, got {inside} and {corner}")
    if gap[3] != 0.0:
        fail(f"offset frame spilled into the padding: RGBA {gap}")
    if red != [1.0, 0.0, 0.0, 1.0]:
        fail(f"second frame should be red, got RGBA {red}")
    py("_t64_img_off.delete()", "_t64_img_red.delete()")
    print("PASS: source='images' keeps each layer's position on its canvas and clips it to the cell")


def check_edit_text_font():
    py(
        "from gi.repository import Gimp",
        "_t64_text = Gimp.Image.new(200, 80, Gimp.ImageBaseType.RGB)",
        "_t64_bg = Gimp.Layer.new(_t64_text, 'bg', 200, 80, "
        "Gimp.ImageType.RGB_IMAGE, 100, Gimp.LayerMode.NORMAL)",
        "_t64_text.insert_layer(_t64_bg, None, 0)",
    )
    r = cmd('add_text', {'image_index': image_index('_t64_text'), 'text': 'Font test',
                         'x': 5, 'y': 5, 'font': 'Sans-serif', 'size': 20, 'color': 'black'})
    if r.get('status') != 'success':
        fail(f"add_text failed: {r.get('error', r)}")
    layer_name = r['results']['layer_name']

    r = cmd('edit_text', {'image_index': image_index('_t64_text'),
                          'layer_name': layer_name, 'font': 'Serif'})
    print(f"edit_text font=Serif: {json.dumps(r)[:300]}")
    if r.get('status') != 'success':
        fail(f"edit_text with a font failed: {r.get('error', r)}")
    if r['results'].get('font') != 'Serif':
        fail(f"edit_text reported font {r['results'].get('font')!r}, expected 'Serif'")
    applied = py(f"print(next(l for l in _t64_text.get_layers() if l.get_name() == {layer_name!r})"
                 ".get_font().get_name())")
    if applied != 'Serif':
        fail(f"text layer font is {applied!r} after edit_text, expected 'Serif'")
    print("PASS: edit_text changed the font to Serif and reported it")


def main():
    tmpdir = tempfile.mkdtemp(prefix='gimp-mcp-64-')
    try:
        # First, while no test images are open: it exports every open image.
        check_sprite_sheet_from_images(tmpdir)
        check_sprite_sheet(tmpdir)
        check_sprite_sheet_mixed_frames(tmpdir)
        check_sprite_sheet_frame_effects(tmpdir)
        check_edit_text_font()
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        for var in ('_t64_img_off', '_t64_img_red', '_t64_frames', '_t64_mixed', '_t64_fx',
                    '_t64_text', '_t64_load'):
            try:
                py(f"if '{var}' in globals() and {var}.is_valid(): {var}.delete()")
            except SystemExit:
                print(f"  (cleanup) could not delete {var}", file=sys.stderr)
        # The plugin's Python namespace is shared and persistent, so drop the
        # test's names too, not only the images they pointed at.
        try:
            py("[globals().pop(n) for n in [n for n in globals() if n.startswith('_t64_')]]")
        except SystemExit:
            print("  (cleanup) could not remove the test's _t64_* names", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
