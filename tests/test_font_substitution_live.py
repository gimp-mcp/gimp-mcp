#!/usr/bin/env python3
"""Live regression test for issue #124: add_text and edit_text font reporting.

Checks font lookup outcomes against a running GIMP 3.2 MCP server for both
add_text and edit_text:
1. Default / empty / whitespace-padded font names: `add_text` without `font`
   uses the default ("Sans-serif") without `requested_font` or `warning`,
   trailing whitespace is stripped, and `""` selects the default font without a
   warning.
2. An exact installed font name: reports `font` only (no `requested_font`, no
   `warning`) and applies that font to the text layer.
3. A GIMP 2.x / style alias (e.g. "Sans" -> "Sans-serif"): reports `font` and
   `requested_font`, without `warning`, and applies the resolved font.
4. An unknown font name ("Comic Snas"): reports `font`, `requested_font`, and a
   `warning` naming both fonts and pointing to `list_fonts`, and applies the
   fallback font.
Also checks that edit_text without `font` omits all font fields.

Requires the GIMP MCP plugin running on localhost:9877 (GIMP_MCP_PORT overrides
the port). Run it directly, not with pytest. Exits 0 on pass, 1 on fail.
"""
import json
import os
import socket
import sys

HOST, PORT = "127.0.0.1", int(os.environ.get("GIMP_MCP_PORT", 9877))
UNKNOWN_FONT = "Comic Snas"


def send(msg, timeout=60):
    """Send one JSON message to the plugin socket and return the decoded reply."""
    with socket.create_connection((HOST, PORT), timeout=timeout) as s:
        s.sendall(json.dumps(msg).encode() + b"\n")
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            try:
                return json.loads(buf.decode())
            except json.JSONDecodeError:
                continue
    return {"status": "error", "error": "parse: " + buf.decode()[:200]}


def cmd(t, params=None):
    """Send a {type, params} tool command."""
    return send({"type": t, "params": params or {}})


def py(*lines):
    """Run Python lines in the plugin's persistent context; return printed output."""
    r = send({"cmds": list(lines)})
    if r.get("status") != "success":
        fail(f"exec failed: {r.get('error', r)}")
    return "".join(r.get("results") or []).strip()


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def image_index():
    """Return the current index in Gimp.get_images() of _t124_img."""
    out = py(
        "print(next(i for i, im in enumerate(Gimp.get_images()) "
        "if im.get_id() == _t124_img.get_id()))"
    )
    return int(out)


def layer_font_name(layer_name):
    """Return the actual Gimp.Font name applied to `layer_name` on _t124_img."""
    return py(
        f"print(next(l for l in _t124_img.get_layers() if l.get_name() == {layer_name!r})"
        ".get_font().get_name())"
    )


def discover_font_cases():
    """Query GIMP for an exact font, a step-2 alias pair, and the step-3 fallback."""
    out = py(
        "import json\n"
        "from gi.repository import Gimp\n"
        "_t124_raw = Gimp.fonts_get_list('')\n"
        "_t124_flist = (\n"
        "    list(_t124_raw[1]) if isinstance(_t124_raw, tuple) else list(_t124_raw)\n"
        ")\n"
        "_t124_names = [f.get_name() for f in _t124_flist]\n"
        "_t124_exact = (\n"
        "    Gimp.Font.get_by_name('Serif').get_name()\n"
        "    if Gimp.Font.get_by_name('Serif') is not None\n"
        "    else (_t124_names[0] if _t124_names else None)\n"
        ")\n"
        "_t124_alias = None\n"
        "if Gimp.Font.get_by_name('Sans') is None and Gimp.Font.get_by_name('Sans-serif') is not None:\n"
        "    _t124_alias = ['Sans', Gimp.Font.get_by_name('Sans-serif').get_name()]\n"
        "else:\n"
        "    for _t124_n in _t124_names:\n"
        "        for _t124_cand in (\n"
        "            _t124_n[:-6] if _t124_n.endswith('-serif') else None,\n"
        "            _t124_n[:-8] if _t124_n.endswith(' Regular') else None,\n"
        "            _t124_n.replace('-', ' ') if '-' in _t124_n else None,\n"
        "        ):\n"
        "            if _t124_cand and Gimp.Font.get_by_name(_t124_cand) is None:\n"
        "                for _t124_a in (\n"
        "                    _t124_cand + '-serif',\n"
        "                    _t124_cand + ' Regular',\n"
        "                    _t124_cand.replace(' ', '-'),\n"
        "                ):\n"
        "                    _t124_hit = Gimp.Font.get_by_name(_t124_a)\n"
        "                    if _t124_hit is not None:\n"
        "                        _t124_alias = [_t124_cand, _t124_hit.get_name()]\n"
        "                        break\n"
        "                if _t124_alias:\n"
        "                    break\n"
        "        if _t124_alias:\n"
        "            break\n"
        "_t124_fb = None\n"
        "for _t124_f in ('Sans-serif', 'Serif', 'Monospace'):\n"
        "    _t124_o = Gimp.Font.get_by_name(_t124_f)\n"
        "    if _t124_o is not None:\n"
        "        _t124_fb = _t124_o.get_name()\n"
        "        break\n"
        "if _t124_fb is None and _t124_names:\n"
        "    _t124_fb = _t124_names[0]\n"
        f"_t124_u = {UNKNOWN_FONT!r}\n"
        "_t124_unknown_matches = any(\n"
        "    Gimp.Font.get_by_name(_t124_q) is not None\n"
        "    for _t124_q in (\n"
        "        _t124_u,\n"
        "        _t124_u + '-serif',\n"
        "        _t124_u + ' Regular',\n"
        "        _t124_u.replace(' ', '-'),\n"
        "    )\n"
        ")\n"
        "print(json.dumps({'exact': _t124_exact, 'alias': _t124_alias, "
        "'fallback': _t124_fb, 'unknown_matches': _t124_unknown_matches, "
        "'has_sans_serif': Gimp.Font.get_by_name('Sans-serif') is not None}))"
    )
    info = json.loads(out)
    if not info["exact"]:
        fail("GIMP reported no installed fonts")
    if not info["alias"]:
        fail("could not find an uninstalled alias for any installed GIMP font")
    if info["unknown_matches"]:
        fail(f"expected {UNKNOWN_FONT!r} (and its aliases) not to be installed in GIMP")
    return info["exact"], tuple(info["alias"]), info["fallback"], info["has_sans_serif"]


