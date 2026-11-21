#!/usr/bin/env python3
"""Tests for the plug-in's socket transport (#63).

The client handler read until the whole buffer parsed as JSON, so:
- a request that isn't JSON (or isn't UTF-8) got no reply: the handler waited
  for more bytes forever, holding its thread;
- two requests sent together never parsed as one document: neither got a
  reply;
- a client that connected and sent nothing, or half a request, held its
  thread forever (no timeout);
- (found while fixing these) under pythonw, where sys.stdout is None, a
  stdout router writing to None would kill the accept thread; parsing the
  whole buffer after every read made large requests quadratic; closing with
  unread input reset the connection and could lose the error reply;
- (while fixing these) reading on after something that isn't a request
  would run the JSON body of an HTTP POST a web page sends to this port: the
  connection must end at the first malformed request;
- a reply json couldn't encode killed the handler thread, and the client
  waited for a reply that never came;
- exec_and_get_results swapped sys.stdout for the whole process: log lines
  printed by other threads ("Connected to client: ...") ended up in a
  command's results, and a command that raised left sys.stdout swapped;
- "disable_auto_disconnect" set a flag for every client, and the handler
  still served one request per connection.

Runs a real server (accept thread + client threads) on a free localhost port,
with gi.repository stubbed so the plug-in imports without GIMP. Requests use
the "cmds" path (Python exec), which needs no GIMP.

Run with: uv run tests/test_transport.py. Exits 0 on pass, 1 on fail.
"""
import io
import json
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from test_menu_check_restart import (free_port, install_gi_stub, load_plugin,  # noqa: E402
                                     start_server, stop_server)

failures = []


def check(name, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}: {name}"
          f"{f' ({detail})' if detail and not condition else ''}")
    if not condition:
        failures.append(name)


def cmds(*lines):
    return json.dumps({"cmds": list(lines)}).encode()


def read_replies(sock, count, timeout=5.0):
    """Read newline-terminated replies until count arrive, the server closes, or timeout."""
    sock.settimeout(timeout)
    buf = b""
    deadline = time.monotonic() + timeout
    try:
        while buf.count(b"\n") < count and time.monotonic() < deadline:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    except OSError:  # a timeout, or the server resetting the connection
        pass
    return [json.loads(line) for line in buf.split(b"\n") if line.strip()]


def closed_by_server(sock, timeout=2.0):
    """True if the server closes the connection (recv returns b'') within timeout."""
    sock.settimeout(timeout)
    try:
        return sock.recv(1) == b""
    except socket.timeout:
        return False
    except OSError:
        return True


def exchange(port, data, count=1, timeout=5.0, shut_write=False):
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        try:
            s.sendall(data)
        except OSError:
            return []
        if shut_write:
            s.shutdown(socket.SHUT_WR)
        return read_replies(s, count, timeout)


def test_next_request(mod):
    nr = mod.next_request
    req, err, rest = nr(b'{"a": 1}\n{"b": 2}\n')
    check("next_request splits the first of two requests",
          (req, err, rest) == ('{"a": 1}', None, b'\n{"b": 2}\n'), (req, err, rest))
    check("a request without a newline is complete", nr(b'  {"a": 1}')[:2] == ('{"a": 1}', None))
    for partial in (b'{"cmds": ["pri', b'{"a": 1', b'{\n  "a": tru', b'{"a":\n', b'{"a": -'):
        check(f"{partial!r} is waited for", nr(partial) == (None, None, partial), nr(partial))
    req, err, rest = nr(b'{this is not json}\n{"a": 1}')
    check("a line that isn't JSON is an error, and the next line is kept",
          req is None and "not JSON" in err["error"] and rest == b'{"a": 1}', (err, rest))
    req, err, rest = nr(b'{"a": 1')
    check("half a request is waited for", (req, err) == (None, None))
    req, err, rest = nr(b'{"a": 1', eof=True)
    check("half a request at the end of the stream is an error",
          req is None and "not JSON" in err["error"], err)
    req, err, rest = nr(b'\xff\xfe{"a": 1}\n{"b": 2}')
    check("bytes that aren't UTF-8 are an error, and the next line is kept",
          req is None and "not UTF-8" in err["error"] and rest == b'{"b": 2}', (err, rest))
    req, err, rest = nr(b'{"a": 1}\n\xff\n')
    check("a request before bytes that aren't UTF-8 comes first",
          (req, err, rest) == ('{"a": 1}', None, b'\n\xff\n'), (req, err, rest))
    euro = '{"s": "€"}'.encode()
    check("a character cut by a read is waited for", nr(euro[:-3]) == (None, None, euro[:-3]))
    check("... and parsed once complete", nr(euro)[0] == '{"s": "€"}')
    check("the keep-alive request is recognised",
          nr(b"disable_auto_disconnect\n{")[:2] == ("disable_auto_disconnect", None))
    check("only whitespace is nothing", nr(b" \r\n\t ") == (None, None, b""))
    # The handler keeps its buffer in a bytearray (adding to bytes copies it).
    for data in (b'{"a": 1}\n{"b": 2}\n', b'{"a": 1', b'{this is not json}\n{"a": 1}',
                 b'\xff\xfe{"a": 1}\n{"b": 2}', b"disable_auto_disconnect\n{", b"  "):
        check(f"bytearray({data!r}) is split like bytes",
              nr(bytearray(data)) == nr(data), (nr(bytearray(data)), nr(data)))
    for line in (b'{"a": 1}oops\n{"b": 2}', b'{"a": 1} {"b": 2}\n', b'{"a": 1}\xff\n'):
        req, err, rest = nr(line)
        check(f"{line!r}: text after the object on its line is an error",
              req is None and err is not None, (req, err, rest))
    for value in (b"[]", b'"text"', b"42", b"null"):
        req, err, rest = nr(value + b'\n{"a": 1}')
        check(f"JSON {value!r} (not an object) is an error",
              req is None and "not an object" in err["error"], (req, err))


