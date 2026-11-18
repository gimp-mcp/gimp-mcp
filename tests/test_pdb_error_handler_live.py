#!/usr/bin/env python3
"""Live test: the MCP server handles failed PDB calls itself.

By default GIMP shows a message for every PDB call that fails in a plug-in,
even when the plug-in checks the failure and reports it. The server process
sets the plug-in error handler, so a refused call only comes back as the tool's
error. Checks that:
- the server process uses Gimp.PDBErrorHandler.PLUGIN;
- a refused call still leaves GIMP's reason in get_last_error(), which the
  tools' error messages use (_check_ok);
- a tool whose GIMP call is refused returns an error.

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

        py("_peh_img = Gimp.Image.new(10, 10, Gimp.ImageBaseType.RGB)",
           "_l = Gimp.Layer.new(_peh_img, 'a', 10, 10, Gimp.ImageType.RGB_IMAGE,"
           " 100, Gimp.LayerMode.NORMAL)",
           "_peh_img.insert_layer(_l, None, 0)")
        made_image = True

        # Converting an RGB image to RGB is refused; the reason must still be readable.
        out = py("print(_peh_img.convert_rgb())", "print(Gimp.get_pdb().get_last_error())")
        check(out.startswith("False") and "must not be of type 'rgb'" in out,
              "a refused call did not leave GIMP's reason in get_last_error()", out)
        print("PASS: a refused call returns False and GIMP's reason")

        index = int(py("print(next(i for i, im in enumerate(Gimp.get_images()) "
                       "if im.get_id() == _peh_img.get_id()))"))
        r = cmd('crop_to_rect', {'x': 5, 'y': 5, 'width': 50, 'height': 50,
                                 'image_index': index})
        check(r.get('status') == 'error' and 'Cropping the image failed' in r.get('error', ''),
              "a crop GIMP refuses did not fail the tool", r)
        print("PASS: a tool whose GIMP call is refused returns an error")
        return 0
    finally:
        if made_image:
            try:
                py("_peh_img.delete()")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
