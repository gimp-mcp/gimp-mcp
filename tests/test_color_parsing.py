#!/usr/bin/env python3
"""Tests for color parsing in the plugin (issue #44, PR #54).

Gegl.Color.new() paints cyan for any string it can't read and reports
success. color_to_hex() turns every supported color into hex first and
raises ValueError for anything else:

- all 148 CSS color names, case-insensitive
- hex: #rgb, #rgba, #rrggbb, #rrggbbaa
- rgb()/rgba() with 0-255 or 0-1 values, percentages, or "r g b / a"
  ("r g b a" too, since 0.5.0 accepted it)
- [r, g, b(, a)] lists
- errors for unknown names, bad hex, too few or too many values, and
  non-numbers, before anything is painted

Stubs gi.repository, so no GIMP is needed. Run with:
uv run tests/test_color_parsing.py
Exits 0 on pass, 1 on fail.
"""
import hashlib
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from test_gimp3_compat import FakeImage3, install_gi_stub, load_plugin  # noqa: E402

failures = []

# sha256 of the sorted "name=#hex" pairs in CSS_COLORS, computed from Pillow's
# ImageColor.colormap (lowercased); see the check in main().
CSS_COLORS_SHA256 = "cf637d36ac5eef8835d63fd5f9f0cd88bdc1a1c0d0aa2dd44f68cf0cd6d03d10"


def check(name, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}: {name}{f' ({detail})' if detail and not condition else ''}")
    if not condition:
        failures.append(name)