def test_stdout_capture(mod):
    """Captures only the command's own output, and releases it even when the command raises."""
    real = sys.stdout
    default = io.StringIO()
    sys.stdout = mod._ThreadStdout(default)
    try:
        out = mod.exec_and_get_results(
            "import sys\nprint('a')\nsys.stdout.writelines(['b\\n', 'c\\n'])", {})
        try:
            mod.exec_and_get_results("print('lost')\nraise ValueError('boom')", {})
            raised = False
        except ValueError:
            raised = True
        print("after")
    finally:
        sys.stdout = real
    check("print and writelines are both captured", out == "a\nb\nc\n", repr(out))
    check("a command that raises propagates the error", raised)
    check("after a command that raises, prints go to the real stdout again",
          default.getvalue() == "after\n", repr(default.getvalue()))


def test_server(mod, port):
    # A malformed request gets an error reply, and the server keeps serving.
    t0 = time.monotonic()
    replies = exchange(port, b"{this is not json}\n")
    check("a malformed request gets an error reply at once",
          len(replies) == 1 and replies[0]["status"] == "error"
          and "Malformed request" in replies[0]["error"] and time.monotonic() - t0 < 2,
          replies)
    replies = exchange(port, cmds("print('alive')"))
    check("the server keeps serving after a malformed request",
          replies == [{"status": "success", "results": ["alive\n"]}], replies)

    # The first thing that isn't a request ends the connection: nothing after
    # it runs. A web page can POST to this port; the JSON body after the HTTP
    # header lines must not be executed.
    body = json.dumps({"cmds": ["HTTP_EXECUTED = True"]})
    http = (f"POST / HTTP/1.1\r\nHost: localhost:{port}\r\n"
            f"Content-Type: text/plain;charset=UTF-8\r\nOrigin: http://example.com\r\n"
            f"Content-Length: {len(body)}\r\n\r\n{body}").encode()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(http)
        replies = read_replies(s, 2)
        closed = closed_by_server(s)
    executed = exchange(port, cmds("print(globals().get('HTTP_EXECUTED', False))"))
    check("an HTTP POST gets one error reply, and the connection is closed",
          len(replies) == 1 and replies[0]["status"] == "error" and closed, replies)
    check("... and the JSON body of the HTTP request is not run",
          executed == [{"status": "success", "results": ["False\n"]}], executed)
    replies = exchange(port, b"oops\n" + cmds("print('after')") + b"\n", count=2)
    check("a request after a malformed line on the same connection is not run",
          len(replies) == 1 and replies[0]["status"] == "error", replies)

    # A valid request sent together with bytes that aren't UTF-8 is still served.
    replies = exchange(port, cmds("print('valid')") + b"\n\xff\n", count=2)
    check("a request before bytes that aren't UTF-8 is served, then the error",
          [r.get("status") for r in replies] == ["success", "error"]
          and replies[0]["results"] == ["valid\n"] and "not UTF-8" in replies[1]["error"],
          replies)

    # A request without a newline (older clients, the menu probe) still works.
    replies = exchange(port, cmds("print('bare')"))
    check("a request without a newline gets its reply", replies[:1] == [
        {"status": "success", "results": ["bare\n"]}], replies)

    # Two requests sent together each get a reply, in order; then the server closes.
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(cmds("print('one')") + b"\n" + cmds("print('two')") + b"\n")
        replies = read_replies(s, 2)
        check("two requests sent together both get replies, in order",
              [r.get("results") for r in replies] == [["one\n"], ["two\n"]], replies)
        check("without keep-alive the server then closes the connection", closed_by_server(s))

    # One request: reply, then close (auto-disconnect, as before).
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(cmds("print(1)") + b"\n")
        check("a single request gets its reply", len(read_replies(s, 1)) == 1)
        check("... and the server closes the connection", closed_by_server(s))

    # Keep-alive: the connection serves requests sent one after another.
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"disable_auto_disconnect\n")
        first = read_replies(s, 1)
        got = []
        for n in range(3):
            s.sendall(cmds(f"print({n})") + b"\n")
            got += read_replies(s, 1)
        check("keep-alive serves requests sent one after another",
              first == [{"status": "success", "results": "OK"}]
              and [r.get("results") for r in got] == [["0\n"], ["1\n"], ["2\n"]], (first, got))
        check("keep-alive leaves the connection open", not closed_by_server(s, 0.5))
    # Keep-alive without a newline, split across two reads.
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"disable_auto")
        time.sleep(0.2)
        s.sendall(b"_disconnect")
        first = read_replies(s, 1)
        s.sendall(cmds("print(7)") + b"\n")
        second = read_replies(s, 1)
        check("a bare keep-alive split across reads is recognised",
              first == [{"status": "success", "results": "OK"}]
              and second == [{"status": "success", "results": ["7\n"]}], (first, second))
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(cmds("print(2)") + b"\n")
        read_replies(s, 1)
        check("keep-alive on one connection doesn't keep others open", closed_by_server(s))

    # A client that closes its end gets the reply to half a request, as an error.
    replies = exchange(port, b'{"cmds": ["print(1)"', shut_write=True)
    check("half a request followed by the end of the stream is an error",
          len(replies) == 1 and replies[0]["status"] == "error", replies)

    # Half a request, then silence: an error after the partial-request timeout.
    mod.CLIENT_PARTIAL_TIMEOUT = 1
    t0 = time.monotonic()
    replies = exchange(port, b'{"cmds": ["print(1)"', timeout=5)
    check("half a request then silence gets an error after the timeout",
          len(replies) == 1 and "not complete after" in replies[0].get("error", "")
          and 0.8 < time.monotonic() - t0 < 4, (replies, time.monotonic() - t0))

    # A byte now and then doesn't extend the partial-request timeout.
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        t0 = time.monotonic()
        s.sendall(b'{"cmds": ["')
        replies, buf = [], b""
        s.settimeout(0.3)
        while time.monotonic() - t0 < 4 and b"\n" not in buf:
            try:
                s.sendall(b"x")
                data = s.recv(65536)
                if not data:
                    break
                buf += data
            except socket.timeout:
                continue
            except OSError:
                break
        if b"\n" in buf:
            replies = [json.loads(buf.split(b"\n", 1)[0])]
        elapsed = time.monotonic() - t0
    check("a client sending a byte now and then still times out",
          len(replies) == 1 and "not complete after" in replies[0].get("error", "")
          and elapsed < 2.5,
          (replies, elapsed))

    # Silence from the start: the server closes after the idle timeout.
    mod.CLIENT_IDLE_TIMEOUT = 1
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        check("a client that sends nothing is closed after the idle timeout",
              closed_by_server(s, 4))

    # A reply json can't encode still reaches the client, as an error.
    replies = exchange(port, json.dumps({"type": "unencodable", "params": {}}).encode() + b"\n")
    check("a reply json can't encode becomes an error reply",
          len(replies) == 1 and replies[0]["status"] == "error"
          and "could not be encoded" in replies[0]["error"], replies)

    # Other threads' prints don't end up in a command's output.
    result = {}
    slow = threading.Thread(target=lambda: result.update(r=exchange(
        port, cmds("import time\nfor _i in range(5):\n    print(_i)\n    time.sleep(0.2)"),
        timeout=10)))
    slow.start()
    time.sleep(0.2)
    for _ in range(3):
        exchange(port, json.dumps({"type": "check_server", "params": {}}).encode())
        time.sleep(0.1)
    slow.join(10)
    check("other connections' log lines don't end up in a command's output",
          result.get("r") == [{"status": "success", "results": ["0\n1\n2\n3\n4\n"]}],
          result.get("r"))

    replies = exchange(port, cmds("print('partial')\nraise ValueError('boom')"))
    check("a command that raises gets an error reply",
          replies[:1] and replies[0]["status"] == "error" and "boom" in replies[0]["error"],
          replies)

    # Text after an object on the same line: the object doesn't run.
    replies = exchange(port, cmds("SUFFIX_RAN = True") + b"oops\n", count=2)
    after = exchange(port, cmds("print(globals().get('SUFFIX_RAN', False))"))
    check("an object followed by other text on its line is an error, and doesn't run",
          len(replies) == 1 and replies[0]["status"] == "error"
          and after == [{"status": "success", "results": ["False\n"]}], (replies, after))

    # A large request is read in time (parsing the whole buffer after every
    # read was quadratic: 50 MB timed out).
    big = cmds(f"_big = {'x' * 50_000_000!r}", "print(len(_big))") + b"\n"
    shortened, mod.CLIENT_PARTIAL_TIMEOUT = mod.CLIENT_PARTIAL_TIMEOUT, 30
    t0 = time.monotonic()
    replies = exchange(port, big, timeout=30)
    mod.CLIENT_PARTIAL_TIMEOUT = shortened
    check("a 50 MB request gets its reply",
          replies == [{"status": "success", "results": ["", "50000000\n"]}],
          str(replies)[:200])
    # Adding each read to a bytes buffer copied the whole buffer: 6.5 s.
    check("... within 3 s", time.monotonic() - t0 < 3, time.monotonic() - t0)
    exchange(port, cmds("del _big"))

    # The error reply reaches a client that is still sending (closing with
    # unread input resets the connection, which can lose the reply).
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(b"oops\n")
            try:
                s.sendall(b"x" * 5_000_000)
            except OSError:
                pass
            replies = read_replies(s, 1)
    except OSError as e:
        replies = [repr(e)]
    check("the error reply reaches a client that is still sending",
          len(replies) == 1 and isinstance(replies[0], dict)
          and replies[0].get("status") == "error", str(replies)[:200])

    # Deep nesting is a malformed request, not a crash of the handler.
    replies = exchange(port, b'{"a": ' + b"[" * 200_000 + b"\n")
    check("a request nested too deeply gets an error reply",
          len(replies) == 1 and "nested too deeply" in replies[0].get("error", ""), replies)

    # A JSON value that isn't an object is malformed: what follows doesn't run.
    replies = exchange(port, b"[]\n" + cmds("AFTER_ARRAY = True") + b"\n", count=2)
    after = exchange(port, cmds("print(globals().get('AFTER_ARRAY', False))"))
    check("a request after a JSON array on the same connection is not run",
          len(replies) == 1 and "not an object" in replies[0].get("error", "")
          and after == [{"status": "success", "results": ["False\n"]}], (replies, after))


