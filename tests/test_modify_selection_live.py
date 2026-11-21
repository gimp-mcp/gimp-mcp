#!/usr/bin/env python3
"""Live test for modify_selection's result and amount checks (#63).

modify_selection reported plain success even when it left nothing selected
(a shrink larger than the selection), and when GIMP refused the amount: a
negative grow changed nothing. grow/shrink/border take whole pixels, and 2.7
was truncated to 2. Checks that:
- the result has has_selection and, while something is selected, the bounds;
  a shrink that empties the selection and an operation on no selection are a
  success with has_selection false;
- a negative, fractional (grow/shrink/border), non-finite or non-numeric
  amount is an error and leaves the selection as it was; feather takes a
  fractional radius; sharpen ignores the amount;
- an unknown operation is an error listing the valid ones.

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


def modify(operation, amount):
    index = int(py("print([i.get_id() for i in Gimp.get_images()].index(_MS.get_id()))"))
    return send({'type': 'modify_selection',
                 'params': {'operation': operation, 'amount': amount, 'image_index': index}})


def select_square():
    """Select (10, 10)-(30, 30); feather off so the bounds are exact."""
    py("Gimp.context_push()\n"
       "try:\n"
       "    Gimp.context_set_feather(False)\n"
       "    _MS.select_rectangle(Gimp.ChannelOps.REPLACE, 10, 10, 20, 20)\n"
       "finally:\n"
       "    Gimp.context_pop()")


def bounds():
    return py("_b = Gimp.Selection.bounds(_MS)", "print([bool(_b[1])] + list(_b[2:]))")


def main():
    made = False
    try:
        py("_MS = Gimp.Image.new(100, 80, Gimp.ImageBaseType.RGB)")
        made = True
        py("_MS.insert_layer(Gimp.Layer.new(_MS, 'bg', 100, 80, Gimp.ImageType.RGB_IMAGE, 100,"
           " Gimp.LayerMode.NORMAL), None, 0)")

        select_square()
        r = modify('grow', 2)
        check(r.get('status') == 'success'
              and r['results'] == {'status': 'success', 'has_selection': True,
                                   'x': 8, 'y': 8, 'width': 24, 'height': 24},
              "grow 2 didn't report the new bounds", r)
        select_square()
        r = modify('shrink', 15)
        check(r.get('status') == 'success' and r['results'].get('has_selection') is False
              and 'x' not in r['results'], "a shrink that empties the selection isn't reported", r)
        r = modify('grow', 5)
        check(r.get('status') == 'success' and r['results'].get('has_selection') is False,
              "grow on no selection isn't reported as has_selection false", r)
        print("PASS: the result has has_selection and the bounds")

        select_square()
        before = bounds()
        for operation, amount, error in (
                ('grow', -5, 'amount must be between 0 and'),
                ('shrink', -1, 'amount must be between 0 and'),
                ('feather', -0.5, 'amount must be between 0 and'),
                ('grow', 2.7, 'amount must be a whole number'),
                ('border', 1.5, 'amount must be a whole number'),
                ('grow', True, 'amount must be a whole number'),
                ('grow', 'big', 'amount must be a whole number'),
                ('grow', 2 ** 31, 'amount must be between 0 and'),
                ('feather', 'nan', 'amount must be a number'),
                ('feather', 'inf', 'amount must be a number'),
                ('feather', 'soft', 'amount must be a number'),
                ('feather', False, 'amount must be a number'),
                ('bogus', 1, "Unknown operation 'bogus': use one of 'grow'")):
            r = modify(operation, amount)
            check(r.get('status') == 'error' and error in r.get('error', ''),
                  f"{operation} {amount!r} was not refused", r)
            check(bounds() == before, f"{operation} {amount!r} changed the selection",
                  (before, bounds()))
        print("PASS: bad amounts and operations are errors and leave the selection")

        for operation, amount in (('feather', 2.5), ('grow', 3.0), ('shrink', '2'),
                                  ('sharpen', 'ignored'), ('GROW', 0)):
            select_square()
            r = modify(operation, amount)
            check(r.get('status') == 'success' and r['results'].get('has_selection') is True,
                  f"{operation} {amount!r} failed", r)
        print("PASS: fractional feather, whole-number floats, digit strings and sharpen work")
        return 0
    finally:
        if made:
            try:
                py("_MS.delete() if _MS.is_valid() else None")
            except (SystemExit, OSError):
                print("  (cleanup) could not delete the test image", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
