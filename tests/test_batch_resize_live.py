#!/usr/bin/env python3
"""Live test for batch_resize (#63).

With no width, height or scale_factor it used to "resize" every image to its
own size, mark it as changed and report success. Checks that:
- with no size, bad values or no open images it returns an error, and no
  image is resized or marked as changed;
- a result above GIMP's size limit for the last image scaled resizes no image
  at all, not even the ones scaled before it;
- width alone keeps the aspect ratio; width and height fit each image inside
  the box; maintain_aspect=false and scale_factor work;
- an image already at its target size is left untouched (scaled: false, not
  marked as changed).

batch_resize resizes every open image, so this needs GIMP with no images open
(it prints SKIP otherwise). Requires the GIMP MCP plugin running on
localhost:9877 (GIMP_MCP_PORT overrides the port). Exits 0 on pass or skip,
1 on fail.
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


def sizes():
    """[[width, height, dirty], ...] of the two test images."""
    return json.loads(py("print(__import__('json').dumps([[im.get_width(), im.get_height(), "
                         "im.is_dirty()] for im in (_br_a, _br_b)]))"))


def resize(**params):
    return cmd('batch_resize', params)


def expect_error(params, words, before):
    r = resize(**params)
    check(r.get('status') == 'error' and words in r.get('error', ''),
          f"batch_resize({params}) did not fail with '{words}'", r)
    check(sizes() == before, f"batch_resize({params}) changed an image", sizes())


def main():
    if py("print(len(Gimp.get_images()))") != "0":
        print("SKIP: batch_resize resizes every open image; needs GIMP with no images open")
        return 0
    made_images = False
    try:
        py("_br_a = _br_b = _br_c = None")
        made_images = True
        py("def _br_new(w, h):\n"
           "    im = Gimp.Image.new(w, h, Gimp.ImageBaseType.RGB)\n"
           "    im.insert_layer(Gimp.Layer.new(im, 'bg', w, h, Gimp.ImageType.RGB_IMAGE,"
           " 100, Gimp.LayerMode.NORMAL), None, 0)\n"
           "    im.clean_all()\n"
           "    return im",
           # GIMP lists (and batch_resize scales) the newest image first, so
           # _br_c, made first, comes last.
           "_br_c = _br_new(60, 60)", "_br_a = _br_new(40, 30)", "_br_b = _br_new(20, 40)")
        order = py("print([im.get_id() for im in Gimp.get_images()] == "
                   "[_br_b.get_id(), _br_a.get_id(), _br_c.get_id()])")
        check(order == "True", "test setup: GIMP doesn't list the newest image first")
        start = [[40, 30, False], [20, 40, False]]
        check(sizes() == start, "test images not as made", sizes())

        expect_error({}, 'needs width, height or scale_factor', start)
        expect_error({'width': None, 'height': None, 'scale_factor': None},
                     'needs width, height or scale_factor', start)
        for bad, words in [({'width': 0}, 'above 0'), ({'height': -5}, 'above 0'),
                           ({'width': 10.5}, 'whole number'), ({'width': 'abc'}, 'a number'),
                           ({'width': True}, 'a number'), ({'scale_factor': 0}, 'above 0'),
                           ({'scale_factor': float('nan')}, 'above 0'),
                           ({'scale_factor': 20000}, 'no image was resized')]:
            expect_error(bad, words, start)
        # 524288 / 10000 = 52: the 40-pixel images, scaled first, would fit; the
        # 60x60 one, scaled last, wouldn't. None may be resized.
        c_before = py("print(_br_c.get_width(), _br_c.get_height(), _br_c.is_dirty())")
        expect_error({'scale_factor': 10000}, 'no image was resized', start)
        check(py("print(_br_c.get_width(), _br_c.get_height(), _br_c.is_dirty())") == c_before,
              "the too-large image changed", c_before)
        py("_br_c.delete()", "_br_c = None")
        print("PASS: no size, bad values and a too-large result are errors; nothing changes")

        r = resize(width=80)
        check(r.get('status') == 'success', "width=80 failed", r)
        check(sizes() == [[80, 60, True], [80, 160, True]], "width=80 gave wrong sizes", sizes())
        check([x['scaled'] for x in r['results']['results']] == [True, True],
              "scaled flags wrong", r)
        r = resize(width=40, height=40)
        check(r.get('status') == 'success', "a 40x40 box failed", r)
        check([s[:2] for s in sizes()] == [[40, 30], [20, 40]],
              "the images were not fitted inside 40x40", sizes())
        print("PASS: width keeps the aspect ratio; width and height fit each image in the box")

        py("_br_a.clean_all()", "_br_b.clean_all()")
        r = resize(width=40, height=40)
        check(r.get('status') == 'success', "the same 40x40 box failed", r)
        check([x['scaled'] for x in r['results']['results']] == [False, False],
              "images already at their size were scaled", r)
        check(sizes() == [[40, 30, False], [20, 40, False]],
              "images already at their size were changed or marked dirty", sizes())
        print("PASS: an image already at its target size is left untouched")

        r = resize(width=10, height=10, maintain_aspect=False)
        check(r.get('status') == 'success' and [s[:2] for s in sizes()] == [[10, 10], [10, 10]],
              "maintain_aspect=false did not give 10x10", (r, sizes()))
        r = resize(scale_factor=2)
        check(r.get('status') == 'success' and [s[:2] for s in sizes()] == [[20, 20], [20, 20]],
              "scale_factor=2 did not double the size", (r, sizes()))
        print("PASS: maintain_aspect=false and scale_factor work")

        py("_br_a.delete()", "_br_b.delete()", "_br_a = _br_b = None")
        r = resize(width=10)
        check(r.get('status') == 'error' and 'no images are open' in r.get('error', ''),
              "with no images open it did not fail", r)
        print("PASS: with no images open it returns an error")
        return 0
    finally:
        if made_images:
            try:
                py("for _im in (_br_a, _br_b, _br_c):\n"
                   "    if _im is not None and _im.is_valid():\n"
                   "        _im.delete()")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test images", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
