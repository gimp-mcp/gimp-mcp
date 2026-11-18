#!/usr/bin/env python3
"""Live test for convert_color_mode (#63).

INDEXED used to crash: the plugin asked for Gimp.ConvertDitherType.NO_DITHER,
which doesn't exist (the value is NONE). Checks that:
- INDEXED converts an RGB image with the requested number of colors;
- converting to the mode the image already has succeeds and changes nothing
  (GIMP's convert call returns False then, which used to be ignored for RGB and
  GRAY and would now be an error);
- RGBA and GRAYA convert and add alpha to every layer;
- an out-of-range num_colors, an unknown mode, and an image with a layer group
  (which GIMP's PDB refuses to index) return an error and leave the image as it was.

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
                  "if im.get_id() == _ccm_img.get_id()))"))


def convert(mode, **params):
    return cmd('convert_color_mode', {'mode': mode, 'image_index': image_index(), **params})


def base_type():
    return py("print(_ccm_img.get_base_type().value_nick)")


def main():
    made_image = False
    try:
        py("import json",
           "_ccm_img = Gimp.Image.new(40, 30, Gimp.ImageBaseType.RGB)",
           "_l = Gimp.Layer.new(_ccm_img, 'bg', 40, 30, Gimp.ImageType.RGB_IMAGE,"
           " 100, Gimp.LayerMode.NORMAL)",
           "_ccm_img.insert_layer(_l, None, 0)",
           "_l.fill(Gimp.FillType.WHITE)",
           "_ccm_img.select_rectangle(Gimp.ChannelOps.REPLACE, 0, 0, 20, 30)",
           "Gimp.Drawable.edit_fill(_l, Gimp.FillType.FOREGROUND)",
           "Gimp.Selection.none(_ccm_img)")
        made_image = True

        for mode, params, needle in (('INDEXED', {'num_colors': 0}, 'between 1 and 256'),
                                     ('INDEXED', {'num_colors': 257}, 'between 1 and 256'),
                                     ('CMYK', {}, 'Unknown mode')):
            r = convert(mode, **params)
            check(r.get('status') == 'error' and needle in r.get('error', ''),
                  f"convert_color_mode({mode}, {params}) was not rejected", r)
            check(base_type() == 'rgb', "a rejected conversion changed the image", base_type())
        print("PASS: bad num_colors and unknown modes are rejected")

        # RGB -> RGB is a no-op, not an error.
        py("_ccm_img.clean_all()")
        r = convert('RGB')
        check(r.get('status') == 'success', "RGB on an RGB image failed", r)
        check(py("print(_ccm_img.is_dirty())") == "False", "RGB on an RGB image changed it")
        print("PASS: converting to the current mode is a no-op")

        # A layer group blocks INDEXED with a clear error.
        py("_g = Gimp.GroupLayer.new(_ccm_img, 'grp')", "_ccm_img.insert_layer(_g, None, 0)")
        r = convert('INDEXED', num_colors=4)
        check(r.get('status') == 'error' and 'layer groups' in r.get('error', ''),
              "INDEXED with a layer group did not explain the refusal", r)
        check(base_type() == 'rgb', "the refused conversion changed the image", base_type())
        py("_ccm_img.remove_layer(_g)")
        print("PASS: INDEXED with a layer group is refused with a clear error")

        r = convert('INDEXED', num_colors=4)
        check(r.get('status') == 'success', "INDEXED failed", r)
        check(base_type() == 'indexed', "the image is not indexed", base_type())
        ncolors = int(py("print(_ccm_img.get_palette().get_color_count())"))
        check(1 <= ncolors <= 4, "the palette does not have at most 4 colors", ncolors)
        r = convert('INDEXED', num_colors=4)
        check(r.get('status') == 'success', "INDEXED on an indexed image failed", r)
        print(f"PASS: INDEXED converts ({ncolors} colors); again is a no-op")

        for mode, nick in (('GRAYA', 'gray'), ('RGBA', 'rgb')):
            r = convert(mode)
            check(r.get('status') == 'success', f"{mode} failed", r)
            check(base_type() == nick, f"{mode} gave the wrong base type", base_type())
            check(py("print(all(l.has_alpha() for l in _ccm_img.get_layers()))") == "True",
                  f"{mode} did not add alpha")
        print("PASS: GRAYA and RGBA convert and add alpha")
        return 0
    finally:
        if made_image:
            try:
                py("_ccm_img.delete()")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
