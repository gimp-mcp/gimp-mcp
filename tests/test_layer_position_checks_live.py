#!/usr/bin/env python3
"""Live test for create_layer and reorder_layer (#130).

Checks that create_layer:
- rejects positions below -1 and leaves the image unchanged;
- reports where the layer landed (position, and parent for a layer group) for
  -1, for 1, for -1 with a grouped layer selected, and for 0 under a floating
  selection, where the layer lands at 1;
- fills the whole layer, ignoring the selection and the context's opacity and
  paint mode;
- works on an indexed image.
And that create_layer and reorder_layer report an error when GIMP refuses the
insert or the move, with no leaked layer, the stack unchanged, and the image
not marked dirty. The refusals are stubbed (the method returns False, as libgimp
does when GIMP rejects a call), for the test image only.

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
    """Return the current index of the test image in Gimp.get_images()."""
    return int(py("print(next(i for i, im in enumerate(Gimp.get_images()) "
                  "if im.get_id() == _lpc_img.get_id()))"))


def layer_names():
    return json.loads(py("print(json.dumps([l.get_name() for l in _lpc_img.get_layers()]))"))


def is_dirty():
    return py("print(_lpc_img.is_dirty())") == "True"


def pixels(name):
    """Return [r, g, b, a] (0-1) of the named top-level layer at two points: one inside
    the 10x10 selection the test makes, one outside it."""
    return json.loads(py(f"_l = next(l for l in _lpc_img.get_layers() if l.get_name() == '{name}')",
                         "print(json.dumps([list(_l.get_pixel(x, y).get_rgba()) "
                         "for x, y in ((5, 5), (40, 30))]))"))


def check_reported(r, position, parent):
    """Check create_layer's reported position and parent, and that they match GIMP."""
    res = r.get('results', {})
    check(res.get('position') == position and res.get('parent', 'missing') == parent,
          f"expected position {position} and parent {parent!r}", res)
    actual = json.loads(py(
        f"_l = Gimp.Item.get_by_id({res['layer_id']})",
        "_p = _l.get_parent()",
        "print(json.dumps([_lpc_img.get_item_position(_l), _p.get_name() if _p else None]))"))
    check(actual == [position, parent], "the reported position is not where GIMP has it",
          actual)


