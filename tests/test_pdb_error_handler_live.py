#!/usr/bin/env python3
"""Live test: the MCP server handles failed PDB calls itself.

By default GIMP shows a message for every PDB call that fails in a plug-in,
even when the plug-in checks the failure and reports it. The server process
sets the plug-in error handler, so a refused call only comes back as the tool's
error. Checks that:
- the server process uses Gimp.PDBErrorHandler.PLUGIN;
- a refused call still leaves GIMP's reason in get_last_error(), which the
  tools' error messages use (_check_ok);
- a tool whose GIMP call is refused returns an error;
- the tools whose GIMP calls failed unchecked (so only GIMP's message showed
  the failure) reject those values: new_canvas's resolution, draw_line's
  width, draw_rectangle's and draw_ellipse's line_width. Nothing is changed.

That GIMP shows no message can't be read back from a script. To see it by hand,
run Gimp.message_set_handler(Gimp.MessageHandlerType.CONSOLE) through call_api
and watch GIMP's stderr while this runs.

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


def main():
    made_image = False
    try:
        handler = py("print(Gimp.get_plug_in().get_pdb_error_handler() == "
                     "Gimp.PDBErrorHandler.PLUGIN)")
        check(handler == "True", "the server does not handle PDB errors itself")
        print("PASS: the server process uses the plug-in PDB error handler")

        py("import json",
           "_peh_img = Gimp.Image.new(10, 10, Gimp.ImageBaseType.RGB)",
           "_l = Gimp.Layer.new(_peh_img, 'a', 10, 10, Gimp.ImageType.RGB_IMAGE,"
           " 100, Gimp.LayerMode.NORMAL)",
           "_peh_img.insert_layer(_l, None, 0)")
        made_image = True

        # Converting an RGB image to RGB is refused; the reason must still be
        # readable. GIMP translates it, so only check that there is one.
        out = json.loads(py("_ok = _peh_img.convert_rgb()",
                            "print(json.dumps([_ok, Gimp.get_pdb().get_last_error()]))"))
        check(out[0] is False and out[1].strip(),
              "a refused call did not leave GIMP's reason in get_last_error()", out)
        print("PASS: a refused call returns False and GIMP's reason")

        index = int(py("print(next(i for i, im in enumerate(Gimp.get_images()) "
                       "if im.get_id() == _peh_img.get_id()))"))
        r = cmd('crop_to_rect', {'x': 5, 'y': 5, 'width': 50, 'height': 50,
                                 'image_index': index})
        check(r.get('status') == 'error' and 'Cropping the image failed' in r.get('error', ''),
              "a crop GIMP refuses did not fail the tool", r)
        print("PASS: a tool whose GIMP call is refused returns an error")

        n_images = py("print(len(Gimp.get_images()))")
        for res in (0, 1048577):
            r = cmd('new_canvas', {'width': 10, 'height': 10, 'resolution': res})
            check(r.get('status') == 'error' and 'resolution must be between' in r.get('error', ''),
                  f"new_canvas accepted resolution {res}", r)
            check(py("print(len(Gimp.get_images()))") == n_images,
                  f"new_canvas with resolution {res} left an image behind")
        print("PASS: new_canvas rejects an out-of-range resolution")

        py("_peh_img.select_rectangle(Gimp.ChannelOps.REPLACE, 1, 1, 3, 3)", "_peh_img.clean_all()")
        index = int(py("print(next(i for i, im in enumerate(Gimp.get_images()) "
                       "if im.get_id() == _peh_img.get_id()))"))
        line = {'x1': 0, 'y1': 0, 'x2': 9, 'y2': 9, 'image_index': index}
        shape = {'x': 1, 'y': 1, 'width': 5, 'height': 5, 'image_index': index}
        for tool, params, needle in (
                ('draw_line', {**line, 'width': 0}, 'width must be between 1 and 10000'),
                ('draw_line', {**line, 'width': 10001}, 'width must be between 1 and 10000'),
                ('draw_rectangle', {**shape, 'line_width': 0}, 'line_width must be more than 0'),
                ('draw_rectangle', {**shape, 'line_width': 2001}, 'line_width must be more than 0'),
                ('draw_ellipse', {**shape, 'line_width': 2001}, 'line_width must be more than 0')):
            r = cmd(tool, params)
            check(r.get('status') == 'error' and needle in r.get('error', ''),
                  f"{tool} accepted {params}", r)
            check(py("print(_peh_img.is_dirty())") == "False", f"{tool} {params} changed the image")
        b = json.loads(py("_b = Gimp.Selection.bounds(_peh_img)",
                          "print(json.dumps([_b[2], _b[3], _b[4], _b[5]]))"))
        check(b == [1, 1, 4, 4], "a rejected stroke changed the selection", b)
        print("PASS: draw_line, draw_rectangle and draw_ellipse reject out-of-range widths")
        return 0
    finally:
        if made_image:
            try:
                py("_peh_img.delete()")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
