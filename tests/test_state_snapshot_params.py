#!/usr/bin/env python3
"""Tests for the parameters get_state_snapshot sends to the plugin.

- A negative max_size is rejected in the server, before connecting, with an
  error that names max_size rather than the plugin's max_width.
- max_size 0 means no scaling; otherwise it becomes max_width / max_height.
- A region's x / y / width / height become origin_x / origin_y / width / height.

No GIMP needed: a fake connection records what would be sent.
Run with the project environment (needs the mcp package): uv run tests/test_state_snapshot_params.py
Exits 0 on pass, 1 on fail.
"""
import base64
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))

import gimp_mcp_server as server  # noqa: E402

failures = []
SENT = []  # (command, params) the fake connection received


def check(name, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}: {name}" + ("" if condition else f" ({detail})"))
    if not condition:
        failures.append(name)


class FakeConnection:
    def send_command(self, command_type, params=None):
        SENT.append((command_type, params))
        return {"status": "success", "results": {"image_data": base64.b64encode(b"png").decode()}}


server.get_gimp_connection = lambda: FakeConnection()


def snapshot(**kwargs):
    """Call get_state_snapshot; return (params sent or None, error message or None)."""
    SENT.clear()
    try:
        server.get_state_snapshot(None, **kwargs)
        error = None
    except Exception as e:
        error = str(e)
    return (SENT[0][1] if SENT else None), error


for max_size in (-1, -100):
    params, error = snapshot(max_size=max_size)
    check(f"max_size {max_size} is rejected before anything is sent",
          params is None and error is not None
          and f"max_size must be at least 0 (0 means no scaling), got {max_size}" in error,
          f"sent {params}, error {error!r}")

params, error = snapshot(max_size=0)
check("max_size 0 sends no scaling bounds",
      error is None and params == {"image_index": 0}, f"sent {params}, error {error!r}")

params, error = snapshot(max_size=300, image_index=2)
check("max_size 300 becomes max_width / max_height",
      error is None and params == {"image_index": 2, "max_width": 300, "max_height": 300},
      f"sent {params}, error {error!r}")

params, error = snapshot(max_size=300, region={"x": 5, "y": 7, "width": 40, "height": 20})
check("a region's x / y / width / height become the plugin's region keys",
      error is None and params.get("region") == {"origin_x": 5, "origin_y": 7, "width": 40, "height": 20}
      and params.get("max_width") == 300, f"sent {params}, error {error!r}")

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