def main():
    made_image = False
    saved_context = False
    stubbed = None
    try:
        py("import json", "_lpc_img = Gimp.Image.new(60, 40, Gimp.ImageBaseType.RGB)")
        made_image = True
        for name in ('bottom', 'top'):
            py(f"_l = Gimp.Layer.new(_lpc_img, '{name}', 60, 40, Gimp.ImageType.RGBA_IMAGE,"
               " 100, Gimp.LayerMode.NORMAL)",
               "_lpc_img.insert_layer(_l, None, 0)")
        py("_lpc_img.clean_all()")
        check(layer_names() == ['top', 'bottom'], "setup failed", layer_names())

        # Positions below -1 are rejected before anything changes.
        for pos in (-2, -5):
            r = cmd('create_layer', {'name': 'bad', 'position': pos, 'image_index': image_index()})
            check(r.get('status') == 'error' and 'position must be' in r.get('error', ''),
                  f"position {pos} was not rejected", r)
            check(layer_names() == ['top', 'bottom'], f"position {pos} changed the layers",
                  layer_names())
            check(not is_dirty(), f"position {pos} marked the image as changed")
        print("PASS: create_layer rejects positions below -1, image unchanged")

        # -1 goes directly above the selected layer ('top', inserted last). The fill
        # ignores the context's opacity and paint mode, which earlier calls may have
        # changed, so set odd ones and restore them at the end.
        py("_lpc_op = Gimp.context_get_opacity()",
           "_lpc_pm = Gimp.context_get_paint_mode()")
        saved_context = True
        py("Gimp.context_set_opacity(30)",
           "Gimp.context_set_paint_mode(Gimp.LayerMode.MULTIPLY)")
        r = cmd('create_layer', {'name': 'above-selected', 'position': -1,
                                 'image_index': image_index()})
        check(r.get('status') == 'success', "position -1 failed", r)
        check(layer_names() == ['above-selected', 'top', 'bottom'], "position -1 misplaced",
              layer_names())
        check_reported(r, 0, None)
        check(pixels('above-selected') == [[0.0, 0.0, 0.0, 0.0]] * 2,
              "the default fill is not transparent", pixels('above-selected'))
        # A color fill covers the whole layer even with a selection active.
        py("_lpc_img.select_rectangle(Gimp.ChannelOps.REPLACE, 0, 0, 10, 10)")
        r = cmd('create_layer', {'name': 'second', 'position': 1, 'fill': 'red',
                                 'image_index': image_index()})
        py("Gimp.Selection.none(_lpc_img)")
        check(r.get('status') == 'success', "position 1 failed", r)
        check(layer_names() == ['above-selected', 'second', 'top', 'bottom'],
              "position 1 misplaced", layer_names())
        check_reported(r, 1, None)
        check(pixels('second') == [[1.0, 0.0, 0.0, 1.0]] * 2,
              "the red fill did not cover the whole layer at full strength", pixels('second'))
        print("PASS: create_layer -1 and 1 land and report right; fills ignore the "
              "selection and the context's opacity and mode")

        # -1 with a layer inside a group selected: the layer goes into the group, and
        # the result names the group and gives the index inside it.
        py("_g = Gimp.GroupLayer.new(_lpc_img, 'grp')",
           "_lpc_img.insert_layer(_g, None, 0)",
           "_c = Gimp.Layer.new(_lpc_img, 'child', 60, 40, Gimp.ImageType.RGBA_IMAGE,"
           " 100, Gimp.LayerMode.NORMAL)",
           "_lpc_img.insert_layer(_c, _g, 0)",
           "_lpc_img.set_selected_layers([_c])")
        r = cmd('create_layer', {'name': 'in-group', 'image_index': image_index()})
        check(r.get('status') == 'success', "position -1 in a group failed", r)
        check_reported(r, 0, 'grp')
        kids = json.loads(py("print(json.dumps([l.get_name() for l in _g.get_children()]))"))
        check(kids == ['in-group', 'child'], "position -1 did not go into the group", kids)
        # Delete the group (and its children) so the top-level stack is as before.
        py("_lpc_img.remove_layer(_g)")
        check(layer_names() == ['above-selected', 'second', 'top', 'bottom'],
              "removing the group failed", layer_names())
        print("PASS: create_layer -1 inside a group reports the group and the index in it")

        # With a floating selection in the image (here above 'bottom', not on top),
        # GIMP puts a layer asked for at position 0 at 1. The result must say 1.
        py("_f = Gimp.Layer.new(_lpc_img, 'float', 10, 10, Gimp.ImageType.RGBA_IMAGE,"
           " 100, Gimp.LayerMode.NORMAL)",
           "Gimp.floating_sel_attach(_f, _lpc_img.get_layers()[-1])")
        r = cmd('create_layer', {'name': 'under-float', 'position': 0,
                                 'image_index': image_index()})
        check(r.get('status') == 'success', "position 0 under a floating selection failed", r)
        check_reported(r, 1, None)
        py("Gimp.floating_sel_anchor(_lpc_img.get_floating_sel())")
        check(layer_names() == ['above-selected', 'under-float', 'second', 'top', 'bottom'],
              "anchoring the floating selection failed", layer_names())
        print("PASS: create_layer under a floating selection reports where it landed")

        r = cmd('reorder_layer', {'layer_name': 'second', 'new_position': 4,
                                  'image_index': image_index()})
        check(r.get('status') == 'success', "reorder_layer failed", r)
        check(layer_names() == ['above-selected', 'under-float', 'top', 'bottom', 'second'],
              "reorder_layer misplaced", layer_names())
        print("PASS: reorder_layer still works")

        # GIMP refusing the insert: an error, the stack unchanged, the new layer freed.
        # Gimp.Layer.new is wrapped to record the IDs of the layers it creates.
        before = layer_names()
        py("_lpc_img.clean_all()",
           "_lpc_new = Gimp.Layer.new",
           "_lpc_ids = []",
           "def _lpc_record(*a):\n"
           "    _l = _lpc_new(*a)\n"
           "    if _l is not None:\n"
           "        _lpc_ids.append(_l.get_id())\n"
           "    return _l",
           "_lpc_orig = Gimp.Image.insert_layer")
        stubbed = ('Gimp.Image', 'insert_layer')
        py("Gimp.Image.insert_layer = lambda self, *a: False "
           "if self.get_id() == _lpc_img.get_id() else _lpc_orig(self, *a)",
           "Gimp.Layer.new = _lpc_record")
        try:
            r = cmd('create_layer', {'name': 'refused', 'image_index': image_index()})
        finally:
            py("Gimp.Image.insert_layer = _lpc_orig", "Gimp.Layer.new = _lpc_new")
            stubbed = None
        check(r.get('status') == 'error' and 'Inserting the layer failed' in r.get('error', ''),
              "a refused insert did not fail create_layer", r)
        check(layer_names() == before, "a refused insert changed the layers", layer_names())
        check(not is_dirty(), "a refused insert marked the image as changed")
        ids = json.loads(py("print(json.dumps(_lpc_ids))"))
        check(len(ids) == 1, "expected create_layer to make one layer", ids)
        leaked = json.loads(py("print(json.dumps([i for i in _lpc_ids "
                               "if Gimp.Item.id_is_valid(i)]))"))
        check(not leaked, "a refused insert leaked the layer", leaked)
        print("PASS: a refused insert fails create_layer, no change, no leaked layer")

        # GIMP refusing the move: an error, the stack unchanged.
        py("_lpc_img.clean_all()", "_lpc_orig = Gimp.Image.reorder_item")
        stubbed = ('Gimp.Image', 'reorder_item')
        py("Gimp.Image.reorder_item = lambda self, *a: False "
           "if self.get_id() == _lpc_img.get_id() else _lpc_orig(self, *a)")
        try:
            r = cmd('reorder_layer', {'layer_name': 'bottom', 'new_position': 0,
                                      'image_index': image_index()})
        finally:
            py("Gimp.Image.reorder_item = _lpc_orig")
            stubbed = None
        check(r.get('status') == 'error' and 'Moving the layer failed' in r.get('error', ''),
              "a refused move did not fail reorder_layer", r)
        check(layer_names() == before, "a refused move changed the layers", layer_names())
        check(not is_dirty(), "a refused move marked the image as changed")
        print("PASS: a refused move fails reorder_layer, no change")

        # An indexed image gets an indexed layer, which GIMP accepts.
        py("_lpc_img.convert_indexed(Gimp.ConvertDitherType.NONE,"
           " Gimp.ConvertPaletteType.GENERATE, 16, False, False, '')")
        r = cmd('create_layer', {'name': 'indexed', 'position': 0, 'fill': 'white',
                                 'image_index': image_index()})
        check(r.get('status') == 'success', "create_layer on an indexed image failed", r)
        check(layer_names() == ['indexed'] + before, "the indexed layer is misplaced",
              layer_names())
        check_reported(r, 0, None)
        check(py("print(_lpc_img.get_layers()[0].is_indexed())") == "True",
              "the new layer is not indexed")
        print("PASS: create_layer works on an indexed image")
        return 0
    finally:
        # The stubs replace methods for the whole plug-in, so try the restore again.
        if stubbed:
            try:
                py(f"{stubbed[0]}.{stubbed[1]} = _lpc_orig", "Gimp.Layer.new = _lpc_new")
            except (SystemExit, OSError):
                print(f"  (cleanup) could not restore {stubbed[0]}.{stubbed[1]}: "
                      "restart the MCP server in GIMP", file=sys.stderr)
        if saved_context:
            try:
                py("Gimp.context_set_opacity(_lpc_op)", "Gimp.context_set_paint_mode(_lpc_pm)")
            except (SystemExit, OSError):
                print("  (cleanup) could not restore the context's opacity and paint mode",
                      file=sys.stderr)
        if made_image:
            try:
                py("_lpc_img.delete()")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
