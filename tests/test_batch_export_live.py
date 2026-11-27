#!/usr/bin/env python3
"""Live checks for batch_export failures and output files (#63, #161).

batch_export reported success with exported: [] when every image failed, and
a bad name_pattern failed for each image with a bare "'bogus'" (also a
success). Checks that:
- exporting only an image that can't be exported (no layers) is an error
  that gives GIMP's reason, and writes nothing;
- a name_pattern with a field other than {name} and {index} (also an
  indexed or attribute one, such as {name[3]}, or one in a format spec) or
  bad syntax is an error naming it, and writes nothing;
- one image failing among others still succeeds, listing it in errors.
- two source files with the same base name are rejected before any writes.
- {index} produces distinct paths and allows overwriting an existing export.

The last checks export every open image, so they run only when the test's own
two images are the only ones open (it prints SKIP otherwise); the files go to
a temporary directory that is deleted again.

Requires the GIMP MCP plugin running on localhost:9877 (GIMP_MCP_PORT
overrides the port). Exits 0 on pass, 1 on fail.
"""
import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

HOST, PORT = '127.0.0.1', int(os.environ.get('GIMP_MCP_PORT', 9877))


def send(msg, timeout=300):
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


def index(var):
    return int(py(f"print([i.get_id() for i in Gimp.get_images()].index({var}.get_id()))"))


def files(path):
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


def check_duplicate_names(out_dir, made):
    """Checks collision rejection and the indexed naming workaround."""
    source = Path(out_dir) / 'source'
    fixtures = (
        ('_BE_PNG', source / 'a' / 'photo.png', 'png'),
        ('_BE_JPEG', source / 'b' / 'photo.jpg', 'jpeg'),
    )
    for _, path, fmt in fixtures:
        path.parent.mkdir(parents=True)
        r = cmd('export_image', {'file_path': str(path), 'format': fmt,
                                 'image_index': index('_BE_OK')})
        check(r.get('status') == 'success', 'could not create a source file', r)
    py('_BE_EMPTY.delete()', '_BE_OK.delete()', 'from gi.repository import Gio')
    for var, path, _ in fixtures:
        py(f'{var} = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, '
           f'Gio.File.new_for_path({str(path)!r}))')
        check(py(f'print({var} is not None)') == 'True', 'could not load the source image', path)
        made.append(var)
    indices = [index(var) for var, _, _ in fixtures]

    target = Path(out_dir) / 'duplicate'
    r = cmd('batch_export', {'output_dir': str(target)})
    error = r.get('error', '')
    check(r.get('status') == 'error' and 'photo.png' in error
          and '{index}' in error
          and all(f"image {idx} ('photo')" in error for idx in indices),
          'duplicate output paths were not rejected with both image indices and a workaround', r)
    check(not target.exists(), 'the rejected batch created its output directory')
    print('PASS: conflicting destinations are rejected before writing')

    target.mkdir()
    expected = {target / f'photo_{idx}.png' for idx in indices}
    for path in expected:
        path.write_bytes(b'old export')
    r = cmd('batch_export', {'output_dir': str(target), 'name_pattern': '{name}_{index}'})
    check(r.get('status') == 'success', 'unique destinations were refused', r)
    result = r['results']
    check(result['count'] == 2 and result['errors'] == []
          and {Path(item['file_path']) for item in result['exported']} == expected,
          'the indexed batch did not report both destinations', result)
    check(set(target.iterdir()) == expected
          and all(path.read_bytes().startswith(b'\x89PNG\r\n\x1a\n') for path in expected),
          'both destinations should contain new PNG exports', files(target))
    print('PASS: unique destinations export both images and replace existing files')


def main():
    out_dir = tempfile.mkdtemp(prefix='gimp-mcp-batch-export-')
    made = []
    try:
        py("_BE_EMPTY = Gimp.Image.new(10, 10, Gimp.ImageBaseType.RGB)")
        made.append('_BE_EMPTY')
        py("_BE_OK = Gimp.Image.new(10, 10, Gimp.ImageBaseType.RGB)")
        made.append('_BE_OK')
        py("_BE_OK.insert_layer(Gimp.Layer.new(_BE_OK, 'bg', 10, 10, Gimp.ImageType.RGB_IMAGE, 100,"
           " Gimp.LayerMode.NORMAL), None, 0)")

        target = os.path.join(out_dir, 'one')
        r = cmd('batch_export', {'output_dir': target, 'image_index': index('_BE_EMPTY')})
        check(r.get('status') == 'error'
              and f"Nothing was exported: image {index('_BE_EMPTY')}: " in r.get('error', '')
              and 'no layers' in r.get('error', ''),
              "exporting only a layerless image was not an error giving GIMP's reason", r)
        check(files(target) == [], "the failed export wrote files", files(target))
        print("PASS: nothing exported is an error with each image's reason")

        target = os.path.join(out_dir, 'pattern')
        for pattern, detail in (('{bogus}', "unknown field 'bogus'"), ('{name', 'ValueError'),
                                ('{0}', "unknown field '0'"), ('{}', "unknown field ''"),
                                ('{name[3]}', "unknown field 'name[3]'"),
                                ('{name.upper}', "unknown field 'name.upper'"),
                                ('{index:{bogus}}', "unknown field 'bogus'"),
                                ('{name:d}', 'ValueError'), (5, 'TypeError')):
            r = cmd('batch_export', {'output_dir': target, 'name_pattern': pattern})
            check(r.get('status') == 'error'
                  and f"Bad name_pattern {pattern!r} ({detail}" in r.get('error', '')
                  and '{name} and {index}' in r.get('error', ''),
                  f"name_pattern {pattern!r} was not refused clearly", r)
        check(files(target) == [], "a bad name_pattern wrote files", files(target))
        print("PASS: a bad name_pattern is an error and writes nothing")

        if py("print(len(Gimp.get_images()))") != "2":
            print("SKIP: multi-image cases: batch_export without image_index exports every "
                  "open image; needs GIMP with no other images open")
            return 0
        target = os.path.join(out_dir, 'all')
        r = cmd('batch_export', {'output_dir': target, 'name_pattern': 'be_{index:d}'})
        check(r.get('status') == 'success', "exporting with one bad image among others failed", r)
        failed = [e.get('index') for e in r['results'].get('errors', [])]
        check(index('_BE_EMPTY') in failed, "the layerless image isn't in errors", r['results'])
        check(f"be_{index('_BE_OK')}.png" in files(target), "the good image wasn't exported",
              files(target))
        print("PASS: some images exported is a success that lists the failed ones")
        check_duplicate_names(out_dir, made)
        return 0
    finally:
        for var in made:
            try:
                py(f"{var}.delete() if {var}.is_valid() else None")
            except (SystemExit, OSError):
                print(f"  (cleanup) could not delete {var}", file=sys.stderr)
        shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