def test_stdout_none(mod):
    """Under pythonw (GIMP on Windows) sys.stdout is None: the server must keep working."""
    real = sys.stdout
    sys.stdout = None
    port = free_port()
    server, thread = start_server(mod, port)
    try:
        replies = [exchange(port, cmds("print('hi')") + b"\n") for _ in range(2)]
        alive = thread.is_alive()
        probe = exchange(port, json.dumps({"type": "check_server", "params": {}}).encode())
    finally:
        stop_server(server, thread)
        sys.stdout = real
    check("with sys.stdout None, commands get their output",
          replies == [[{"status": "success", "results": ["hi\n"]}]] * 2, replies)
    check("... and the server keeps accepting", alive and probe[:1] and
          probe[0].get("status") == "success", (alive, probe))


def main():
    install_gi_stub()
    mod = load_plugin()
    test_next_request(mod)
    test_stdout_capture(mod)
    test_stdout_none(mod)

    port = free_port()
    server, thread = start_server(mod, port)
    original = server.execute_command

    def execute(request):
        if json.loads(request).get("type") == "unencodable":
            return {"status": "success", "results": {"value": object()}}
        return original(request)
    server.execute_command = execute
    try:
        test_server(mod, port)
    finally:
        stop_server(server, thread)

    print(f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