def main():
    img = FakeImage3()
    state = {"bounds": (True, False, 0, 0, 0, 0), "calls": [], "ops": []}
    install_gi_stub([img], state)
    plugin = load_plugin()
    to_hex = plugin.color_to_hex

    def expect(spec, want):
        try:
            got = to_hex(spec)
        except Exception as e:  # noqa: BLE001 - report any failure
            got = f"{type(e).__name__}: {e}"
        check(f"{spec!r} -> {want}", got == want, got)

    def expect_error(spec, *fragments):
        try:
            got = to_hex(spec)
        except (ValueError, TypeError) as e:
            message = str(e)
            check(f"{spec!r} rejected", all(f in message for f in fragments), message)
            return
        check(f"{spec!r} rejected", False, f"returned {got!r}")

    # CSS names (#44): the extended names that used to paint cyan.
    check("148 CSS names", len(plugin.CSS_COLORS) == 148, len(plugin.CSS_COLORS))
    expect("orange", "#ffa500")
    expect("cornflowerblue", "#6495ed")
    expect("rebeccapurple", "#663399")
    expect("grey", "#808080")
    expect("  Gold ", "#ffd700")
    expect("transparent", "#00000000")
    expect("Transparent", "#00000000")
    expect("None", "#00000000")  # Gegl reads "none" as transparent, so 0.5.0 did too
    for spec, want in [("transparent", True), (" None ", True), ("NONE", True),
                       ("#00000000", False), ("white", False), (None, False), ([], False)]:
        check(f"is_transparent({spec!r}) is {want}", plugin.is_transparent(spec) is want)
    for name, value in plugin.CSS_COLORS.items():
        if to_hex(name) != value:
            check(f"name {name}", False, to_hex(name))
    # The table was checked against Pillow's ImageColor.colormap (all 148 equal).
    # This checksum makes any later edit to a name or value fail here; if one is
    # intended, check the table again and update the checksum.
    table = ",".join(f"{k}={v}" for k, v in sorted(plugin.CSS_COLORS.items()))
    digest = hashlib.sha256(table.encode()).hexdigest()
    check("CSS_COLORS unchanged", digest == CSS_COLORS_SHA256, digest)

    # Hex.
    expect("#FF5733", "#ff5733")
    expect("#69e", "#6699ee")
    expect("#69e8", "#6699ee88")
    expect("#ff000080", "#ff000080")

    # rgb() / rgba(): the existing 0-255 / 0-1 rules still hold.
    expect("rgb(40,180,90)", "#28b45a")
    expect("rgb(1.0, 0.5, 0)", "#ff8000")
    expect("RGB(255, 87, 51)", "#ff5733")
    expect("rgb(300,-5,90)", "#ff005a")
    expect("rgba(255,0,0,0.5)", "#ff000080")
    expect("rgba(255,0,0,128)", "#ff000080")
    expect("rgb(255 128 0)", "#ff8000")
    expect("rgb(255 128 0 / 0.5)", "#ff800080")
    expect("rgb (255,0,0)", "#ff0000")  # 0.5.0 accepted a space before the paren
    expect("RGBA ( 255, 0, 0, 0.5 )", "#ff000080")

    # Percentages.
    expect("rgb(50%,50%,50%)", "#808080")
    expect("rgb(100%, 0%, 0%)", "#ff0000")
    expect("rgba(100%,0%,0%,50%)", "#ff000080")
    expect("rgba(255,0,0,50%)", "#ff000080")
    expect("rgb(150%,0%,0%)", "#ff0000")

    # Lists.
    expect([255, 128, 0], "#ff8000")
    expect((1.0, 0.5, 0.0, 0.5), "#ff800080")

    # Errors instead of cyan.
    expect_error("definitely-not-a-color", "Unknown color")
    expect_error("", "Unknown color")
    expect_error("ff0000", "Did you mean '#ff0000'?")
    expect("#ff00", "#ffff0000")  # 4 digits is #rgba
    expect_error("#12345", "Invalid hex color")
    expect_error("#gg0000", "Invalid hex color")
    expect_error("rgb(1,2)", "got 2")
    expect_error("rgba(1,2,3,4,5)", "got 5")
    expect_error("rgb(a,b,c)", "'a' is not a number")
    expect_error("rgb(nan,0,0)", "is not a number")
    expect_error("rgb(50%,128,0)", "percentages")
    expect_error("rgb(255,,128,0)", "a value is empty")
    expect_error("rgb(255,0,0,)", "a value is empty")
    expect_error("rgb(,255,0,0)", "a value is empty")
    expect_error("rgb(255, 128 0)", "commas only")
    expect_error("rgb(50\t%,0%,0%)", "commas only")  # whitespace inside a value
    expect("rgb(\t50%,\n0%, 0% )", "#800000")  # whitespace around values is fine
    expect_error("rgba(255,0,0/0.5)", "commas only")
    expect_error("rgb(255 / 128 0 0.5)", "after the '/'")
    expect_error("rgb(255 128 0 / 0.5 1)", "after the '/'")
    expect("rgba(255 128 0 0.5)", "#ff800080")  # accepted before, so still accepted
    expect("rgb(1e308%, 0%, 0%)", "#ff0000")
    expect("rgba(0,0,0,1e308%)", "#000000ff")
    expect_error("rgb(255,0,0", "Unknown color")
    expect_error("rgb(1,2,3) junk", "Unknown color")
    expect_error("rgbx(1,2,3)", "Unknown color")
    expect_error("hsl(0,100%,50%)", "Unknown color")
    expect_error("cmyk(0,100,100,0)", "Unknown color")  # Gegl read it, but undocumented
    expect_error("cmyka(0,100,100,0,1)", "Unknown color")
    expect_error([1, 2], "got 2")
    expect_error(None, "expected a string")

    # _color hands Gegl the hex string, passes Gegl colors through, and
    # rejects other values instead of handing them to GIMP.
    p = plugin.MCPPlugin()
    check("_color builds from hex", p._color("orange") == ("color", "#ffa500"), p._color("orange"))
    gegl_color = sys.modules["gi.repository"].Gegl.Color()
    check("_color passes Gegl colors through", p._color(gegl_color) is gegl_color)
    for bad in (5, False, object()):
        try:
            got = p._color(bad)
        except TypeError:
            check(f"_color rejects {bad!r}", True)
        else:
            check(f"_color rejects {bad!r}", False, f"returned {got!r}")

    # A tool given a bad color returns an error before it starts painting.
    started = []
    img.undo_group_start = lambda: started.append(True)
    r = p._fill_layer({"image_index": 0, "color": "orangeish"})
    check("fill_layer rejects unknown color",
          r.get("status") == "error" and "Unknown color" in r.get("error", ""), r)
    check("fill_layer painted nothing", not started and not state["calls"],
          {"undo_group_start": len(started), "calls": state["calls"]})

    # The other painting tools also reject a bad color before opening an undo
    # group, which would clear GIMP's redo history. (An empty color means
    # "no color" instead; see below.)
    state["bounds"] = (True, True, 0, 0, 10, 10)  # fill_selection needs a selection
    img.get_width = lambda: 100
    img.get_height = lambda: 100
    img._layer.get_id = lambda: 1
    sys.modules["gi.repository"].Gimp.GradientType = types.SimpleNamespace(RADIAL=1, LINEAR=0)
    p._resolve_font = lambda font: (object(), False)
    rect = {"x": 0, "y": 0, "width": 5, "height": 5}
    for tool, params in [
        ("_fill_selection", {"color": "nope", "fill_type": "foreground"}),
        ("_draw_line", {"color": "nope", "x2": 5, "y2": 5}),
        ("_draw_rectangle", {"color": "nope", **rect}),
        ("_draw_ellipse", {"color": "nope", **rect}),
        ("_draw_line", {"color": [], "x2": 5, "y2": 5}),  # only None and "" mean "no color"
        ("_gradient_fill", {"color1": "nope", "color2": "white"}),
        ("_gradient_fill", {"color1": "orange", "color2": "nope"}),
        ("_add_text", {"text": "hi", "color": "nope"}),
    ]:
        started.clear()
        r = getattr(p, tool)({"image_index": 0, **params})
        colors = {k: v for k, v in params.items() if k.startswith("color")}
        name = f"{tool} {colors}"
        error = r.get("error", "")
        check(f"{name} rejected",
              r.get("status") == "error" and ("Unknown color" in error or "Invalid color" in error), r)
        check(f"{name} opened no undo group", not started, len(started))

    # No color ("" or None, which the MCP server sends for an omitted color)
    # means "use the current foreground": the tool parses no color, sets no
    # foreground, and paints. Fill types that don't use the color ignore even
    # a bad one. The stubs record which paint operation ran.
    gimp = sys.modules["gi.repository"].Gimp
    ops = []
    gimp.context_set_foreground = staticmethod(lambda color: ops.append(f"set_foreground {color}"))
    gimp.context_set_brush_size = staticmethod(lambda size: None)
    gimp.context_set_stroke_method = staticmethod(lambda method: None)
    gimp.context_set_line_width = staticmethod(lambda width: None)
    gimp.StrokeMethod = types.SimpleNamespace(LINE=0)
    gimp.pencil = staticmethod(lambda drawable, coords: ops.append("pencil") or True)
    gimp.Drawable.edit_fill = staticmethod(
        lambda drawable, fill_type: ops.append(f"edit_fill {fill_type}") or True)
    gimp.Drawable.edit_clear = staticmethod(lambda drawable: ops.append("edit_clear") or True)
    img.select_rectangle = lambda *args: ops.append("select_rectangle")
    img.select_ellipse = lambda *args: ops.append("select_ellipse")
    p._lookup_pdb = lambda name: types.SimpleNamespace(
        name=name, create_config=lambda: types.SimpleNamespace(set_property=lambda *args: None))
    p._run_pdb = lambda proc, config: ops.append(proc.name)
    stroke = "gimp-drawable-edit-stroke-selection"
    fill_fg = f"edit_fill {gimp.FillType.FOREGROUND}"
    fill_bg = f"edit_fill {gimp.FillType.BACKGROUND}"

    # An unknown fill_type is an error, not a silent color fill.
    for bad_type in ("foregroud", "solid"):
        ops.clear()
        started.clear()
        r = p._fill_selection({"image_index": 0, "fill_type": bad_type, "color": "red"})
        check(f"_fill_selection fill_type={bad_type!r} rejected",
              r.get("status") == "error" and "Unknown fill_type" in r.get("error", "")
              and "'pattern'" in r.get("error", ""), r)
        check(f"_fill_selection fill_type={bad_type!r} changed nothing", not ops, ops)
        check(f"_fill_selection fill_type={bad_type!r} opened no undo group", not started, len(started))

    parsed = []
    real_color = p._color
    p._color = lambda spec: parsed.append(spec) or real_color(spec)
    for tool, params, expected_ops in [
        ("_draw_line", {"color": None, "x2": 5, "y2": 5}, ["pencil"]),
        ("_draw_line", {"color": "", "x2": 5, "y2": 5}, ["pencil"]),
        ("_draw_rectangle", {"color": "", **rect}, ["select_rectangle", stroke]),
        ("_draw_ellipse", {"color": None, **rect}, ["select_ellipse", stroke]),
        ("_fill_selection", {"color": None, "fill_type": None}, [fill_fg]),
        ("_fill_selection", {"color": "", "fill_type": "foreground"}, [fill_fg]),
        ("_fill_selection", {}, [fill_fg]),  # no color key at all (a raw socket call)
        ("_fill_selection", {"color": "nope", "fill_type": "background"}, [fill_bg]),
        ("_fill_selection", {"color": "nope", "fill_type": "transparent"}, ["edit_clear"]),
    ]:
        parsed.clear()
        ops.clear()
        r = getattr(p, tool)({"image_index": 0, **params})
        name = f"{tool} {params}"
        check(f"{name} succeeds", r.get("status") == "success", r)
        check(f"{name} parses no color", not parsed, parsed)
        check(f"{name} runs {expected_ops} and sets no foreground", ops == expected_ops, ops)

    # Control: with a color, the same calls set the foreground to it first.
    # (The stub's Gegl.Color.new returns ("color", hex).)
    set_red = f"set_foreground {('color', '#ff0000')}"
    for tool, params, expected_ops in [
        ("_draw_line", {"color": "red", "x2": 5, "y2": 5}, [set_red, "pencil"]),
        ("_draw_rectangle", {"color": "red", **rect}, [set_red, "select_rectangle", stroke]),
        ("_fill_selection", {"color": "red"}, [set_red, fill_fg]),
    ]:
        ops.clear()
        r = getattr(p, tool)({"image_index": 0, **params})
        check(f"{tool} {params} runs {expected_ops}",
              r.get("status") == "success" and ops == expected_ops, (r.get("error"), ops))
    p._color = real_color

    # resize_canvas: fill="none" is transparent, so it must not flatten the
    # image (a fill color flattens it onto that color).
    flattened = []
    img.resize = lambda *args: True
    img.flatten = lambda: flattened.append(True) or img._layer
    sys.modules["gi.repository"].Gimp.context_set_background = staticmethod(lambda color: None)
    for fill, should_flatten in [("none", False), ("NONE", False), ("transparent", False),
                                 ("white", True)]:
        flattened.clear()
        r = p._resize_canvas({"image_index": 0, "width": 50, "height": 50, "fill": fill})
        check(f"resize_canvas fill={fill!r} succeeds", r.get("status") == "success", r)
        check(f"resize_canvas fill={fill!r} {'flattens' if should_flatten else 'does not flatten'}",
              bool(flattened) == should_flatten, len(flattened))

    # More tools given a bad color return an error before changing anything:
    # no undo group, context push, resize, fill or color set.
    gimp.context_push = staticmethod(lambda: ops.append("context_push"))
    gimp.context_set_background = staticmethod(lambda color: ops.append(f"set_background {color}"))
    img.resize = lambda *args: ops.append("resize") or True
    for tool, params in [
        ("_fill_rectangle", {"color": "nope", **rect}),
        ("_fill_ellipse", {"color": "nope", **rect}),
        ("_select_by_color", {"color": "nope"}),
        ("_set_colors", {"foreground": "red", "background": "nope"}),
        ("_edit_text", {"text": "x", "color": "nope"}),
        ("_apply_drop_shadow", {"color": "nope"}),
        ("_resize_canvas", {"width": 50, "height": 50, "fill": "nope"}),
    ]:
        started.clear()
        ops.clear()
        r = getattr(p, tool)({"image_index": 0, **params})
        name = f"{tool} {params}"
        check(f"{name} rejected",
              r.get("status") == "error" and "Unknown color" in r.get("error", ""), r)
        check(f"{name} changed nothing", not started and not ops,
              {"undo_group_start": len(started), "ops": ops})

    # set_colors and edit_text: "" leaves that color unchanged, like an omitted one.
    p._color = lambda spec: parsed.append(spec) or real_color(spec)
    parsed.clear()
    ops.clear()
    r = p._set_colors({"foreground": "", "background": "blue"})
    check("set_colors foreground='' sets only the background",
          r.get("status") == "success" and parsed == ["blue"]
          and ops == [f"set_background {('color', '#0000ff')}"], (r, parsed, ops))
    ops.clear()
    r = p._set_colors({"foreground": "red", "background": []})
    check("set_colors background=[] rejected before any color is set",
          r.get("status") == "error" and not ops, (r.get("error"), ops))
    parsed.clear()
    ops.clear()
    img._layer.is_text_layer = lambda: True  # edit_text refuses other layers
    r = p._edit_text({"image_index": 0, "layer_name": "layer", "text": "x", "color": ""})
    check("edit_text color='' changes the text but not the color",
          r.get("status") == "success" and not parsed
          and "gimp-text-layer-set-text" in ops and "gimp-text-layer-set-color" not in ops,
          (r.get("error"), parsed, ops))
    p._color = real_color

    # new_canvas and create_layer: "none" (any case) is a transparent fill, and
    # new_canvas reads a null fill as omitted (white), not as the string "None".
    class NewLayer:
        def fill(self, fill_type):
            ops.append(f"layer_fill {fill_type}")
            return True

        def add_alpha(self):
            ops.append("add_alpha")

        def delete(self):
            ops.append("layer_delete")

        def get_parent(self):
            return None

        def get_name(self):
            return "new"

        def get_id(self):
            return 2

    class NewImage:
        def set_resolution(self, x, y):
            pass

        def insert_layer(self, layer, parent, position):
            return True

        def delete(self):
            ops.append("image_delete")

        def get_id(self):
            return 3

    gimp.FillType.TRANSPARENT = 3
    gimp.MAX_IMAGE_SIZE = 524288
    gimp.ImageType = types.SimpleNamespace(RGB_IMAGE=0, RGBA_IMAGE=1, GRAY_IMAGE=2, GRAYA_IMAGE=3,
                                           INDEXEDA_IMAGE=5)
    gimp.Image = types.SimpleNamespace(new=lambda width, height, base_type: NewImage())
    gimp.Layer = types.SimpleNamespace(new=lambda *args: NewLayer())
    p._create_display = lambda image: False
    p._blend_mode_from_string = lambda mode: 0
    img.get_base_type = lambda: gimp.ImageBaseType.RGB
    img.insert_layer = lambda layer, parent, position: True
    img.get_item_position = lambda layer: 0
    transparent_fill = ["layer_fill 3"]
    canvas_transparent_fill = ["add_alpha", "layer_fill 3"]  # new_canvas adds alpha first
    white_fill = [f"set_background {('color', '#ffffff')}", "layer_fill 1"]
    for tool, params, expected_ops in [
        ("_new_canvas", {"width": 10, "height": 10, "fill": "none"}, canvas_transparent_fill),
        ("_new_canvas", {"width": 10, "height": 10, "fill": "None"}, canvas_transparent_fill),
        ("_new_canvas", {"width": 10, "height": 10, "fill": None}, white_fill),
        ("_new_canvas", {"width": 10, "height": 10}, white_fill),
        ("_create_layer", {"name": "a", "fill": "NONE"}, transparent_fill),
        ("_create_layer", {"name": "b", "fill": "transparent"}, transparent_fill),
        ("_create_layer", {"name": "c", "fill": "white"}, white_fill),
    ]:
        ops.clear()
        r = getattr(p, tool)({"image_index": 0, **params})
        name = f"{tool} {params}"
        check(f"{name} succeeds", r.get("status") == "success", r)
        # context_push, from the background fill, isn't part of the comparison.
        got = [op for op in ops if op != "context_push"]
        check(f"{name} fills {expected_ops}", got == expected_ops, got)

    print(f"\n{len(failures)} failure(s)" if failures else "\nAll color parsing tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