def assert_exact(tool, res, layer_name, exact_font):
    if res.get("font") != exact_font:
        fail(f"{tool} exact: expected font={exact_font!r}, got {res}")
    if "requested_font" in res:
        fail(f"{tool} exact: unexpected requested_font in {res}")
    if "warning" in res:
        fail(f"{tool} exact: unexpected warning in {res}")
    applied = layer_font_name(layer_name)
    if applied != exact_font:
        fail(f"{tool} exact: layer font is {applied!r}, expected {exact_font!r}")
    print(f"PASS: {tool} exact font {exact_font!r} -> font={res['font']!r}, no warning")


def assert_alias(tool, res, layer_name, alias_name, alias_target):
    if res.get("font") != alias_target:
        fail(f"{tool} alias: expected font={alias_target!r}, got {res}")
    if res.get("requested_font") != alias_name:
        fail(f"{tool} alias: expected requested_font={alias_name!r}, got {res}")
    if "warning" in res:
        fail(f"{tool} alias: alias match should not report warning: {res}")
    applied = layer_font_name(layer_name)
    if applied != alias_target:
        fail(f"{tool} alias: layer font is {applied!r}, expected {alias_target!r}")
    print(f"PASS: {tool} alias {alias_name!r} -> font={alias_target!r}, no warning")


def assert_substituted(tool, res, layer_name, requested, expected_fallback):
    if res.get("font") != expected_fallback:
        fail(f"{tool} unknown: expected font={expected_fallback!r}, got {res}")
    if res.get("requested_font") != requested:
        fail(f"{tool} unknown: expected requested_font={requested!r}, got {res}")
    warning = res.get("warning")
    if not isinstance(warning, str) or requested not in warning or expected_fallback not in warning:
        fail(f"{tool} unknown: warning must name {requested!r} and {expected_fallback!r}: {res}")
    if "list_fonts" not in warning:
        fail(f"{tool} unknown: warning must point to list_fonts: {warning!r}")
    applied = layer_font_name(layer_name)
    if applied != expected_fallback:
        fail(f"{tool} unknown: layer font is {applied!r}, expected {expected_fallback!r}")
    print(f"PASS: {tool} unknown {requested!r} -> substituted {expected_fallback!r} with warning")


