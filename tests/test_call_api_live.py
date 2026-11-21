#!/usr/bin/env python3
"""Live test for call_api's args (#63).

The docs name the eval mode "pyGObject-eval", but the plugin only knew
"python-fu-eval": the documented spelling ran the expression as a statement
and returned "". "python-fu-eval" evaluated in the plugin's own module, so it
couldn't see variables earlier commands set. A string instead of a list of
code strings ran each of its characters, missing code failed with "list index
out of range", and an unknown command type failed with "'args'". Checks that:
- "pyGObject-eval" and "python-fu-eval" return the values of the expressions,
  and see variables earlier commands set and the modules the plugin imports;
- "pyGObject-console", and any mode other than the eval modes, run code and
  return what it prints; api_path and kwargs make no difference;
- malformed args are an error that says what args should be, and run nothing;
- a string that raises is an error, after the strings before it have run;
- an unknown command type is an error naming it, unless its params have
  args: clients send code under other type names, so those still run.

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


def call_api(args, api_path='exec', kwargs=None):
    """Send a call_api command the way the MCP server does."""
    return send({'type': 'call_api',
                 'params': {'api_path': api_path, 'args': args, 'kwargs': kwargs or {}}})


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def check(cond, msg, detail=None):
    if not cond:
        fail(f"{msg}: {detail}" if detail is not None else msg)


def results(r, what):
    check(r.get('status') == 'success', f"{what} failed", r)
    return r.get('results')


def main():
    made = False
    try:
        results(call_api(['pyGObject-console', ['_ca_x = 21', '_ca_ran = []']]),
                'setting a variable')
        made = True

        for mode in ('pyGObject-eval', 'python-fu-eval'):
            got = results(call_api([mode, ['1 + 2', '_ca_x * 2', "'a' + 'b'"]]), mode)
            check(got == ['3', '42', 'ab'], f"{mode} did not return the values", got)
        # python-fu-eval used to evaluate in the plugin's module and see its imports.
        got = results(call_api(['python-fu-eval', ['json.dumps([1])', 'platform.system() != ""',
                                                   'GLib.MAXINT > 0', 'math.floor(2.5)']]),
                      "eval with the plugin's imports")
        check(got == ['[1]', 'True', 'True', '2'], "eval does not see the plugin's imports", got)
        print("PASS: both eval modes return values and see earlier variables and imports")

        got = results(call_api(['pyGObject-console',
                                ['print(_ca_x)', '_ca_y = 1', 'print(1)\nprint(2)']]),
                      'pyGObject-console')
        check(got == ['21\n', '', '1\n2\n'], "pyGObject-console did not return the output", got)
        for mode in ('python-fu-exec', 'exec', 'anything'):
            got = results(call_api([mode, [f"_ca_ran.append({mode!r})", "print('ran')"]]), mode)
            check(got == ['', 'ran\n'], f"mode {mode!r} did not run the code", got)
        got = results(call_api(['pyGObject-console', ['print(_ca_x)']], api_path='nonsense',
                               kwargs={'x': 1}), 'junk api_path and kwargs')
        check(got == ['21\n'], "api_path or kwargs changed the result", got)
        got = results(send({'cmds': ['print(_ca_x + 1)']}), 'cmds')
        check(got == ['22\n'], "a cmds request did not run", got)
        print("PASS: other modes run code; api_path and kwargs make no difference")

        bad = [
            "_ca_ran.append('string')",                       # not a list
            ['pyGObject-console'],                            # no code
            ['pyGObject-console', "_ca_ran.append('str')"],   # code is a string
            ['pyGObject-console', ["_ca_ran.append('mixed')", 5]],
            ['pyGObject-eval', ["_ca_ran.append('eval')", None]],
            [5, ["_ca_ran.append('mode')"]],
            ['pyGObject-console', ["_ca_ran.append('extra')"], 'extra'],
            [],
            None,
        ]
        for args in bad:
            r = call_api(args)
            check(r.get('status') == 'error' and 'args must be [mode, [code, ...]]'
                  in r.get('error', ''), f"args {args!r} were not refused", r)
        for msg in ({'cmds': "_ca_ran.append('cmds')"}, {'type': 'call_api'},
                    {'type': 'call_api', 'params': []}):
            r = send(msg)
            check(r.get('status') == 'error' and 'args must be' in r.get('error', ''),
                  f"{msg!r} was not refused", r)
        got = results(call_api(['pyGObject-eval', ['_ca_ran']]), 'reading _ca_ran')
        check(got == ["['python-fu-exec', 'exec', 'anything']"],
              "a refused call ran some of its code", got)
        print("PASS: malformed args are an error and run nothing")

        # A string that raises ends the call: the strings before it have run.
        r = call_api(['pyGObject-console', ["_ca_ran.append('before')", "print('lost')",
                                            '1 / 0', "_ca_ran.append('after')"]])
        check(r.get('status') == 'error' and 'division by zero' in r.get('error', ''),
              "a raising command was not an error", r)
        got = results(call_api(['pyGObject-eval', ['_ca_ran[-1]']]), 'reading _ca_ran')
        check(got == ['before'], "the strings around a raising one did not run as documented", got)
        r = call_api(['pyGObject-eval', ['1 + 1', 'undefined_name_ca']])
        check(r.get('status') == 'error' and 'undefined_name_ca' in r.get('error', ''),
              "a failing expression was not an error", r)
        print("PASS: a string that raises is an error; the strings before it have run")

        for msg in ({'type': 'bogus_tool', 'params': {'width': 5}}, {'type': 'bogus_tool'}):
            r = send(msg)
            check(r.get('status') == 'error'
                  and r.get('error') == "Unknown command type 'bogus_tool'",
                  f"{msg!r} did not fail naming its type", r)
        # Clients send code under other type names (run_tests.py uses "exec").
        got = results(send({'type': 'exec', 'params': {'args': ['python-fu-exec',
                                                                ['print(_ca_x)']]}}),
                      "type 'exec'")
        check(got == ['21\n'], "a request of type 'exec' with args did not run", got)
        print("PASS: an unknown command type is an error naming it, unless it has args")
        return 0
    finally:
        if made:
            try:
                call_api(['pyGObject-console', ["for _n in ('_ca_x', '_ca_ran', '_ca_y'):\n"
                                                "    globals().pop(_n, None)"]])
            except OSError:
                print("  (cleanup) could not delete the test variables", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
