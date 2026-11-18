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
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from test_gimp3_compat import FakeImage3, install_gi_stub, load_plugin  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}: {name}{f' ({detail})' if detail and not condition else ''}")
    if not condition:
        failures.append(name)


def main():
    img = FakeImage3()
    state = {"bounds": (True, False, 0, 0, 0, 0), "calls": []}
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
    for name, value in plugin.CSS_COLORS.items():
        if to_hex(name) != value:
            check(f"name {name}", False, to_hex(name))

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
    expect_error("rgb(255,,128,0)", "commas only")
    expect_error("rgb(255, 128 0)", "commas only")
    expect_error("rgba(255,0,0/0.5)", "commas only")
    expect_error("rgb(255 / 128 0 0.5)", "after the '/'")
    expect_error("rgb(255 128 0 / 0.5 1)", "after the '/'")
    expect("rgba(255 128 0 0.5)", "#ff800080")  # accepted before, so still accepted
    expect("rgb(1e308%, 0%, 0%)", "#ff0000")
    expect("rgba(0,0,0,1e308%)", "#000000ff")
    expect_error("rgb(255,0,0", "Unknown color")
    expect_error("hsl(0,100%,50%)", "Unknown color")
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
    # group, which would clear GIMP's redo history. An empty color is an error
    # too, not "use the current foreground".
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
        ("_draw_line", {"color": "", "x2": 5, "y2": 5}),
        ("_draw_rectangle", {"color": "", **rect}),
        ("_draw_ellipse", {"color": "", **rect}),
        ("_gradient_fill", {"color1": "nope", "color2": "white"}),
        ("_gradient_fill", {"color1": "orange", "color2": "nope"}),
        ("_add_text", {"text": "hi", "color": "nope"}),
    ]:
        started.clear()
        r = getattr(p, tool)({"image_index": 0, **params})
        colors = {k: v for k, v in params.items() if k.startswith("color")}
        name = f"{tool} {colors}"
        check(f"{name} rejected",
              r.get("status") == "error" and "Unknown color" in r.get("error", ""), r)
        check(f"{name} opened no undo group", not started, len(started))

    print(f"\n{len(failures)} failure(s)" if failures else "\nAll color parsing tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