def main():
    needs_cleanup = False
    try:
        # Discovery already defines _t124_* names in GIMP, so clean up even if
        # it fails.
        needs_cleanup = True
        (
            exact_font,
            (alias_name, alias_target),
            expected_fallback,
            has_sans_serif,
        ) = discover_font_cases()
        py(
            "from gi.repository import Gimp",
            "_t124_img = Gimp.Image.new(240, 120, Gimp.ImageBaseType.RGB)",
            "_t124_bg = Gimp.Layer.new(_t124_img, 'bg', 240, 120, "
            "Gimp.ImageType.RGB_IMAGE, 100, Gimp.LayerMode.NORMAL)",
            "_t124_img.insert_layer(_t124_bg, None, 0)",
        )

        # add_text: default (font omitted), exact, trailing whitespace, alias, unknown
        r = cmd("add_text", {"image_index": image_index(), "text": "Default",
                             "x": 5, "y": 5, "size": 20, "color": "black"})
        if r.get("status") != "success":
            fail(f"add_text default failed: {r}")
        if has_sans_serif:
            assert_exact("add_text (default)", r["results"], r["results"]["layer_name"],
                         expected_fallback)
        else:
            assert_substituted("add_text (default)", r["results"], r["results"]["layer_name"],
                               "Sans-serif", expected_fallback)

        r = cmd("add_text", {"image_index": image_index(), "text": "Exact",
                             "x": 5, "y": 25, "font": exact_font, "size": 20, "color": "black"})
        if r.get("status") != "success":
            fail(f"add_text exact failed: {r}")
        exact_layer = r["results"]["layer_name"]
        assert_exact("add_text", r["results"], exact_layer, exact_font)

        r = cmd("add_text", {"image_index": image_index(), "text": "Trimmed",
                             "x": 5, "y": 45, "font": exact_font + " ", "size": 20,
                             "color": "black"})
        if r.get("status") != "success":
            fail(f"add_text trailing-space failed: {r}")
        assert_exact("add_text (trimmed)", r["results"], r["results"]["layer_name"], exact_font)

        r = cmd("add_text", {"image_index": image_index(), "text": "Alias",
                             "x": 5, "y": 65, "font": alias_name, "size": 20, "color": "black"})
        if r.get("status") != "success":
            fail(f"add_text alias failed: {r}")
        assert_alias("add_text", r["results"], r["results"]["layer_name"],
                     alias_name, alias_target)

        r = cmd("add_text", {"image_index": image_index(), "text": "Unknown",
                             "x": 5, "y": 85, "font": UNKNOWN_FONT, "size": 20, "color": "black"})
        if r.get("status") != "success":
            fail(f"add_text unknown failed: {r}")
        assert_substituted("add_text", r["results"], r["results"]["layer_name"],
                           UNKNOWN_FONT, expected_fallback)

        # edit_text: alias, exact (with trailing space), empty string, unknown, and no-font
        r = cmd("edit_text", {"image_index": image_index(), "layer_name": exact_layer,
                              "font": alias_name})
        if r.get("status") != "success":
            fail(f"edit_text alias failed: {r}")
        assert_alias("edit_text", r["results"], exact_layer, alias_name, alias_target)

        r = cmd("edit_text", {"image_index": image_index(), "layer_name": exact_layer,
                              "font": exact_font + " "})
        if r.get("status") != "success":
            fail(f"edit_text exact failed: {r}")
        assert_exact("edit_text", r["results"], exact_layer, exact_font)

        for blank in ("", "   "):
            r = cmd("edit_text", {"image_index": image_index(), "layer_name": exact_layer,
                                  "font": blank})
            if r.get("status") != "success":
                fail(f"edit_text blank font {blank!r} failed: {r}")
            for unexpected in ("font", "requested_font", "warning"):
                if unexpected in r["results"]:
                    fail(f"edit_text blank font {blank!r} returned {unexpected!r}: {r['results']}")
            if layer_font_name(exact_layer) != exact_font:
                fail(f"edit_text blank font {blank!r} changed the font to "
                     f"{layer_font_name(exact_layer)!r}, expected {exact_font!r}")
        print("PASS: edit_text with a blank font leaves the font unchanged")

        r = cmd("edit_text", {"image_index": image_index(), "layer_name": exact_layer,
                              "font": UNKNOWN_FONT})
        if r.get("status") != "success":
            fail(f"edit_text unknown failed: {r}")
        assert_substituted("edit_text", r["results"], exact_layer, UNKNOWN_FONT, expected_fallback)

        r = cmd("edit_text", {"image_index": image_index(), "layer_name": exact_layer,
                              "text": "Retitled"})
        if r.get("status") != "success":
            fail(f"edit_text without font failed: {r}")
        for unexpected in ("font", "requested_font", "warning"):
            if unexpected in r["results"]:
                fail(f"edit_text without font returned {unexpected!r}: {r['results']}")
        print("PASS: edit_text without font omits font fields")
        return 0
    except ConnectionRefusedError:
        fail(f"GIMP MCP plugin not reachable on {HOST}:{PORT} (Tools > MCP > Start MCP Server)")
    finally:
        if needs_cleanup:
            try:
                py("if '_t124_img' in globals() and _t124_img.is_valid(): _t124_img.delete()")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete _t124_img", file=sys.stderr)
            try:
                py("[globals().pop(n) for n in [n for n in globals() if n.startswith('_t124_')]]")
            except (SystemExit, OSError):
                pass


if __name__ == "__main__":
    sys.exit(main())
