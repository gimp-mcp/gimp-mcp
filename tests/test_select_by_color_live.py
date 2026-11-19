#!/usr/bin/env python3
"""Live test for select_by_color when nothing matches (#63).

It used to report success when no pixel matched; with "replace" the user's
selection was then silently gone. Checks that:
- with no matching pixel it returns an error for every operation, and the
  selection, the image's dirty flag and the image list are unchanged;
- a threshold that isn't a whole number 0-255 (300 and -5 used the
  context's threshold instead, 1.9 was cut to 1) is an error that changes
  nothing; a whole-number float or a digit string works;
- a sample criterion left in the plugin's context by call_api (RGB_RED
  matched white when selecting red) doesn't change what matches, and is
  still set afterwards;
- a match returns has_selection and the selection's bounds;
- subtracting everything that matched is a success with has_selection false;
- on a layer inside a group (the active layer) it matches that layer's pixels,
  not another layer's.

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


def image_index():
    """Index of the test image in Gimp.get_images(), as the tools count it."""
    return int(py("print(next(i for i, im in enumerate(Gimp.get_images()) "
                  "if im.get_id() == _sbc_img.get_id()))"))


def select_by_color(color, **params):
    return cmd('select_by_color', dict(color=color, image_index=image_index(), **params))


def bounds():
    """The selection as 'non_empty x y width height'."""
    return py("_b = Gimp.Selection.bounds(_sbc_img)",
              "print(_b[1], _b[2], _b[3], _b[4] - _b[2], _b[5] - _b[3])")


def new_test_image():
    """A 100x80 image, clean, with no selection:
    - 'bg': white, with a red 30x25 rectangle at (20, 10);
    - group 'grp' on top, holding 'top' (empty) above 'inner' (transparent, with a
      green 10x10 square at (60, 50)).
    """
    py("_sbc_img = Gimp.Image.new(100, 80, Gimp.ImageBaseType.RGB)",
       "_bg = Gimp.Layer.new(_sbc_img, 'bg', 100, 80, Gimp.ImageType.RGB_IMAGE,"
       " 100, Gimp.LayerMode.NORMAL)",
       "_sbc_img.insert_layer(_bg, None, 0)",
       "_grp = Gimp.GroupLayer.new(_sbc_img, 'grp')",
       "_sbc_img.insert_layer(_grp, None, 0)",
       "_inner = Gimp.Layer.new(_sbc_img, 'inner', 100, 80, Gimp.ImageType.RGBA_IMAGE,"
       " 100, Gimp.LayerMode.NORMAL)",
       "_sbc_img.insert_layer(_inner, _grp, 0)",
       "_top = Gimp.Layer.new(_sbc_img, 'top', 100, 80, Gimp.ImageType.RGBA_IMAGE,"
       " 100, Gimp.LayerMode.NORMAL)",
       "_sbc_img.insert_layer(_top, _grp, 0)",
       # One block, so the context is popped even if a fill fails. Earlier
       # call_api code may have changed opacity, paint mode or feathering.
       "Gimp.context_push()\n"
       "try:\n"
       "    Gimp.context_set_opacity(100.0)\n"
       "    Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)\n"
       "    Gimp.context_set_feather(False)\n"
       "    Gimp.context_set_foreground(Gegl.Color.new('#ffffff'))\n"
       "    _bg.edit_fill(Gimp.FillType.FOREGROUND)\n"
       "    _sbc_img.select_rectangle(Gimp.ChannelOps.REPLACE, 20, 10, 30, 25)\n"
       "    Gimp.context_set_foreground(Gegl.Color.new('#ff0000'))\n"
       "    _bg.edit_fill(Gimp.FillType.FOREGROUND)\n"
       "    _sbc_img.select_rectangle(Gimp.ChannelOps.REPLACE, 60, 50, 10, 10)\n"
       "    Gimp.context_set_foreground(Gegl.Color.new('#00ff00'))\n"
       "    _inner.edit_fill(Gimp.FillType.FOREGROUND)\n"
       "finally:\n"
       "    Gimp.context_pop()",
       "Gimp.Selection.none(_sbc_img)",
       "_sbc_img.clean_all()")


def main():
    made_image = False
    try:
        py("from gi.repository import Gegl", "_sbc_img = None",
           "_sbc_crit = Gimp.context_get_sample_criterion()")
        # Owned from here on, so cleanup also runs if the setup fails part way.
        made_image = True
        new_test_image()

        py("_sbc_img.select_rectangle(Gimp.ChannelOps.REPLACE, 5, 5, 10, 10)",
           "_sbc_img.clean_all()")
        n_images = py("print(len(Gimp.get_images()))")
        for op in ('replace', 'add', 'subtract', 'intersect'):
            r = select_by_color('#0000ff', layer_name='bg', operation=op)
            check(r.get('status') == 'error' and 'No pixels' in r.get('error', '')
                  and 'unchanged' in r.get('error', ''),
                  f"no match with operation={op} did not fail", r)
            check(bounds() == "True 5 5 10 10", f"operation={op} changed the selection",
                  bounds())
            check(py("print(_sbc_img.is_dirty())") == "False",
                  f"operation={op} marked the image dirty")
            check(py("print(len(Gimp.get_images()))") == n_images,
                  "an extra image was left open", n_images)
        print("PASS: no match is an error for every operation; selection and image unchanged")

        for bad in (300, -5, 256, 1.9, True, 'abc', None):
            r = select_by_color('#ff0000', layer_name='bg', threshold=bad)
            check(r.get('status') == 'error' and 'threshold' in r.get('error', ''),
                  f"threshold={bad!r} did not fail", r)
            check(bounds() == "True 5 5 10 10", f"threshold={bad!r} changed the selection",
                  bounds())
            check(py("print(_sbc_img.is_dirty())") == "False",
                  f"threshold={bad!r} marked the image dirty")
        for good in (0.0, '255'):
            r = select_by_color('#ff0000', layer_name='bg', threshold=good)
            check(r.get('status') == 'success', f"threshold={good!r} failed", r)
            # 255 matches every pixel, 0 only the red rectangle.
            want = "True 20 10 30 25" if good == 0.0 else "True 0 0 100 80"
            check(bounds() == want, f"threshold={good!r} selected the wrong pixels", bounds())
            py("_sbc_img.select_rectangle(Gimp.ChannelOps.REPLACE, 5, 5, 10, 10)",
               "_sbc_img.clean_all()")
        print("PASS: a threshold that isn't a whole number 0-255 is an error; 0.0 and '255' work")

        # Set without a context push, as call_api code may do: comparing only
        # the red channel, red at threshold 0 also matched the white background.
        py("Gimp.context_set_sample_criterion(Gimp.SelectCriterion.RGB_RED)")
        r = select_by_color('#ff0000', layer_name='bg', threshold=0)
        check(r.get('status') == 'success', "select_by_color red with criterion RGB_RED failed", r)
        check(bounds() == "True 20 10 30 25",
              "a sample criterion left in the context changed what matched", bounds())
        check(py("print(Gimp.context_get_sample_criterion() == Gimp.SelectCriterion.RGB_RED)")
              == "True", "select_by_color changed the caller's sample criterion")
        py("Gimp.context_set_sample_criterion(_sbc_crit)")
        print("PASS: a sample criterion left in the context doesn't change what matches")

        r = select_by_color('#ff0000', layer_name='bg')
        check(r.get('status') == 'success', "select_by_color red failed", r)
        got = {k: r['results'].get(k) for k in ('has_selection', 'x', 'y', 'width', 'height')}
        check(got == {'has_selection': True, 'x': 20, 'y': 10, 'width': 30, 'height': 25},
              "wrong result for the red rectangle", r)
        check(bounds() == "True 20 10 30 25", "the red rectangle was not selected", bounds())
        print("PASS: a match returns has_selection and the selection's bounds")

        r = select_by_color('#ff0000', layer_name='bg', operation='subtract')
        check(r.get('status') == 'success', "subtracting the match failed", r)
        check(r['results'].get('has_selection') is False and 'x' not in r['results'],
              "subtracting everything did not report an empty selection", r)
        check(bounds().startswith("False"), "the selection is not empty", bounds())
        print("PASS: subtracting everything that matched is a success with has_selection false")

        # The active layer is 'inner', the second layer of the group; layer_name
        # only finds top-level layers.
        py("_sbc_img.set_selected_layers([_inner])")
        r = select_by_color('#00ff00')
        check(r.get('status') == 'success', "select_by_color green on 'inner' failed", r)
        check(bounds() == "True 60 50 10 10", "the green square was not selected", bounds())
        r = select_by_color('#ff0000')
        check(r.get('status') == 'error' and "'inner'" in r.get('error', ''),
              "red matched on 'inner', which has none (wrong layer tried?)", r)
        check(bounds() == "True 60 50 10 10", "the failed match changed the selection",
              bounds())
        print("PASS: on a layer inside a group it matches that layer's pixels only")
        return 0
    finally:
        if made_image:
            try:
                py("if _sbc_img is not None and _sbc_img.is_valid(): _sbc_img.delete()",
                   "Gimp.context_set_sample_criterion(_sbc_crit)")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
