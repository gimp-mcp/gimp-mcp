#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
GIMP MCP Plugin - Model Context Protocol integration for GIMP
Provides bitmap extraction and metadata access functionality
"""

import gi
gi.require_version('Gimp', '3.0')

from gi.repository import Gimp
from gi.repository import GLib
from gi.repository import GObject

import io
import sys
import json
import socket
import traceback
import threading
import time
import base64
import tempfile
import os
import math
import platform
import signal

# Constants for configuration and thresholds
MAX_REGION_SIZE = 8192  # Maximum region dimension in pixels
DEFAULT_TIMEOUT_SECONDS = 30  # Default timeout for operations
VIGNETTE_SHAPES = ("circle", "square", "diamond", "horizontal", "vertical")  # gegl:vignette shape

# Reported to the MCP server, which warns when it doesn't match its own version.
# Keep in sync with SERVER_VERSION in gimp_mcp_server.py and version in pyproject.toml
# (tests/test_version_handshake.py checks this).
PLUGIN_VERSION = "0.5.0"

DEFAULT_PORT = 9877
SETTINGS_FILE_NAME = "gimp-mcp.json"
DEFAULT_SETTINGS = {"autostart": False, "port": DEFAULT_PORT}


def N_(message): return message
def _(message): return GLib.dgettext(None, message)


def _settings_path():
    """Settings live in GIMP's per-user config dir (e.g. ~/.config/GIMP/3.2)."""
    return os.path.join(Gimp.directory(), SETTINGS_FILE_NAME)


def load_settings():
    """Read the plug-in settings, falling back to defaults for anything missing or invalid."""
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(_settings_path(), "r", encoding="utf-8") as f:
            stored = json.load(f)
    except FileNotFoundError:
        return settings
    except Exception as e:
        print(f"[MCP] Could not read {_settings_path()}: {e}; using defaults")
        return settings
    if not isinstance(stored, dict):
        print(f"[MCP] {_settings_path()} is not a JSON object; using defaults")
        return settings
    if isinstance(stored.get("autostart"), bool):
        settings["autostart"] = stored["autostart"]
    port = stored.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        settings["port"] = port
    return settings


def save_settings(settings):
    path = _settings_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp_path, path)


def _exchange(sock, payload, timeout):
    """Send a JSON request on a connected socket and return the parsed reply.

    Returns None if the listener closes, errors, sends something that is not
    JSON, or does not finish replying within `timeout` seconds.
    """
    try:
        # One deadline for the whole reply, so a listener that trickles bytes
        # cannot stretch the exchange past `timeout`.
        deadline = time.monotonic() + timeout
        sock.settimeout(timeout)
        sock.sendall(json.dumps(payload).encode("utf-8"))
        data = b""
        while len(data) < 65536:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            chunk = sock.recv(4096)
            if not chunk:
                return None
            data += chunk
            try:
                return json.loads(data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    except OSError:
        pass
    return None


def send_request(host, port, payload, timeout):
    """Connect to host:port, send one JSON request and return the parsed reply.

    Returns None if the connection fails or no valid reply arrives in time.
    """
    try:
        sock = socket.create_connection((host, port), timeout=0.5)
    except OSError:
        return None
    try:
        return _exchange(sock, payload, timeout)
    finally:
        sock.close()


def probe_listener(host, port, timeout=2.0):
    """Identify what is listening on host:port.

    Returns "none" if nothing accepts connections, "mcp" if the listener
    answers check_server like this plug-in, and "other" for anything else
    (another program, or a listener that does not reply in time).
    """
    try:
        sock = socket.create_connection((host, port), timeout=0.5)
    except OSError:
        return "none"
    try:
        reply = _exchange(sock, {"type": "check_server", "params": {}}, timeout)
    finally:
        sock.close()
    results = reply.get("results") if isinstance(reply, dict) else None
    if isinstance(results, dict) and results.get("running") is True:
        return "mcp"
    return "other"


def exec_and_get_results(command, context):
    buffer = io.StringIO()
    original_stdout = sys.stdout
    sys.stdout = buffer
    exec(command, context)
    sys.stdout = original_stdout
    output = buffer.getvalue()
    return output


# CSS Color Module Level 4 color names (all 148), from Pillow's
# ImageColor.colormap. Gegl.Color.new() knows only a few basic names; for any
# other string it returns cyan (0, 1, 1, 0.67) instead of failing (#44), so
# every color is turned into hex before Gegl sees it. Table from #54 by
# @floraschouw.
CSS_COLORS = {
    "aliceblue": "#f0f8ff", "antiquewhite": "#faebd7", "aqua": "#00ffff", "aquamarine": "#7fffd4",
    "azure": "#f0ffff", "beige": "#f5f5dc", "bisque": "#ffe4c4", "black": "#000000",
    "blanchedalmond": "#ffebcd", "blue": "#0000ff", "blueviolet": "#8a2be2", "brown": "#a52a2a",
    "burlywood": "#deb887", "cadetblue": "#5f9ea0", "chartreuse": "#7fff00", "chocolate": "#d2691e",
    "coral": "#ff7f50", "cornflowerblue": "#6495ed", "cornsilk": "#fff8dc", "crimson": "#dc143c",
    "cyan": "#00ffff", "darkblue": "#00008b", "darkcyan": "#008b8b", "darkgoldenrod": "#b8860b",
    "darkgray": "#a9a9a9", "darkgreen": "#006400", "darkgrey": "#a9a9a9", "darkkhaki": "#bdb76b",
    "darkmagenta": "#8b008b", "darkolivegreen": "#556b2f", "darkorange": "#ff8c00", "darkorchid": "#9932cc",
    "darkred": "#8b0000", "darksalmon": "#e9967a", "darkseagreen": "#8fbc8f", "darkslateblue": "#483d8b",
    "darkslategray": "#2f4f4f", "darkslategrey": "#2f4f4f", "darkturquoise": "#00ced1", "darkviolet": "#9400d3",
    "deeppink": "#ff1493", "deepskyblue": "#00bfff", "dimgray": "#696969", "dimgrey": "#696969",
    "dodgerblue": "#1e90ff", "firebrick": "#b22222", "floralwhite": "#fffaf0", "forestgreen": "#228b22",
    "fuchsia": "#ff00ff", "gainsboro": "#dcdcdc", "ghostwhite": "#f8f8ff", "gold": "#ffd700",
    "goldenrod": "#daa520", "gray": "#808080", "green": "#008000", "greenyellow": "#adff2f",
    "grey": "#808080", "honeydew": "#f0fff0", "hotpink": "#ff69b4", "indianred": "#cd5c5c",
    "indigo": "#4b0082", "ivory": "#fffff0", "khaki": "#f0e68c", "lavender": "#e6e6fa",
    "lavenderblush": "#fff0f5", "lawngreen": "#7cfc00", "lemonchiffon": "#fffacd", "lightblue": "#add8e6",
    "lightcoral": "#f08080", "lightcyan": "#e0ffff", "lightgoldenrodyellow": "#fafad2", "lightgray": "#d3d3d3",
    "lightgreen": "#90ee90", "lightgrey": "#d3d3d3", "lightpink": "#ffb6c1", "lightsalmon": "#ffa07a",
    "lightseagreen": "#20b2aa", "lightskyblue": "#87cefa", "lightslategray": "#778899", "lightslategrey": "#778899",
    "lightsteelblue": "#b0c4de", "lightyellow": "#ffffe0", "lime": "#00ff00", "limegreen": "#32cd32",
    "linen": "#faf0e6", "magenta": "#ff00ff", "maroon": "#800000", "mediumaquamarine": "#66cdaa",
    "mediumblue": "#0000cd", "mediumorchid": "#ba55d3", "mediumpurple": "#9370db", "mediumseagreen": "#3cb371",
    "mediumslateblue": "#7b68ee", "mediumspringgreen": "#00fa9a", "mediumturquoise": "#48d1cc", "mediumvioletred": "#c71585",
    "midnightblue": "#191970", "mintcream": "#f5fffa", "mistyrose": "#ffe4e1", "moccasin": "#ffe4b5",
    "navajowhite": "#ffdead", "navy": "#000080", "oldlace": "#fdf5e6", "olive": "#808000",
    "olivedrab": "#6b8e23", "orange": "#ffa500", "orangered": "#ff4500", "orchid": "#da70d6",
    "palegoldenrod": "#eee8aa", "palegreen": "#98fb98", "paleturquoise": "#afeeee", "palevioletred": "#db7093",
    "papayawhip": "#ffefd5", "peachpuff": "#ffdab9", "peru": "#cd853f", "pink": "#ffc0cb",
    "plum": "#dda0dd", "powderblue": "#b0e0e6", "purple": "#800080", "rebeccapurple": "#663399",
    "red": "#ff0000", "rosybrown": "#bc8f8f", "royalblue": "#4169e1", "saddlebrown": "#8b4513",
    "salmon": "#fa8072", "sandybrown": "#f4a460", "seagreen": "#2e8b57", "seashell": "#fff5ee",
    "sienna": "#a0522d", "silver": "#c0c0c0", "skyblue": "#87ceeb", "slateblue": "#6a5acd",
    "slategray": "#708090", "slategrey": "#708090", "snow": "#fffafa", "springgreen": "#00ff7f",
    "steelblue": "#4682b4", "tan": "#d2b48c", "teal": "#008080", "thistle": "#d8bfd8",
    "tomato": "#ff6347", "turquoise": "#40e0d0", "violet": "#ee82ee", "wheat": "#f5deb3",
    "white": "#ffffff", "whitesmoke": "#f5f5f5", "yellow": "#ffff00", "yellowgreen": "#9acd32",
}

_HEX_DIGITS = set("0123456789abcdef")


def _is_hex(text):
    return bool(text) and set(text) <= _HEX_DIGITS


def _components_to_hex(parts, spec):
    """rgb()/rgba() values, or an [r, g, b(, a)] list, as "#rrggbb(aa)".

    RGB values are read as 0-255 when any of them is above 1, else as 0-1.
    Percentages ("50%") must be used for all three RGB values or none. Alpha
    is read as 0-1 when it is at most 1, else as 0-255; "50%" also works.
    """
    if len(parts) not in (3, 4):
        raise ValueError(f"Invalid color {spec!r}: rgb() and rgba() take 3 or 4 values, "
                         f"got {len(parts)}")

    def number(part):
        text = str(part).strip()
        percent = text.endswith("%")
        if percent:
            text = text[:-1]
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"Invalid color {spec!r}: {str(part).strip()!r} is not a number") from None
        if not math.isfinite(value):
            raise ValueError(f"Invalid color {spec!r}: {str(part).strip()!r} is not a number")
        return value, percent

    rgb = [number(p) for p in parts[:3]]
    percents = {percent for _, percent in rgb}
    if percents == {True, False}:
        raise ValueError(f"Invalid color {spec!r}: use percentages for all of r, g and b, "
                         f"or for none of them")
    if True in percents:
        values = [max(0.0, min(100.0, v)) * 255.0 / 100.0 for v, _ in rgb]
    else:
        values = [v for v, _ in rgb]
        if not any(v > 1.0 for v in values):
            values = [v * 255.0 for v in values]
    out = "#" + "".join(f"{max(0, min(255, round(v))):02x}" for v in values)

    if len(parts) == 4:
        alpha, percent = number(parts[3])
        if percent:
            alpha = max(0.0, min(100.0, alpha)) * 255.0 / 100.0
        elif alpha <= 1.0:
            alpha = alpha * 255.0
        out += f"{max(0, min(255, round(alpha))):02x}"
    return out


def _split_rgb(inside, spec):
    """The values inside rgb(...): "r, g, b[, a]", "r g b[ / a]" or "r g b a"."""
    if "," in inside:
        parts = [p.strip() for p in inside.split(",")]
        if any(not p for p in parts):
            raise ValueError(f"Invalid color {spec!r}: a value is empty (check for a doubled "
                             f"or trailing comma)")
        if any("/" in p or any(ch.isspace() for ch in p) for p in parts):
            raise ValueError(f"Invalid color {spec!r}: separate the values with commas only, "
                             f"as in rgb(255, 128, 0)")
        return parts
    rgb, slash, alpha = inside.partition("/")
    parts = rgb.split()
    if slash:
        if len(parts) != 3 or len(alpha.split()) != 1:
            raise ValueError(f"Invalid color {spec!r}: put one alpha value after the '/', "
                             f"as in rgb(255 128 0 / 0.5)")
        parts.append(alpha.strip())
    return parts


def is_transparent(spec):
    """True for "transparent" or "none" (Gegl reads "none" as transparent too).

    Tools that fill with a color check this first, so these get a real
    transparent fill instead of a fill with a fully transparent color.
    """
    return isinstance(spec, str) and spec.strip().lower() in ("transparent", "none")


def color_to_hex(spec):
    """Turn a color spec into "#rrggbb" or "#rrggbbaa" for Gegl.Color.new().

    Accepts hex ("#rgb", "#rgba", "#rrggbb", "#rrggbbaa"), "rgb(r,g,b)" and
    "rgba(r,g,b,a)" (see _components_to_hex for the scales; "rgb(r g b / a)"
    works too), any CSS color name, "transparent" or "none", or an
    [r, g, b(, a)] list or tuple. Hex is the only form Gegl reads as sRGB:
    it reads rgb() strings as linear values.

    Raises ValueError for anything else (TypeError for a value that is not a
    string, list or tuple), so an unknown color is reported instead of
    painted as cyan.
    """
    if isinstance(spec, (list, tuple)):
        return _components_to_hex(list(spec), spec)
    if not isinstance(spec, str):
        raise TypeError(f"Invalid color {spec!r}: expected a string such as '#ff8000'")

    text = spec.strip().lower()
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) not in (3, 4, 6, 8) or not _is_hex(digits):
            raise ValueError(f"Invalid hex color {spec!r}: use #rgb, #rgba, #rrggbb or #rrggbbaa")
        if len(digits) in (3, 4):
            digits = "".join(ch * 2 for ch in digits)
        return "#" + digits
    name, paren, rest = text.partition("(")
    if paren and name.rstrip() in ("rgb", "rgba") and rest.endswith(")"):
        return _components_to_hex(_split_rgb(rest[:-1], spec), spec)
    if text in CSS_COLORS:
        return CSS_COLORS[text]
    if is_transparent(text):
        return "#00000000"

    hint = f" Did you mean '#{text}'?" if len(text) in (3, 4, 6, 8) and _is_hex(text) else ""
    raise ValueError(f"Unknown color {spec!r}: use hex ('#ff8000'), rgb()/rgba(), "
                     f"'transparent', or a CSS color name such as 'orange'.{hint}")


# Gegl stores colour in linear light, so Gegl.Color.get_rgba() returns linear
# floats; scaling those by 255 yields linear values wearing an sRGB label, and
# #ff5733 reads back as (255, 24, 8).
#
# babl would convert this for us, but Babl.format() cannot be reached safely
# from here: if babl is not yet initialised it calls babl_fatal() -- "you must
# call babl_init first" -- which aborts the process at the C level and cannot
# be caught from Python. GIMP imports this file during its plug-in scan,
# before that initialisation, so touching babl here makes the whole plug-in
# vanish from the menu. The sRGB transfer function is exact for 8-bit output
# anyway: it round-trips all 256 levels and matches babl on every colour
# tested.


def _linear_to_srgb_u8(value):
    """One linear-light channel (0..1 float) -> 0-255 sRGB byte."""
    if value <= 0.0:
        return 0
    if value <= 0.0031308:
        encoded = value * 12.92
    else:
        encoded = 1.055 * (value ** (1.0 / 2.4)) - 0.055
    return max(0, min(255, round(encoded * 255)))


def srgb_bytes_from_color(color):
    """Gegl.Color -> (r, g, b, a) as 0-255 sRGB values."""
    red, green, blue, alpha = color.get_rgba()
    return (
        _linear_to_srgb_u8(red),
        _linear_to_srgb_u8(green),
        _linear_to_srgb_u8(blue),
        # Alpha is stored linearly; it takes no transfer function.
        max(0, min(255, round(alpha * 255))),
    )


class MCPPlugin(Gimp.PlugIn):
    def __init__(self, host='localhost', port=DEFAULT_PORT):
        super().__init__()
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None
        self.context = {}
        exec("from gi.repository import Gimp", self.context)
        self.auto_disconnect_client = True
        # One client thread per connection; a retry after a client-side timeout
        # would otherwise drive the libgimp wire protocol concurrently and crash
        self.command_lock = threading.Lock()
        # A restart_server request asks the accept thread to re-bind the
        # listening socket, so the socket is only ever replaced by the thread
        # that calls accept() on it.
        self._restart_requested = threading.Event()
        self._restart_done = threading.Event()
        self._restart_result = None
        # Displays opened by _create_display, keyed by image id; values are
        # always Gimp.Display objects. GIMP 3.2 exposes no Gimp.get_displays()
        # or Display.get_image(), so close_image and set_active_image cannot
        # find an image's window any other way.
        self._displays = {}

    def do_set_i18n(self, procname):
        # Plugin has no translations; tell GIMP so it stops logging
        # "catalog directory does not exist" for every registered procedure.
        return False

    def do_query_procedures(self):
        """Register the plugin procedures.

        GIMP sorts plug-in menu items alphabetically by label (registration
        order is ignored), so the Tools > MCP labels are numbered to keep
        them in Start, Check, Restart, Settings order.
        """
        return ["plug-in-mcp-server", "plug-in-mcp-check", "plug-in-mcp-restart",
                "plug-in-mcp-settings", "plug-in-mcp-autostart"]

    def do_create_procedure(self, name):
        """Define the procedure properties."""
        if name == "plug-in-mcp-autostart":
            # GIMP runs every PERSISTENT procedure that takes no arguments once
            # at startup. It stays hidden (no menu path) and only starts the
            # server when autostart is enabled in the settings dialog.
            procedure = Gimp.Procedure.new(self, name, Gimp.PDBProcType.PERSISTENT,
                                           self._run_autostart, None)
            procedure.set_documentation(_("Starts the MCP server when GIMP launches, if enabled"),
                                        _("Run automatically by GIMP at startup. Enable it in "
                                          "Tools > MCP > MCP Server Settings."),
                                        name)
            procedure.set_attribution("Viesar Lab", "Viesar Lab", "2026")
            return procedure

        if name == "plug-in-mcp-settings":
            procedure = Gimp.Procedure.new(self, name, Gimp.PDBProcType.PLUGIN, self._run_settings, None)
            procedure.set_menu_label(_("4. MCP Server Settings..."))
            procedure.set_documentation(_("Configure the MCP server"),
                                        _("Choose whether the MCP server starts when GIMP launches, "
                                          "and which port it listens on. Changes apply the next "
                                          "time the server starts."),
                                        name)
            procedure.set_attribution("Viesar Lab", "Viesar Lab", "2026")
            procedure.add_enum_argument("run-mode", _("Run mode"), _("The run mode"),
                                        Gimp.RunMode, Gimp.RunMode.INTERACTIVE,
                                        GObject.ParamFlags.READWRITE)
            procedure.add_boolean_argument("autostart", _("_Start MCP server when GIMP launches"),
                                           _("Start the MCP server automatically when GIMP launches"),
                                           DEFAULT_SETTINGS["autostart"], GObject.ParamFlags.READWRITE)
            procedure.add_int_argument("port", _("_Port"),
                                       _("TCP port the MCP server listens on (localhost only). "
                                         "Set GIMP_MCP_PORT for the MCP client to match."),
                                       1, 65535, DEFAULT_PORT, GObject.ParamFlags.READWRITE)
            procedure.add_menu_path('<Image>/Tools/MCP')
            return procedure

        if name == "plug-in-mcp-check":
            procedure = Gimp.Procedure.new(self, name, Gimp.PDBProcType.PLUGIN, self._run_check, None)
            procedure.set_menu_label(_("2. Check MCP Server"))
            procedure.set_documentation(_("Check whether the MCP server is running"),
                                        _("Reports whether an MCP server is listening on the "
                                          "port saved in MCP Server Settings"),
                                        name)
            procedure.set_attribution("Viesar Lab", "Viesar Lab", "2026")
            procedure.add_enum_argument("run-mode", _("Run mode"), _("The run mode"),
                                        Gimp.RunMode, Gimp.RunMode.INTERACTIVE,
                                        GObject.ParamFlags.READWRITE)
            procedure.add_menu_path('<Image>/Tools/MCP')
            return procedure

        if name == "plug-in-mcp-restart":
            procedure = Gimp.Procedure.new(self, name, Gimp.PDBProcType.PLUGIN, self._run_restart, None)
            procedure.set_menu_label(_("3. Restart MCP Server"))
            procedure.set_documentation(_("Restart the MCP server socket"),
                                        _("Asks the running MCP server to drop and re-bind its "
                                          "listening socket"),
                                        name)
            procedure.set_attribution("Viesar Lab", "Viesar Lab", "2026")
            procedure.add_enum_argument("run-mode", _("Run mode"), _("The run mode"),
                                        Gimp.RunMode, Gimp.RunMode.INTERACTIVE,
                                        GObject.ParamFlags.READWRITE)
            procedure.add_menu_path('<Image>/Tools/MCP')
            return procedure

        # Default: plug-in-mcp-server
        procedure = Gimp.Procedure.new(self, name, Gimp.PDBProcType.PLUGIN, self.run, None)
        procedure.set_menu_label(_("1. Start MCP Server"))
        procedure.set_documentation(_("Starts an MCP server to control GIMP externally"),
                                    _("Starts an MCP server to control GIMP externally"),
                                    name)
        procedure.set_attribution("Viesar Lab", "Viesar Lab", "2026")
        procedure.add_enum_argument("run-mode", _("Run mode"), _("The run mode"),
                                    Gimp.RunMode, Gimp.RunMode.INTERACTIVE,
                                    GObject.ParamFlags.READWRITE)
        procedure.add_menu_path('<Image>/Tools/MCP')
        return procedure

    @staticmethod
    def _report(config, message):
        """Print a menu-action result, and show it in GIMP when run interactively."""
        print(f"[MCP] {message}")
        if config.get_property("run-mode") == Gimp.RunMode.INTERACTIVE:
            Gimp.message(message)

    # Each menu click runs in a fresh plug-in process, separate from the one
    # running the server (autostart or Start), so the menu actions below talk
    # to the server over its socket instead of reading self.running.

    def _run_check(self, procedure, config, run_data):
        """Menu action: report whether the MCP server is listening."""
        port = load_settings()["port"]
        listener = probe_listener(self.host, port)
        status = {"mcp": "RUNNING", "none": "STOPPED"}.get(listener, "PORT IN USE by another program")
        self._report(config, f"Server status: {status} on {self.host}:{port}")
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

    def _run_restart(self, procedure, config, run_data):
        """Menu action: ask the running MCP server to re-bind its socket."""
        port = load_settings()["port"]
        listener = probe_listener(self.host, port)
        if listener == "mcp":
            reply = send_request(self.host, port, {"type": "restart_server", "params": {}},
                                 DEFAULT_TIMEOUT_SECONDS)
            if isinstance(reply, dict) and reply.get("status") == "success":
                self._report(config, f"MCP server restarted on {self.host}:{port}")
                return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
            error = reply.get("error") if isinstance(reply, dict) else None
            message = f"Could not restart the MCP server on {self.host}:{port}: {error or 'no reply'}"
        elif listener == "none":
            message = (f"No MCP server is running on {self.host}:{port}. "
                       f"Start it with Tools > MCP > Start MCP Server.")
        else:
            message = (f"Port {port} on {self.host} is in use by another program, not the MCP "
                       f"server. Choose a different port in Tools > MCP > MCP Server Settings.")
        self._report(config, message)
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(message))

    def _color(self, spec):
        """Build a Gegl.Color from a color spec; see color_to_hex for the formats.

        Raises ValueError for a color it can't read, and TypeError for a value
        that isn't a string, list or tuple. A Gegl.Color is returned unchanged.
        """
        from gi.repository import Gegl
        if isinstance(spec, Gegl.Color):
            return spec
        return Gegl.Color.new(color_to_hex(spec))

    def shutdown_server(self, signum=None, frame=None):
        """Gracefully shutdown the server."""
        print(f"Shutdown signal received (signal: {signum}), closing MCP server...")
        self.running = False
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        if hasattr(self, '_glib_loop') and self._glib_loop:
            self._glib_loop.quit()

    def _start_server_thread(self):
        """Accept loop — runs in a background thread on the socket bound by _serve_forever."""
        try:
            while self.running:
                if self._restart_requested.is_set():
                    self._rebind()
                    if not self.running:
                        break
                    continue
                try:
                    client, address = self.socket.accept()
                    print(f"Connected to client: {address}")
                except socket.timeout:
                    continue
                except OSError:
                    break
                client_thread = threading.Thread(target=self._handle_client, args=(client,))
                client_thread.daemon = True
                client_thread.start()

            print("MCP server shutting down...")
            if self.socket:
                try:
                    self.socket.close()
                except Exception:
                    pass
                self.socket = None
            print("MCP server stopped")
        except Exception as e:
            print(f"Error in MCP server thread: {str(e)}")
            self.running = False

    def _serve_forever(self, announce=False):
        """Bind the server socket, then block in the GLib main loop.

        With announce, show a success message in GIMP once the socket is
        bound (the Start menu action; autostart stays quiet on every launch).
        Returns None once the server has run and stopped, or an
        (already_running, message) tuple if it could not start.
        """
        # Each menu click runs in a fresh plug-in process, so self.running
        # cannot tell whether the autostarted server (another process) is up.
        listener = probe_listener(self.host, self.port)
        if listener == "mcp":
            return (True, f"MCP server is already running on {self.host}:{self.port}")
        if listener == "other":
            return (False, f"Could not start MCP server: port {self.port} on {self.host} is in use "
                           f"by another program. Choose a different port in "
                           f"Tools > MCP > MCP Server Settings.")

        # Bind in the calling thread so a failure is reported instead of
        # leaving the process idling in the main loop with no server.
        print("Creating socket...")
        try:
            self.socket = self._bind_listener()
        except OSError as e:
            return (False, f"Could not start MCP server on {self.host}:{self.port}: {e}")
        self.running = True
        print(f"GimpMCP server started on {self.host}:{self.port}")
        if announce:
            Gimp.message(f"MCP Server started on {self.host}:{self.port}")

        signal.signal(signal.SIGTERM, self.shutdown_server)
        signal.signal(signal.SIGINT, self.shutdown_server)

        # Server socket runs in a background thread
        server_thread = threading.Thread(target=self._start_server_thread, daemon=True)
        server_thread.start()

        # GLib main loop runs in the main thread — required for GIMP API calls
        # (all Gimp.* calls go over the wire protocol which needs GLib to dispatch)
        self._glib_loop = GLib.MainLoop()
        self._glib_loop.run()
        return None

    def run(self, procedure, config, run_data):
        """Menu handler: start the server."""
        if self.running:
            print("MCP Server is already running")
            return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

        self.port = load_settings()["port"]
        failure = self._serve_forever(announce=True)
        if failure:
            already_running, message = failure
            print(f"[MCP] {message}")
            Gimp.message(message)
            if not already_running:
                return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR,
                                                   GLib.Error(message))
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

    def _run_autostart(self, procedure, config, run_data):
        """Run by GIMP at launch: start the server if autostart is enabled."""
        # GIMP holds its startup until a persistent procedure reports ready.
        # persistent_enable() is deliberately not called: it would make the
        # main loop read GIMP's wire channel and race the client threads for
        # their PDB replies.
        procedure.persistent_ready()

        settings = load_settings()
        if not settings["autostart"]:
            return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

        self.port = settings["port"]
        failure = self._serve_forever()
        if failure:
            already_running, message = failure
            print(f"[MCP] Autostart: {message}")
            if not already_running:
                Gimp.message(message)
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

    def _show_settings_dialog(self, settings):
        """Show the settings dialog; return the new settings, or None if cancelled.

        A plain GimpUi.Dialog rather than GimpUi.ProcedureDialog: the latter
        also shows the run-mode argument and GIMP's own Load/Save Settings
        preset buttons, which are separate from gimp-mcp.json and confusing.
        """
        gi.require_version('GimpUi', '3.0')
        gi.require_version('Gtk', '3.0')
        from gi.repository import GimpUi, Gtk
        GimpUi.init("plug-in-mcp-settings")

        use_header_bar = Gtk.Settings.get_default().get_property("gtk-dialogs-use-header")
        dialog = GimpUi.Dialog(use_header_bar=use_header_bar, title=_("MCP Server Settings"),
                               role="plug-in-mcp-settings")
        dialog.add_button(_("_Cancel"), Gtk.ResponseType.CANCEL)
        dialog.add_button(_("_OK"), Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_border_width(12)
        dialog.get_content_area().pack_start(box, True, True, 0)

        autostart = Gtk.CheckButton.new_with_mnemonic(_("_Start MCP server when GIMP launches"))
        autostart.set_active(settings["autostart"])
        box.pack_start(autostart, False, False, 0)

        port_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        port_label = Gtk.Label.new_with_mnemonic(_("_Port:"))
        port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        port.set_value(settings["port"])
        port.set_activates_default(True)
        port_label.set_mnemonic_widget(port)
        port_row.pack_start(port_label, False, False, 0)
        port_row.pack_start(port, False, False, 0)
        box.pack_start(port_row, False, False, 0)

        note = Gtk.Label(label=_("Changes apply the next time the MCP server starts.\n"
                                 "If you change the port, set GIMP_MCP_PORT to the same\n"
                                 "value in your MCP client config."))
        note.set_xalign(0.0)
        note.get_style_context().add_class("dim-label")
        box.pack_start(note, False, False, 0)

        dialog.show_all()
        response = dialog.run()
        port.update()  # commit a value typed without leaving the field
        result = {"autostart": autostart.get_active(), "port": port.get_value_as_int()}
        dialog.destroy()
        return result if response == Gtk.ResponseType.OK else None

    def _run_settings(self, procedure, config, run_data):
        """Menu action: edit autostart and port, saved to gimp-mcp.json."""
        if config.get_property("run-mode") == Gimp.RunMode.INTERACTIVE:
            new_settings = self._show_settings_dialog(load_settings())
            if new_settings is None:
                return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
        else:
            new_settings = {
                "autostart": bool(config.get_property("autostart")),
                "port": int(config.get_property("port")),
            }
        try:
            save_settings(new_settings)
        except OSError as e:
            message = f"Could not save MCP settings to {_settings_path()}: {e}"
            print(f"[MCP] {message}")
            return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(message))

        message = (f"MCP settings saved: autostart {'on' if new_settings['autostart'] else 'off'}, "
                   f"port {new_settings['port']}. Takes effect the next time the MCP server starts "
                   f"(restart GIMP for autostart).")
        if new_settings["port"] != DEFAULT_PORT:
            message += (f" Set GIMP_MCP_PORT={new_settings['port']} in your MCP client config "
                        f"so it connects to the same port.")
        print(f"[MCP] {message}")
        if config.get_property("run-mode") == Gimp.RunMode.INTERACTIVE:
            Gimp.message(message)
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

    @staticmethod
    def _is_check_server(request):
        try:
            j = json.loads(request)
        except (json.JSONDecodeError, TypeError):
            return False
        return isinstance(j, dict) and j.get("type") == "check_server"

    def _handle_client(self, client):
        """Handle connected client"""
        # print("Client handler started")
        buffer = b''

        # Receive data in chunks to handle larger payloads
        while True:
            data = client.recv(4096)
            # print(f"Received data: {data}")
            if not data:
                break
            buffer += data
            
            # Check if we have a complete message
            # For simplicity, assume messages end with newline or are complete JSON
            try:
                if isinstance(buffer, (bytes, bytearray)):
                    request = buffer.decode('utf-8')
                else:
                    request = str(buffer)
                
                # Try to parse as JSON to see if complete
                if request.strip():
                    json.loads(request)  # This will raise if incomplete
                    break
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Continue receiving if JSON is incomplete
                continue
        
        if not buffer:
            print("Client disconnected")
            return

        if isinstance(buffer, (bytes, bytearray)):
            request = buffer.decode('utf-8')
        else:
            request = str(buffer)
        
        # print(f"Parsed request: {request}")
        if self._is_check_server(request):
            # check_server is handled only here, without the lock (it makes no
            # GIMP calls), so a probe from another plug-in process gets a reply
            # even mid-command.
            response = {"status": "success", "results": {"running": True, "port": self.port}}
        else:
            with self.command_lock:
                response = self.execute_command(request)
        print(f"response type: {type(response)}")
        
        if isinstance(response, dict):
            response_str = json.dumps(response)
        else:
            response_str = str(response)
            
        # Send response in chunks for large data
        response_bytes = response_str.encode('utf-8')
        bytes_sent = 0
        while bytes_sent < len(response_bytes):
            chunk = response_bytes[bytes_sent:bytes_sent + 8192]
            client.sendall(chunk)
            bytes_sent += len(chunk)
            
        if self.auto_disconnect_client:
            client.close()
        return

    def execute_command(self, request):
        """Run one command. Called from client threads; command_lock keeps commands from overlapping."""
        try:
            if request == "disable_auto_disconnect":
                self.auto_disconnect_client = False
                return {"status": "success", "results": "OK"}
            j = json.loads(request)
            if "type" in j and j["type"] == "get_image_bitmap":
                params = j.get("params", {})
                return self._get_current_image_bitmap(params)
            elif "type" in j and j["type"] == "get_image_metadata":
                return self._get_current_image_metadata()
            elif "type" in j and j["type"] == "get_gimp_info":
                return self._get_gimp_info()
            elif "type" in j and j["type"] == "get_context_state":
                return self._get_context_state()
            elif "type" in j and j["type"] == "restart_server":
                return self._restart_server()
            elif "type" in j and j["type"] == "new_canvas":
                params = j.get("params", {})
                return self._new_canvas(params)
            # ── Category 1: File Operations ──────────────────────────────────
            elif "type" in j and j["type"] == "open_image":
                return self._open_image(j.get("params", {}))
            elif "type" in j and j["type"] == "open_image_as_layer":
                return self._open_image_as_layer(j.get("params", {}))
            elif "type" in j and j["type"] == "save_xcf":
                return self._save_xcf(j.get("params", {}))
            elif "type" in j and j["type"] == "export_image":
                return self._export_image(j.get("params", {}))
            elif "type" in j and j["type"] == "batch_export":
                return self._batch_export(j.get("params", {}))
            # ── Category 2: Image Adjustments ────────────────────────────────
            elif "type" in j and j["type"] == "auto_levels":
                return self._auto_levels(j.get("params", {}))
            elif "type" in j and j["type"] == "adjust_curves":
                return self._adjust_curves(j.get("params", {}))
            elif "type" in j and j["type"] == "adjust_brightness_contrast":
                return self._adjust_brightness_contrast(j.get("params", {}))
            elif "type" in j and j["type"] == "adjust_hue_saturation":
                return self._adjust_hue_saturation(j.get("params", {}))
            elif "type" in j and j["type"] == "adjust_color_balance":
                return self._adjust_color_balance(j.get("params", {}))
            elif "type" in j and j["type"] == "sharpen":
                return self._sharpen(j.get("params", {}))
            elif "type" in j and j["type"] == "blur":
                return self._blur(j.get("params", {}))
            elif "type" in j and j["type"] == "denoise":
                return self._denoise(j.get("params", {}))
            elif "type" in j and j["type"] == "desaturate":
                return self._desaturate(j.get("params", {}))
            elif "type" in j and j["type"] == "invert_colors":
                return self._invert_colors(j.get("params", {}))
            # ── Category 3: Resize & Transform ───────────────────────────────
            elif "type" in j and j["type"] == "scale_image":
                return self._scale_image(j.get("params", {}))
            elif "type" in j and j["type"] == "scale_to_fit":
                return self._scale_to_fit(j.get("params", {}))
            elif "type" in j and j["type"] == "crop_to_selection":
                return self._crop_to_selection(j.get("params", {}))
            elif "type" in j and j["type"] == "crop_to_rect":
                return self._crop_to_rect(j.get("params", {}))
            elif "type" in j and j["type"] == "rotate_image":
                return self._rotate_image(j.get("params", {}))
            elif "type" in j and j["type"] == "flip_image":
                return self._flip_image(j.get("params", {}))
            elif "type" in j and j["type"] == "resize_canvas":
                return self._resize_canvas(j.get("params", {}))
            # ── Category 4: Selections ────────────────────────────────────────
            elif "type" in j and j["type"] == "select_rectangle":
                return self._select_rectangle(j.get("params", {}))
            elif "type" in j and j["type"] == "select_ellipse":
                return self._select_ellipse(j.get("params", {}))
            elif "type" in j and j["type"] == "select_by_color":
                return self._select_by_color(j.get("params", {}))
            elif "type" in j and j["type"] == "select_all":
                return self._select_all(j.get("params", {}))
            elif "type" in j and j["type"] == "select_none":
                return self._select_none(j.get("params", {}))
            elif "type" in j and j["type"] == "invert_selection":
                return self._invert_selection(j.get("params", {}))
            elif "type" in j and j["type"] == "modify_selection":
                return self._modify_selection(j.get("params", {}))
            # ── Category 5: Layer Operations ──────────────────────────────────
            elif "type" in j and j["type"] == "create_layer":
                return self._create_layer(j.get("params", {}))
            elif "type" in j and j["type"] == "duplicate_layer":
                return self._duplicate_layer(j.get("params", {}))
            elif "type" in j and j["type"] == "delete_layer":
                return self._delete_layer(j.get("params", {}))
            elif "type" in j and j["type"] == "rename_layer":
                return self._rename_layer(j.get("params", {}))
            elif "type" in j and j["type"] == "set_layer_properties":
                return self._set_layer_properties(j.get("params", {}))
            elif "type" in j and j["type"] == "reorder_layer":
                return self._reorder_layer(j.get("params", {}))
            elif "type" in j and j["type"] == "flatten_image":
                return self._flatten_image(j.get("params", {}))
            elif "type" in j and j["type"] == "merge_visible_layers":
                return self._merge_visible_layers(j.get("params", {}))
            elif "type" in j and j["type"] == "list_layers":
                return self._list_layers(j.get("params", {}))
            # ── Category 6: Color & Paint ─────────────────────────────────────
            elif "type" in j and j["type"] == "fill_layer":
                return self._fill_layer(j.get("params", {}))
            elif "type" in j and j["type"] == "fill_selection":
                return self._fill_selection(j.get("params", {}))
            elif "type" in j and j["type"] == "set_colors":
                return self._set_colors(j.get("params", {}))
            elif "type" in j and j["type"] == "draw_line":
                return self._draw_line(j.get("params", {}))
            elif "type" in j and j["type"] == "draw_rectangle":
                return self._draw_rectangle(j.get("params", {}))
            elif "type" in j and j["type"] == "draw_ellipse":
                return self._draw_ellipse(j.get("params", {}))
            elif "type" in j and j["type"] == "fill_rectangle":
                return self._fill_rectangle(j.get("params", {}))
            elif "type" in j and j["type"] == "fill_ellipse":
                return self._fill_ellipse(j.get("params", {}))
            elif "type" in j and j["type"] == "gradient_fill":
                return self._gradient_fill(j.get("params", {}))
            # ── Category 7: Text ──────────────────────────────────────────────
            elif "type" in j and j["type"] == "add_text":
                return self._add_text(j.get("params", {}))
            elif "type" in j and j["type"] == "edit_text":
                return self._edit_text(j.get("params", {}))
            elif "type" in j and j["type"] == "list_fonts":
                return self._list_fonts(j.get("params", {}))
            # ── Category 8: Filters & Effects ────────────────────────────────
            elif "type" in j and j["type"] == "apply_drop_shadow":
                return self._apply_drop_shadow(j.get("params", {}))
            elif "type" in j and j["type"] == "apply_gaussian_blur":
                return self._apply_gaussian_blur(j.get("params", {}))
            elif "type" in j and j["type"] == "apply_pixelate":
                return self._apply_pixelate(j.get("params", {}))
            elif "type" in j and j["type"] == "apply_emboss":
                return self._apply_emboss(j.get("params", {}))
            elif "type" in j and j["type"] == "apply_vignette":
                return self._apply_vignette(j.get("params", {}))
            elif "type" in j and j["type"] == "apply_noise":
                return self._apply_noise(j.get("params", {}))
            # ── Category 9: Export Pipelines ──────────────────────────────────
            elif "type" in j and j["type"] == "export_icon_sizes":
                return self._export_icon_sizes(j.get("params", {}))
            elif "type" in j and j["type"] == "export_web_optimized":
                return self._export_web_optimized(j.get("params", {}))
            elif "type" in j and j["type"] == "batch_resize":
                return self._batch_resize(j.get("params", {}))
            elif "type" in j and j["type"] == "export_sprite_sheet":
                return self._export_sprite_sheet(j.get("params", {}))
            elif "type" in j and j["type"] == "export_social_media_kit":
                return self._export_social_media_kit(j.get("params", {}))
            # ── Category 10: Utility ──────────────────────────────────────────
            elif "type" in j and j["type"] == "list_images":
                return self._list_images(j.get("params", {}))
            elif "type" in j and j["type"] == "set_active_image":
                return self._set_active_image(j.get("params", {}))
            elif "type" in j and j["type"] == "undo":
                return self._undo(j.get("params", {}))
            elif "type" in j and j["type"] == "redo":
                return self._redo(j.get("params", {}))
            elif "type" in j and j["type"] == "convert_color_mode":
                return self._convert_color_mode(j.get("params", {}))
            elif "type" in j and j["type"] == "close_image":
                return self._close_image(j.get("params", {}))
            elif "type" in j and j["type"] == "get_selection_bounds":
                return self._get_selection_bounds(j.get("params", {}))
            elif "type" in j and j["type"] == "get_pixel_color":
                return self._get_pixel_color(j.get("params", {}))
            elif "type" in j and j["type"] == "get_histogram":
                return self._get_histogram(j.get("params", {}))
            elif "type" in j and j["type"] == "warp_region":
                return self._warp_region(j.get("params", {}))
            elif "cmds" in j:
                a = ['python-fu-exec', j["cmds"]]
            else:
                p = j["params"]
                a = p['args']

            # Protect against empty args list
            if len(a) == 0:
                return {
                    "status": "error",
                    "error": "No command arguments provided"
                }

            if a[0] == 'python-fu-eval':
                if len(a) > 0:
                    print(f"evaluating exprs: {a[1]}")
                    vals = [str(eval(e)) for e in a[1]]
                    results = {
                        "status": "success",
                        "results": vals
                    }
                else:
                    results = {
                    "status": "success",
                    "results": "[NULL]"
                }
                print(f"expression result: {results}")
                return results
            else:
                outputs = ["OK"]
                if len(a) > 0:
                    print(f"Executing commands: {a[1]}")
                    outputs = [exec_and_get_results(c, self.context) for c in a[1]]
                else:
                    print("no command to execute")
                result = {
                    "status": "success",
                    "results": outputs
                }

                print(f"Command result: {result}")
                return result

        except Exception as e:
            error_msg = f"Error executing command: {str(e)}\n{traceback.format_exc()}"
            print(error_msg)
            return {
                "status": "error",
                "error": str(e),
                "traceback": traceback.format_exc()
            }

    def _get_current_image_bitmap(self, params=None):
        """Get the current image as a base64-encoded bitmap with optional scaling and region selection."""
        try:
            if params is None:
                params = {}
                
            print(f"Getting current image bitmap with params: {params}")

            # Extract parameters
            max_width = params.get("max_width")
            max_height = params.get("max_height")
            image_index = params.get("image_index", 0)

            # Scaling bounds below 1 would divide by zero or scale to nothing
            for key, value in [("max_width", max_width), ("max_height", max_height)]:
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, int):
                    return {
                        "status": "error",
                        "error": f"'{key}' must be of type int, got {type(value).__name__}"
                    }
                if value < 1:
                    return {
                        "status": "error",
                        "error": f"'{key}' must be at least 1, got {value}"
                    }

            # Extract region parameters if provided
            region = params.get("region")
            # Any region given, even {} or one with only scaling bounds, asks for a
            # region and must name its rectangle; it never means the full image
            region_requested = region is not None
            if region is None:
                region = {}
            if not isinstance(region, dict):
                return {
                    "status": "error",
                    "error": f"'region' must be an object, got {type(region).__name__}"
                }

            # Region values are integers: the origin at least 0, sizes and
            # scaling bounds at least 1 (an empty region can't be rendered)
            for key, minimum in [("origin_x", 0), ("origin_y", 0), ("width", 1), ("height", 1),
                                 ("max_width", 1), ("max_height", 1)]:
                value = region.get(key)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, int):
                    return {
                        "status": "error",
                        "error": f"Region parameter '{key}' must be of type int, got {type(value).__name__}"
                    }
                if value < minimum:
                    return {
                        "status": "error",
                        "error": f"Region parameter '{key}' must be at least {minimum}, got {value}"
                    }
            
            origin_x = region.get("origin_x")
            origin_y = region.get("origin_y")
            region_width = region.get("width")
            region_height = region.get("height")
            scaled_to_width = region.get("max_width")  # Region scaling uses max_width/max_height
            scaled_to_height = region.get("max_height")

            # Select the requested image (default 0 = most recently active);
            # int() would turn -0.5 into a valid 0, so require an integer
            if isinstance(image_index, bool) or not isinstance(image_index, int) or image_index < 0:
                return {
                    "status": "error",
                    "error": f"image_index must be a non-negative integer, got {image_index!r}"
                }
            try:
                original_image = self._get_image(image_index)
            except RuntimeError as e:
                return {
                    "status": "error",
                    "error": str(e)
                }

            # Get original image dimensions
            orig_img_width = original_image.get_width()
            orig_img_height = original_image.get_height()

            # With nothing visible the snapshot would be blank (or, with every layer
            # hidden, show a hidden layer's pixels), so report an error instead. A layer
            # counts only if it is visible with opacity above 0; a group counts only
            # when it does and contains such a layer
            def has_visible(items):
                for item in items:
                    if not item.get_visible() or item.get_opacity() <= 0:
                        continue
                    if not item.is_group() or has_visible(item.get_children()):
                        return True
                return False

            if not has_visible(original_image.get_layers()):
                return {
                    "status": "error",
                    "error": "No visible layers in the image (all layers are hidden or at 0% opacity, or the image has no layers)"
                }

            # Validate region before creating any temporary image
            if region_requested:
                # Validate region parameters
                missing = [key for key, value in [("origin_x", origin_x), ("origin_y", origin_y),
                                                  ("width", region_width), ("height", region_height)]
                           if value is None]
                if missing:
                    return {
                        "status": "error",
                        "error": "For region selection, all parameters are required: origin_x, origin_y, width, height"
                                 f" (missing: {', '.join(missing)})"
                    }

                # Validate region bounds
                if (origin_x < 0 or origin_y < 0 or
                    origin_x + region_width > orig_img_width or
                    origin_y + region_height > orig_img_height):
                    return {
                        "status": "error",
                        "error": f"Region bounds invalid. Image size: {orig_img_width}x{orig_img_height}, "
                               f"requested region: ({origin_x},{origin_y}) {region_width}x{region_height}"
                    }

            # Render the visible composite of the region or the full image into a
            # temporary image, deleted in finally. Filters crossing the region edge
            # apply before the cut, so a region equals the same rectangle of the full
            # bitmap; only one layer is copied, and the user's selection and
            # clipboard stay untouched
            final_image = None
            temp_path = None

            try:
                if region_requested:
                    print("Processing region extraction...")
                    final_image = self._render_visible(original_image, origin_x, origin_y,
                                                       region_width, region_height)
                else:
                    print("Processing full image...")
                    final_image = self._render_visible(original_image, 0, 0,
                                                       orig_img_width, orig_img_height)

                # Calculate target dimensions
                current_width = final_image.get_width()
                current_height = final_image.get_height()
                target_width = current_width
                target_height = current_height
                
                # Determine scaling target: a region's own bounds, or else the
                # top-level ones. Either bound may be given alone and limits
                # only its own dimension
                if scaled_to_width is not None or scaled_to_height is not None:
                    max_w, max_h = scaled_to_width, scaled_to_height
                else:
                    max_w, max_h = max_width, max_height

                # Apply center inside scaling if target dimensions provided
                if max_w is not None or max_h is not None:
                    # Bounds only shrink: a missing bound, or one above the current
                    # size, is the current size. Otherwise a lone max_width=100 would
                    # blow a 1x1000 image up to 100x100000
                    max_w = current_width if max_w is None else min(max_w, current_width)
                    max_h = current_height if max_h is None else min(max_h, current_height)

                    # Calculate center inside scaling in integers: float division can
                    # come out a pixel short (51x43 with max_width=51 gave 51x42)
                    if current_width * max_h > max_w * current_height:
                        # Width is the limiting factor
                        target_width = max_w
                        target_height = max(1, max_w * current_height // current_width)
                    else:
                        # Height is the limiting factor
                        target_height = max_h
                        target_width = max(1, max_h * current_width // current_height)
                    
                    print(f"Scaling from {current_width}x{current_height} to {target_width}x{target_height}")

                    # Scale the image if dimensions changed
                    if target_width != current_width or target_height != current_height:
                        try:
                            self._check_ok(final_image.scale(target_width, target_height),
                                           "Scaling the preview copy")
                        except (RuntimeError) as scale_error:
                            raise RuntimeError(f"Failed to scale image from {current_width}x{current_height} to {target_width}x{target_height}: {scale_error}")

                # Create a temporary file for export
                temp_fd, temp_path = tempfile.mkstemp(suffix='.png')
                os.close(temp_fd)  # Close the file descriptor as GIMP will handle the file
            
                # Export the final image as PNG (_render_visible always gives it a layer)
                # Export the image to PNG
                try:
                    # In GIMP 3.0, use the simplified export approach
                    from gi.repository import Gio
                    file_obj = Gio.File.new_for_path(temp_path)
                    
                    # Use file-png-export with the correct parameters for GIMP 3.0
                    export_proc = Gimp.get_pdb().lookup_procedure('file-png-export')
                    if not export_proc:
                        return {
                            "status": "error",
                            "error": "PNG export procedure not found"
                        }
                    
                    export_config = export_proc.create_config()
                    export_config.set_property('image', final_image)
                    export_config.set_property('file', file_obj)

                    result = self._run_pdb(export_proc, export_config)
                    print(f"Export result: {result}")
                    
                except Exception as export_error:
                    print(f"Export error: {export_error}")
                    # Fallback: try using the PDB directly with correct arguments
                    try:
                        from gi.repository import Gio
                        file_obj = Gio.File.new_for_path(temp_path)
                        
                        # Gimp.file_save(run_mode, image, file, options) returns False
                        # instead of raising when the save fails.
                        self._check_ok(
                            Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, final_image, file_obj, None),
                            "Gimp.file_save",
                        )
                        print("Fallback export successful")
                    except Exception as fallback_error:
                        print(f"Fallback export error: {fallback_error}")
                        # Try another fallback using gimp-file-save PDB procedure
                        try:
                            pdb = Gimp.get_pdb()
                            save_proc = pdb.lookup_procedure('gimp-file-save')
                            if save_proc:
                                save_config = save_proc.create_config()
                                save_config.set_property('image', final_image)
                                save_config.set_property('file', file_obj)
                                save_result = self._run_pdb(save_proc, save_config)
                                print(f"PDB save result: {save_result}")
                            else:
                                return {
                                    "status": "error",
                                    "error": f"All export methods failed: {export_error}, fallback: {fallback_error}"
                                }
                        except Exception as pdb_error:
                            return {
                                "status": "error",
                                "error": f"All export methods failed: {export_error}, fallback: {fallback_error}, PDB: {pdb_error}"
                            }
                
                # Read the exported file and encode as base64
                with open(temp_path, 'rb') as f:
                    image_data = f.read()
                    encoded_image = base64.b64encode(image_data).decode('utf-8')
                
                # Get final image metadata
                final_width = final_image.get_width()
                final_height = final_image.get_height()
                
                return {
                    "status": "success",
                    "results": {
                        "image_data": encoded_image,
                        "format": "png",
                        "width": final_width,
                        "height": final_height,
                        "original_width": orig_img_width,
                        "original_height": orig_img_height,
                        "encoding": "base64",
                        "processing_applied": {
                            "region_extracted": region_requested,
                            "scaled": target_width != current_width or target_height != current_height,
                            "region_coords": {"x": origin_x, "y": origin_y, "w": region_width, "h": region_height} if origin_x is not None else None
                        }
                    }
                }
                
            finally:
                # Clean up the temporary image
                if final_image is not None:
                    try:
                        final_image.delete()
                    except (AttributeError, RuntimeError) as e:
                        print(f"Warning: Failed to delete temporary image: {e}")
                        
                # Clean up the temporary file
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)

        except (RuntimeError, AttributeError, OSError, ValueError) as e:
            return {
                "status": "error",
                "error": f"Processing error: {str(e)}",
                "traceback": traceback.format_exc()
            }

    def _get_current_image_metadata(self):
        """Get comprehensive metadata about the current image without bitmap data."""
        try:
            print("Getting current image metadata...")
            
            # Get the current images
            images = Gimp.get_images()
            if not images:
                return {
                    "status": "error",
                    "error": "No images are currently open in GIMP"
                }
            
            # Use the first image (most recently active)
            image = images[0]
            
            # Basic image properties
            width = image.get_width()
            height = image.get_height()
            
            # Get image type and base type
            base_type = image.get_base_type()
            base_type_str = self._base_type_to_string(base_type)
            
            # Get precision and color profile info
            precision = image.get_precision()
            precision_str = self._precision_to_string(precision)
            
            # Get layers information
            layers = image.get_layers()
            layers_info = []
            for i, layer in enumerate(layers):
                try:
                    layer_info = {
                        "name": layer.get_name(),
                        "visible": layer.get_visible(),
                        "opacity": layer.get_opacity(),
                        "width": layer.get_width(),
                        "height": layer.get_height(),
                        "has_alpha": layer.has_alpha(),
                        "is_group": hasattr(layer, 'get_children') and callable(getattr(layer, 'get_children')),
                        "layer_type": self._get_layer_type_string(layer)
                    }
                    # Try to get layer mode if available
                    try:
                        layer_info["blend_mode"] = str(layer.get_mode())
                    except Exception:
                        layer_info["blend_mode"] = "unknown"
                    
                    layers_info.append(layer_info)
                except Exception as layer_error:
                    print(f"Error getting layer {i} info: {layer_error}")
                    layers_info.append({
                        "name": f"Layer {i}",
                        "error": str(layer_error)
                    })
            
            # Get channels information
            channels = image.get_channels()
            channels_info = []
            for i, channel in enumerate(channels):
                try:
                    channel_info = {
                        "name": channel.get_name(),
                        "visible": channel.get_visible(),
                        "opacity": channel.get_opacity(),
                        "color": str(channel.get_color()) if hasattr(channel, 'get_color') else "unknown"
                    }
                    channels_info.append(channel_info)
                except Exception as channel_error:
                    print(f"Error getting channel {i} info: {channel_error}")
                    channels_info.append({
                        "name": f"Channel {i}",
                        "error": str(channel_error)
                    })
            
            # Get paths/vectors information
            paths = []
            try:
                image_paths = image.get_paths()
                for i, path in enumerate(image_paths):
                    try:
                        path_info = {
                            "name": path.get_name(),
                            "visible": path.get_visible(),
                            "num_strokes": len(path.get_strokes()) if hasattr(path, 'get_strokes') else 0
                        }
                        paths.append(path_info)
                    except Exception as path_error:
                        print(f"Error getting path {i} info: {path_error}")
                        paths.append({
                            "name": f"Path {i}",
                            "error": str(path_error)
                        })
            except Exception as paths_error:
                print(f"Error getting paths: {paths_error}")
            
            # Get file information if available
            file_info = {}
            try:
                image_file = image.get_file()
                if image_file:
                    file_info = {
                        "path": image_file.get_path() if hasattr(image_file, 'get_path') else None,
                        "uri": image_file.get_uri() if hasattr(image_file, 'get_uri') else None,
                        "basename": image_file.get_basename() if hasattr(image_file, 'get_basename') else None
                    }
            except Exception as file_error:
                print(f"Error getting file info: {file_error}")
                file_info = {"error": str(file_error)}
            
            # Get resolution information
            resolution_x = resolution_y = None
            try:
                resolution_x, resolution_y = image.get_resolution()
            except Exception as res_error:
                print(f"Error getting resolution: {res_error}")
            
            # Check if image has unsaved changes
            is_dirty = False
            try:
                is_dirty = image.is_dirty()
            except Exception as dirty_error:
                print(f"Error getting dirty status: {dirty_error}")
            
            metadata = {
                "basic": {
                    "width": width,
                    "height": height,
                    "base_type": base_type_str,
                    "precision": precision_str,
                    "resolution_x": resolution_x,
                    "resolution_y": resolution_y,
                    "is_dirty": is_dirty
                },
                "structure": {
                    "num_layers": len(layers),
                    "num_channels": len(channels),
                    "num_paths": len(paths),
                    "layers": layers_info,
                    "channels": channels_info,
                    "paths": paths
                },
                "file": file_info
            }
            
            return {
                "status": "success",
                "results": metadata
            }
            
        except Exception as e:
            error_msg = f"Error getting image metadata: {str(e)}\n{traceback.format_exc()}"
            print(error_msg)
            return {
                "status": "error",
                "error": str(e),
                "traceback": traceback.format_exc()
            }

    def _base_type_to_string(self, base_type):
        """Convert GIMP base type enum to string."""
        try:
            base_type_map = {
                Gimp.ImageBaseType.RGB: "RGB",
                Gimp.ImageBaseType.GRAY: "Grayscale",
                Gimp.ImageBaseType.INDEXED: "Indexed"
            }
            return base_type_map.get(base_type, f"Unknown ({base_type})")
        except Exception:
            return str(base_type)

    def _precision_to_string(self, precision):
        """Convert GIMP precision enum to readable string."""
        try:
            precision_map = {
                100: "u8",      # Gimp.Precision.U8_LINEAR
                150: "u8-gamma", # Gimp.Precision.U8_GAMMA  
                200: "u16",     # Gimp.Precision.U16_LINEAR
                250: "u16-gamma", # Gimp.Precision.U16_GAMMA
                300: "u32",     # Gimp.Precision.U32_LINEAR
                350: "u32-gamma", # Gimp.Precision.U32_GAMMA
                500: "half",    # Gimp.Precision.HALF_LINEAR
                550: "half-gamma", # Gimp.Precision.HALF_GAMMA
                600: "float",   # Gimp.Precision.FLOAT_LINEAR
                650: "float-gamma", # Gimp.Precision.FLOAT_GAMMA
                700: "double",  # Gimp.Precision.DOUBLE_LINEAR
                750: "double-gamma" # Gimp.Precision.DOUBLE_GAMMA
            }
            return precision_map.get(int(precision), f"precision-{precision}")
        except Exception:
            return str(precision)
            
    def _get_layer_type_string(self, layer):
        """Get layer type string with compatibility for different GIMP versions."""
        try:
            # Try different methods to get layer type
            if hasattr(layer, 'get_type'):
                return str(layer.get_type())
            elif hasattr(layer, 'get_image_type'):
                return str(layer.get_image_type())
            elif hasattr(layer, 'type'):
                return str(layer.type)
            else:
                # Fallback - determine from layer properties
                if layer.has_alpha():
                    return "RGBA"
                else:
                    return "RGB"
        except Exception as e:
            print(f"Warning: Could not determine layer type: {e}")
            return "unknown"

    def _get_gimp_info(self):
        """Get comprehensive information about GIMP installation and environment."""
        try:
            print("Getting GIMP environment information...")
            
            gimp_info = {"mcp_plugin_version": PLUGIN_VERSION}

            # Basic GIMP version and build information
            try:
                version_info = {}
                
                # Try different methods to get version info
                try:
                    # Try the version() method if it exists
                    if hasattr(Gimp, 'version'):
                        version_info["version_method"] = str(Gimp.version())
                except Exception as v_error:
                    version_info["version_method_error"] = str(v_error)
                
                # Try to get version from constants if they exist
                for attr in ['MAJOR_VERSION', 'MINOR_VERSION', 'MICRO_VERSION']:
                    try:
                        if hasattr(Gimp, attr):
                            version_info[attr.lower()] = getattr(Gimp, attr)
                    except Exception as attr_error:
                        version_info[f"{attr.lower()}_error"] = str(attr_error)
                
                # Get available version-related attributes
                version_attrs = [attr for attr in dir(Gimp) if 'version' in attr.lower()]
                if version_attrs:
                    version_info["available_version_attributes"] = version_attrs
                
                # Try to get version string from any available source
                version_string = "Unknown"
                try:
                    # Check if there's a version string constant
                    if hasattr(Gimp, 'VERSION'):
                        version_string = str(Gimp.VERSION)
                    elif hasattr(Gimp, 'version_string'):
                        version_string = str(Gimp.version_string())
                    elif hasattr(Gimp, 'get_version'):
                        version_string = str(Gimp.get_version())
                except Exception:
                    pass
                
                version_info["detected_version"] = version_string
                version_info["gimp_module_type"] = str(type(Gimp))
                
                gimp_info["version"] = version_info
                
            except Exception as version_error:
                print(f"Error getting version info: {version_error}")
                gimp_info["version"] = {"error": str(version_error)}
            
            # Installation and directory information
            try:
                directories = {}
                
                # Safely try each directory method
                directory_methods = [
                    ('user_directory', 'directory'),
                    ('system_data_directory', 'data_directory'), 
                    ('locale_directory', 'locale_directory'),
                    ('plugin_directory', 'plug_in_directory'),
                    ('sysconf_directory', 'sysconf_directory')
                ]
                
                for dir_name, method_name in directory_methods:
                    try:
                        if hasattr(Gimp, method_name):
                            method = getattr(Gimp, method_name)
                            if callable(method):
                                directories[dir_name] = str(method())
                            else:
                                directories[dir_name] = str(method)
                        else:
                            directories[f"{dir_name}_not_available"] = True
                    except Exception as method_error:
                        directories[f"{dir_name}_error"] = str(method_error)
                
                # List available directory-related methods
                dir_attrs = [attr for attr in dir(Gimp) if 'dir' in attr.lower()]
                directories["available_directory_methods"] = dir_attrs
                
                gimp_info["directories"] = directories
                
            except Exception as dir_error:
                print(f"Error getting directory info: {dir_error}")
                gimp_info["directories"] = {"error": str(dir_error)}
            
            # Current session information
            try:
                images = Gimp.get_images()
                gimp_info["session"] = {
                    "num_open_images": len(images),
                    "has_open_images": len(images) > 0,
                    "open_image_files": []
                }
                
                # Get file information for open images
                for i, image in enumerate(images):
                    try:
                        image_file = image.get_file()
                        file_info = {
                            "index": i,
                            "width": image.get_width(),
                            "height": image.get_height(),
                            "base_type": self._base_type_to_string(image.get_base_type()),
                            "is_dirty": image.is_dirty() if hasattr(image, 'is_dirty') else None
                        }
                        
                        if image_file:
                            file_info.update({
                                "path": image_file.get_path() if hasattr(image_file, 'get_path') else None,
                                "basename": image_file.get_basename() if hasattr(image_file, 'get_basename') else None
                            })
                        else:
                            file_info["path"] = "Untitled"
                            
                        gimp_info["session"]["open_image_files"].append(file_info)
                    except Exception as image_error:
                        print(f"Error getting image {i} info: {image_error}")
                        gimp_info["session"]["open_image_files"].append({
                            "index": i,
                            "error": str(image_error)
                        })
                        
            except Exception as session_error:
                print(f"Error getting session info: {session_error}")
                gimp_info["session"] = {"error": str(session_error)}
            
            # PDB (Procedure Database) information
            try:
                pdb = Gimp.get_pdb()
                pdb_info = {
                    "available": pdb is not None,
                    "type": str(type(pdb)) if pdb else None
                }
                
                # Try to get some example procedures
                if pdb:
                    sample_procedures = []
                    try:
                        # Test some common procedures
                        test_procs = ['file-png-export', 'gimp-file-save', 'gimp-image-new', 'python-fu-console']
                        for proc_name in test_procs:
                            try:
                                proc = pdb.lookup_procedure(proc_name)
                                sample_procedures.append({
                                    "name": proc_name,
                                    "available": proc is not None,
                                    "type": str(type(proc)) if proc else None
                                })
                            except Exception:
                                sample_procedures.append({
                                    "name": proc_name,
                                    "available": False,
                                    "error": "lookup_failed"
                                })
                    except Exception as proc_error:
                        print(f"Error testing procedures: {proc_error}")
                    
                    pdb_info["sample_procedures"] = sample_procedures
                
                gimp_info["pdb"] = pdb_info
                
            except Exception as pdb_error:
                print(f"Error getting PDB info: {pdb_error}")
                gimp_info["pdb"] = {"error": str(pdb_error)}
            
            # Context and environment information
            try:
                context_info = {}
                
                # Try to get current context information
                try:
                    fg_color = Gimp.context_get_foreground()
                    context_info["foreground_color"] = str(fg_color) if fg_color else None
                except Exception:
                    context_info["foreground_color"] = "unavailable"
                
                try:
                    bg_color = Gimp.context_get_background()
                    context_info["background_color"] = str(bg_color) if bg_color else None
                except Exception:
                    context_info["background_color"] = "unavailable"
                
                try:
                    brush_size = Gimp.context_get_brush_size()
                    context_info["brush_size"] = brush_size if brush_size else None
                except Exception:
                    context_info["brush_size"] = "unavailable"
                
                gimp_info["context"] = context_info
                
            except Exception as context_error:
                print(f"Error getting context info: {context_error}")
                gimp_info["context"] = {"error": str(context_error)}
            
            # Capabilities and features
            try:
                capabilities = {
                    "has_python_console": True,  # We're running Python
                    "mcp_server_running": True,  # We're responding to MCP requests
                    "supports_image_export": True,  # We have the bitmap export function
                    "supports_metadata_export": True,  # We have the metadata function
                    "supports_gimp_info": True,  # We have the gimp info function
                    "api_version": "3.0+",
                    "python_version": sys.version,
                    "available_modules": [],
                    "gimp_module_attributes": len(dir(Gimp)),
                    "gimp_methods": [attr for attr in dir(Gimp) if callable(getattr(Gimp, attr, None))][:20]  # First 20 methods
                }
                
                # Test for available Python modules
                test_modules = ['gi.repository.Gimp', 'gi.repository.Gegl', 'gi.repository.Gio', 'json', 'base64', 'tempfile']
                for module_name in test_modules:
                    try:
                        if module_name == 'gi.repository.Gimp':
                            # Already imported
                            capabilities["available_modules"].append({"name": module_name, "available": True})
                        elif module_name == 'gi.repository.Gegl':
                            from gi.repository import Gegl  # noqa: F401
                            capabilities["available_modules"].append({"name": module_name, "available": True})
                        elif module_name == 'gi.repository.Gio':
                            from gi.repository import Gio  # noqa: F401
                            capabilities["available_modules"].append({"name": module_name, "available": True})
                        else:
                            __import__(module_name)
                            capabilities["available_modules"].append({"name": module_name, "available": True})
                    except ImportError:
                        capabilities["available_modules"].append({"name": module_name, "available": False})
                    except Exception as mod_error:
                        capabilities["available_modules"].append({"name": module_name, "available": False, "error": str(mod_error)})
                
                gimp_info["capabilities"] = capabilities
                
            except Exception as cap_error:
                print(f"Error getting capabilities: {cap_error}")
                gimp_info["capabilities"] = {"error": str(cap_error)}
            
            # System and platform information
            try:
                
                system_info = {
                    "platform": platform.platform(),
                    "system": platform.system(),
                    "machine": platform.machine(),
                    "python_version": platform.python_version(),
                    "environment_vars": {
                        "HOME": os.environ.get("HOME"),
                        "USER": os.environ.get("USER"), 
                        "GIMP_PLUG_IN_DIR": os.environ.get("GIMP_PLUG_IN_DIR"),
                        "GIMP_DATA_DIR": os.environ.get("GIMP_DATA_DIR")
                    }
                }
                
                gimp_info["system"] = system_info
                
            except Exception as sys_error:
                print(f"Error getting system info: {sys_error}")
                gimp_info["system"] = {"error": str(sys_error)}
            
            return {
                "status": "success",
                "results": gimp_info
            }
            
        except Exception as e:
            error_msg = f"Error getting GIMP info: {str(e)}\n{traceback.format_exc()}"
            return {
                "status": "error",
                "error": error_msg,
                "traceback": traceback.format_exc()
            }

    def _get_context_state(self):
        """Get current GIMP context state (colors, brush, tool settings)."""
        try:
            print("Getting GIMP context state...")

            context_state = {}

            # Get foreground and background colors
            try:
                fg_color = Gimp.context_get_foreground()
                bg_color = Gimp.context_get_background()

                # Convert colors to RGB values
                context_state["foreground_color"] = {
                    "color_object": str(fg_color),
                    "description": "Current foreground color"
                }
                context_state["background_color"] = {
                    "color_object": str(bg_color),
                    "description": "Current background color"
                }

                # Try to get RGB values if possible
                try:
                    if hasattr(fg_color, 'get_rgba'):
                        rgba = fg_color.get_rgba()
                        context_state["foreground_color"]["rgba"] = list(rgba) if rgba else None
                except Exception as color_error:
                    context_state["foreground_color"]["rgba_error"] = str(color_error)

                try:
                    if hasattr(bg_color, 'get_rgba'):
                        rgba = bg_color.get_rgba()
                        context_state["background_color"]["rgba"] = list(rgba) if rgba else None
                except Exception as color_error:
                    context_state["background_color"]["rgba_error"] = str(color_error)

            except Exception as color_err:
                context_state["colors_error"] = str(color_err)

            # Get brush information
            try:
                brush = Gimp.context_get_brush()
                if brush:
                    context_state["brush"] = {
                        "name": brush.get_name() if hasattr(brush, 'get_name') else str(brush),
                        "description": "Current brush"
                    }
            except Exception as brush_err:
                context_state["brush_error"] = str(brush_err)

            # Get opacity
            try:
                opacity = Gimp.context_get_opacity()
                context_state["opacity"] = {
                    "value": opacity,  # Already in percentage (0-100)
                    "description": "Current opacity percentage (0-100)"
                }
            except Exception as opacity_err:
                context_state["opacity_error"] = str(opacity_err)

            # Get paint mode
            try:
                paint_mode = Gimp.context_get_paint_mode()
                context_state["paint_mode"] = {
                    "value": str(paint_mode),
                    "description": "Current paint/blend mode"
                }
            except Exception as mode_err:
                context_state["paint_mode_error"] = str(mode_err)

            # Get feather setting (if available)
            try:
                feather = Gimp.context_get_feather()
                feather_radius = Gimp.context_get_feather_radius()
                context_state["feather"] = {
                    "enabled": feather,
                    "radius": feather_radius,
                    "description": "Selection feathering state"
                }
            except Exception:
                context_state["feather_note"] = "Feather settings not available in context"

            # Get antialias setting
            try:
                antialias = Gimp.context_get_antialias()
                context_state["antialias"] = {
                    "enabled": antialias,
                    "description": "Antialiasing state for selections"
                }
            except Exception:
                context_state["antialias_note"] = "Antialias setting not available"

            return {
                "status": "success",
                "results": context_state
            }

        except Exception as e:
            error_msg = f"Error getting context state: {str(e)}\n{traceback.format_exc()}"
            return {
                "status": "error",
                "error": error_msg,
                "traceback": traceback.format_exc()
            }

    def _bind_listener(self):
        """Create, bind and listen on the server socket. Raises OSError on failure."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(1.0)
            sock.bind((self.host, self.port))
            sock.listen(1)
        except OSError:
            sock.close()
            raise
        return sock

    def _rebind(self):
        """Replace the listening socket. Runs only on the accept thread.

        Clients already connected keep their own sockets. If the port can't
        be bound again the server stops, and the main loop is quit so the
        plug-in process exits instead of idling without a server.
        """
        print("Restarting MCP server socket...")
        if self.socket:
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None
        try:
            self.socket = self._bind_listener()
            print(f"MCP server restarted on {self.host}:{self.port}")
            self._restart_result = {
                "status": "success",
                "results": {"restarted": True, "host": self.host, "port": self.port}
            }
        except OSError as e:
            print(f"MCP server restart failed, server stopped: {e}")
            self.running = False
            self._restart_result = {
                "status": "error",
                "error": f"Restart failed, the MCP server has stopped: {e}"
            }
            if getattr(self, "_glib_loop", None):
                self._glib_loop.quit()
        self._restart_requested.clear()
        self._restart_done.set()

    def _restart_server(self):
        """Re-bind the listening socket of the server running in this process.

        The accept thread does the re-bind (see _rebind) so the socket is never
        replaced under a running accept(). Called from a client thread.
        """
        if not self.running or self.socket is None:
            return {"status": "error", "error": "The MCP server is not running in this process"}
        self._restart_done.clear()
        self._restart_requested.set()
        # The accept loop checks for a restart at least once a second
        # (the listening socket's accept timeout).
        if not self._restart_done.wait(timeout=5.0):
            return {"status": "error", "error": "Restart timed out waiting for the server thread"}
        return self._restart_result

    def _new_canvas(self, params):
        """Create a new blank canvas and open it in a GIMP display window."""
        try:
            width  = int(params.get("width", 1024))
            height = int(params.get("height", 1024))
            name   = str(params.get("name", "Untitled"))
            color_mode = str(params.get("color_mode", "RGB")).upper()
            # A null fill means "not given", not the string "None" (which is
            # transparent).
            fill   = params.get("fill")
            fill   = "white" if fill is None else str(fill)
            resolution = int(params.get("resolution", 72))

            mode_map = {
                "RGB":   Gimp.ImageBaseType.RGB,
                "RGBA":  Gimp.ImageBaseType.RGB,
                "GRAY":  Gimp.ImageBaseType.GRAY,
                "GRAYA": Gimp.ImageBaseType.GRAY,
            }
            layer_type_map = {
                "RGB":   Gimp.ImageType.RGB_IMAGE,
                "RGBA":  Gimp.ImageType.RGBA_IMAGE,
                "GRAY":  Gimp.ImageType.GRAY_IMAGE,
                "GRAYA": Gimp.ImageType.GRAYA_IMAGE,
            }
            base_type  = mode_map.get(color_mode, Gimp.ImageBaseType.RGB)
            layer_type = layer_type_map.get(color_mode, Gimp.ImageType.RGB_IMAGE)

            image = Gimp.Image.new(width, height, base_type)
            image.set_resolution(resolution, resolution)

            layer = Gimp.Layer.new(image, name, width, height, layer_type, 100, Gimp.LayerMode.NORMAL)
            image.insert_layer(layer, None, 0)

            # Drawable.fill ignores the context's opacity and paint mode, which
            # an earlier call may have changed; edit_fill would blend with them.
            try:
                if is_transparent(fill):
                    layer.add_alpha()
                    self._check_ok(layer.fill(Gimp.FillType.TRANSPARENT), "Filling the new canvas")
                else:
                    bg_color = self._color(fill)
                    Gimp.context_push()
                    try:
                        Gimp.context_set_background(bg_color)
                        self._check_ok(layer.fill(Gimp.FillType.BACKGROUND), "Filling the new canvas")
                    finally:
                        Gimp.context_pop()
            except Exception:
                # Don't leave a half-made image behind with no display.
                image.delete()
                raise

            display_opened = self._create_display(image)

            print(f"New canvas created: {width}x{height} {color_mode} fill={fill}")
            return {
                "status": "success",
                "results": {
                    "image_id": image.get_id(),
                    "width": width,
                    "height": height,
                    "color_mode": color_mode,
                    "fill": fill,
                    "resolution": resolution,
                    "display_opened": display_opened
                }
            }
        except Exception as e:
            return {
                "status": "error",
                "error": f"new_canvas failed: {str(e)}",
                "traceback": traceback.format_exc()
            }


    # =========================================================================
    # SHARED HELPERS
    # =========================================================================

    def _create_display(self, image):
        """Open a display for image; return True if one was created.

        Headless runs (gimp -i) have no display backend: Gimp.Display.new()
        raises "constructor returned NULL" there even though the image itself
        is fine, so a failure here must not fail the calling operation.

        The display is recorded in self._displays (image id -> Gimp.Display):
        GIMP 3.2 can't list an image's displays, so close_image and
        set_active_image can only reach windows the plugin opened.
        """
        try:
            display = Gimp.Display.new(image)
        except Exception as display_err:
            print(f"[MCP] Display creation skipped (headless?): {display_err}")
            return False
        self._displays[image.get_id()] = display
        # The display exists at this point; a failed refresh must not make the
        # caller report that no display was opened.
        try:
            Gimp.displays_flush()
        except Exception as flush_err:
            print(f"[MCP] displays_flush failed after opening display: {flush_err}")
        return True

    def _render_visible(self, image, x, y, width, height):
        """Return a temporary image holding the visible composite of a rectangle of image.

        The caller deletes it. Commands run one at a time under command_lock, so
        no other command sees the temporary image in Gimp.get_images().
        """
        # Render at 8 bits for an 8-bit source and at 16 bits for anything
        # deeper: PNG holds at most 16 bits, so deeper sources give the same PNG
        # at 16 bits, in half the memory for 32-bit and float and a quarter for
        # double.
        # The image is non-linear sRGB: new_from_visible converts into it, so
        # the PNG's pixels are sRGB as PNG readers assume. Copying the source's
        # profile instead would leave e.g. Adobe RGB values in a PNG tagged
        # sRGB, since file-png-export doesn't embed profiles by default.
        # Precision values are 100 * component type (1 = U8, 2 = U16, higher =
        # deeper) plus 0 (linear), 50 (non-linear) or 75 (perceptual).
        component_type = int(image.get_precision()) // 100
        precision = Gimp.Precision(min(component_type, 2) * 100 + 50)
        temp = self._check_item(
            Gimp.Image.new_with_precision(width, height, Gimp.ImageBaseType.RGB, precision),
            "Creating the snapshot image")
        layer, inserted = None, False
        try:
            # No undo history: it would keep the full-size render alive after
            # the crop and scale below
            temp.undo_disable()
            # new_from_visible renders the whole canvas, and the rectangle is cut
            # out afterwards, so a small region of a large image still costs a
            # full-size composite (8000x8000 RGBA is about 256 MB at 8 bits)
            layer = self._check_item(Gimp.Layer.new_from_visible(image, temp, "visible"),
                                     "Rendering the visible layers")
            self._check_ok(temp.insert_layer(layer, None, 0), "Adding the visible layer")
            inserted = True
            self._check_ok(layer.set_offsets(-x, -y), "Positioning the visible layer")
            self._check_ok(layer.resize_to_image_size(), "Cropping the visible layer")
        except Exception:
            # Deleting the image frees only the layers it holds. Report the
            # original error, not one from the cleanup.
            cleanup = [temp.delete]
            if layer is not None and not inserted:
                cleanup.insert(0, layer.delete)
            for delete in cleanup:
                try:
                    delete()
                except Exception:
                    pass
            raise
        return temp

    def _get_image(self, image_index):
        """Return the image at image_index from Gimp.get_images(), raise if none open."""
        images = Gimp.get_images()
        if not images:
            raise RuntimeError("No images are currently open in GIMP")
        if image_index >= len(images):
            raise RuntimeError(f"image_index {image_index} out of range (only {len(images)} images open)")
        return images[image_index]

    def _lookup_pdb(self, name):
        """Return the PDB procedure `name`, raise if GIMP doesn't have it.

        For call sites with no alternative. Where a missing procedure has a
        fallback (another API, a GEGL op), use lookup_procedure() directly.
        """
        proc = Gimp.get_pdb().lookup_procedure(name)
        if proc is None:
            raise RuntimeError(f"PDB procedure '{name}' not found")
        return proc

    def _run_pdb(self, proc, cfg):
        """Run a PDB procedure, raise if it returns a non-SUCCESS status.

        Gimp.Procedure.run() does not raise when GIMP rejects a call (locked
        layer, layer group, invalid argument); it returns a Gimp.ValueArray whose
        first value is the status. Without this check a handler would report
        success while the image stays unchanged (issue #90).
        """
        result = proc.run(cfg)
        status = result.index(0)
        if status != Gimp.PDBStatusType.SUCCESS:
            # On failure GIMP puts the error message, if any, in the second value.
            msg = None
            try:
                if result.length() > 1:
                    msg = result.index(1)
            except Exception:
                pass
            nick = getattr(status, "value_nick", None) or str(status)
            detail = f": {msg}" if isinstance(msg, str) and msg else ""
            raise RuntimeError(f"{proc.get_name()} failed ({nick}){detail}")
        return result

    def _check_ok(self, ok, what):
        """Raise if a direct libgimp call returned False.

        Methods such as drawable.curves_spline() wrap a PDB procedure and, like
        Procedure.run(), don't raise when GIMP rejects the call: they return
        False. GIMP's reason is then available from the PDB's last error.
        """
        if ok is False:
            msg = None
            try:
                msg = Gimp.get_pdb().get_last_error()
            except Exception:
                pass
            raise RuntimeError(f"{what} failed" + (f": {msg}" if msg else ""))

    def _check_item(self, item, what):
        """Return item, raise if a libgimp call that returns an item gave None.

        image.flatten() and image.merge_visible_layers() return the resulting
        layer rather than a success flag; when GIMP rejects the call they
        return None, which _check_ok() would let through.
        """
        if item is None:
            self._check_ok(False, what)
        return item

    def _resolve_layer(self, image, layer_name, layer_index):
        """Resolve a layer by name, index, or fall back to the active layer."""
        if layer_name is not None:
            layers = image.get_layers()
            for layer in layers:
                if layer.get_name() == layer_name:
                    return layer
            raise RuntimeError(f"Layer '{layer_name}' not found")
        if layer_index is not None:
            layers = image.get_layers()
            if layer_index >= len(layers):
                raise RuntimeError(f"layer_index {layer_index} out of range")
            return layers[layer_index]
        layer = (image.get_selected_layers() or image.get_layers() or [None])[0]
        if layer is None:
            layers = image.get_layers()
            if not layers:
                raise RuntimeError("No layers in image")
            return layers[0]
        return layer

    def _channel_ops_from_string(self, op):
        """Map operation string to Gimp.ChannelOps enum value."""
        return {
            "replace":   Gimp.ChannelOps.REPLACE,
            "add":       Gimp.ChannelOps.ADD,
            "subtract":  Gimp.ChannelOps.SUBTRACT,
            "intersect": Gimp.ChannelOps.INTERSECT,
        }.get(op.lower(), Gimp.ChannelOps.REPLACE)

    def _interp_from_string(self, interp):
        """Map interpolation string to Gimp.InterpolationType."""
        return {
            "cubic":  Gimp.InterpolationType.CUBIC,
            "linear": Gimp.InterpolationType.LINEAR,
            "none":   Gimp.InterpolationType.NONE,
        }.get(interp.lower(), Gimp.InterpolationType.CUBIC)

    # GIMP 3 export procedures. The GIMP 2 file-*-save names don't exist in 3.x.
    EXPORT_PROCS = {
        "png":  "file-png-export",
        "jpeg": "file-jpeg-export",
        "jpg":  "file-jpeg-export",
        "webp": "file-webp-export",
        "tiff": "file-tiff-export",
        "tif":  "file-tiff-export",
        "bmp":  "file-bmp-export",
    }

    def _check_export_args(self, fmt, quality):
        """Validate an export format and quality; return (format, procedure name).

        Export tools call this before doing any work, so a bad format fails the
        whole call instead of each image, and before writing any files.
        """
        fmt_lower = str(fmt).lower()
        proc_name = self.EXPORT_PROCS.get(fmt_lower)
        if proc_name is None:
            supported = ", ".join(sorted(self.EXPORT_PROCS))
            raise RuntimeError(f"Unsupported export format '{fmt}' (supported: {supported})")
        if fmt_lower in ("jpeg", "jpg", "webp") and not 0 <= quality <= 100:
            # GLib only warns about an out-of-range value and keeps the default.
            raise RuntimeError(f"quality must be between 0 and 100, got {quality}")
        return fmt_lower, proc_name

    def _export_to_path(self, image, file_path, fmt, quality, flatten):
        """Export image to file_path in the given format. Returns file size in bytes.

        An unknown format or a missing export procedure raises, instead of
        writing PNG data under the requested name (issue #64, item 5).
        """
        from gi.repository import Gio
        fmt_lower, proc_name = self._check_export_args(fmt, quality)
        if not image.get_layers():
            # Exporting an image without layers crashes GIMP's file plug-ins
            # (reported in #63), and flatten() on it fails, so check first.
            raise RuntimeError("Cannot export an image that has no layers")
        should_delete = False
        try:
            if flatten:
                image = image.duplicate()
                should_delete = True
                self._check_item(image.flatten(), "Flattening the export copy")
            proc = self._lookup_pdb(proc_name)
            cfg = proc.create_config()
            cfg.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
            cfg.set_property("image", image)
            cfg.set_property("file", Gio.File.new_for_path(file_path))
            # Quality ranges differ: file-jpeg-export takes 0-1, file-webp-export 0-100.
            if fmt_lower in ("jpeg", "jpg"):
                cfg.set_property("quality", quality / 100.0)
            elif fmt_lower == "webp":
                cfg.set_property("quality", float(quality))
            self._run_pdb(proc, cfg)
            return os.path.getsize(file_path)
        finally:
            if should_delete:
                try:
                    image.delete()
                except Exception:
                    pass

    def _push_paint_context(self):
        """Push the context and reset the settings that change how paint lands.

        context_push() starts from a copy of the current context, so an
        opacity or paint mode left by an earlier call (e.g. via call_api)
        would otherwise blend every fill and stroke. Pair with context_pop().
        """
        Gimp.context_push()
        Gimp.context_set_opacity(100.0)
        Gimp.context_set_paint_mode(Gimp.LayerMode.NORMAL)

    def _apply_gegl_filter(self, image, drawable, op_name, props):
        """Apply a GEGL operation to a drawable and merge it destructively.

        GEGL properties are set on the filter's config, not on the filter
        itself. Unknown property names and out-of-range values raise instead
        of being skipped, since GLib only warns about them and keeps the old
        value, which would let a filter silently do the wrong thing.
        """
        filtr = Gimp.DrawableFilter.new(drawable, op_name, op_name)
        if filtr is None:
            raise RuntimeError(f"Could not create GEGL filter {op_name!r}")
        try:
            config = filtr.get_config()
            for key, value in props.items():
                pspec = config.find_property(key)
                if pspec is None:
                    raise ValueError(f"{op_name} has no property {key!r}")
                lo = getattr(pspec, "minimum", None)
                hi = getattr(pspec, "maximum", None)
                if isinstance(value, (int, float)) and lo is not None and not lo <= value <= hi:
                    raise ValueError(f"{op_name} {key}={value} is outside [{lo}, {hi}]")
                config.set_property(key, value)
            filtr.update()
            drawable.merge_filter(filtr)
        except Exception:
            # GIMP keeps an unmerged filter alive until it is deleted.
            if filtr.is_valid():
                filtr.delete()
            raise

    # =========================================================================
    # CATEGORY 1 — File Operations
    # =========================================================================

    def _open_image(self, params):
        """Open an image file, create a display, return metadata."""
        try:
            from gi.repository import Gio
            file_path = params.get("file_path", "")
            gio_file = Gio.File.new_for_path(file_path)
            image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, gio_file)
            if image is None:
                return {"status": "error", "error": f"Could not open file: {file_path}"}
            display_opened = self._create_display(image)
            base_type = image.get_base_type()
            mode_map = {
                Gimp.ImageBaseType.RGB:     "RGB",
                Gimp.ImageBaseType.GRAY:    "Grayscale",
                Gimp.ImageBaseType.INDEXED: "Indexed",
            }
            return {
                "status": "success",
                "results": {
                    "image_id":      image.get_id(),
                    "width":         image.get_width(),
                    "height":        image.get_height(),
                    "color_mode":    mode_map.get(base_type, str(base_type)),
                    "num_layers":    len(image.get_layers()),
                    "display_opened": display_opened,
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _open_image_as_layer(self, params):
        """Import an image file as one new layer in an open image.

        The file is loaded into a temporary image with no display, and
        Gimp.Layer.new_from_visible() turns its visible composite into a layer
        of the target image. That keeps transparency, converts to the target's
        color mode, and keeps content where it sits on the source canvas.
        """
        try:
            from gi.repository import Gio
            file_path = params.get("file_path", "")
            if not file_path:
                return {"status": "error", "error": "file_path is required"}
            image_index = int(params.get("image_index", 0))
            layer_name = (params.get("layer_name")
                          or os.path.splitext(os.path.basename(file_path))[0])
            position = int(params.get("position", 0))
            if position < 0:
                return {"status": "error",
                        "error": f"position must be >= 0 (0 = top), got {position}"}
            scale = str(params.get("scale") or "none").lower()
            if scale not in ("none", "fit", "canvas"):
                return {"status": "error",
                        "error": f"Invalid scale '{scale}' (use 'none', 'fit' or 'canvas')"}
            offset_x = params.get("offset_x")
            offset_y = params.get("offset_y")

            # A relative path would resolve against GIMP's working directory, which
            # the client can't know. (The MCP server expands "~": GIMP's own home
            # can differ, e.g. a Snap GIMP's.)
            if not os.path.isabs(file_path):
                return {"status": "error",
                        "error": f"file_path must be an absolute path, got '{file_path}'"}
            if os.path.isdir(file_path):
                return {"status": "error",
                        "error": f"{file_path} is a directory, not an image file"}
            # GIMP's own error for a missing file is cryptic ("... out of range").
            if not os.path.isfile(file_path):
                return {"status": "error", "error": f"File not found: {file_path}"}
            # Resolve the target before loading: Gimp.get_images() lists the
            # newest image first, so the temporary image would shift the indices.
            target = self._get_image(image_index)
            canvas_w, canvas_h = target.get_width(), target.get_height()
            loaded = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(file_path))
            # GIMP returns None for a missing file and for one it can't read;
            # _check_item adds GIMP's own error message, which tells them apart.
            self._check_item(loaded, f"Opening {file_path}")
            # Everything up to insert_layer() works on the temporary image or on a
            # layer that isn't in the target yet, so a failure leaves the target
            # untouched: no layer, no undo step, not marked dirty.
            layer = None
            inserted = False
            try:
                try:
                    src_w, src_h = loaded.get_width(), loaded.get_height()
                    if scale != "none" and loaded.get_base_type() == Gimp.ImageBaseType.INDEXED:
                        # GIMP never interpolates indexed images, so scaling one as
                        # it is gives hard, blocky edges.
                        self._check_ok(loaded.convert_rgb(), "Converting the image to RGB")
                    if scale == "fit":
                        factor = min(canvas_w / src_w, canvas_h / src_h)
                        self._check_ok(loaded.scale(max(1, round(src_w * factor)),
                                                    max(1, round(src_h * factor))),
                                       "Scaling the image")
                    elif scale == "canvas":
                        self._check_ok(loaded.scale(canvas_w, canvas_h), "Scaling the image")
                    layer = self._check_item(Gimp.Layer.new_from_visible(loaded, target, layer_name),
                                             "Creating a layer from the file")
                finally:
                    # The temporary image never had a display, so delete() removes it.
                    loaded.delete()
                w, h = layer.get_width(), layer.get_height()
                x = int(offset_x) if offset_x is not None else (canvas_w - w) // 2
                y = int(offset_y) if offset_y is not None else (canvas_h - h) // 2
                self._check_ok(layer.set_offsets(x, y), "Moving the layer")
                self._check_ok(target.insert_layer(layer, None, position), "Inserting the layer")
                inserted = True
            except Exception:
                # A layer that never made it into the target must be freed here.
                # (Its get_image() is already the target, so it can't tell us.)
                if layer is not None and not inserted:
                    try:
                        layer.delete()
                    except Exception:
                        pass
                raise
            Gimp.displays_flush()
            _, actual_x, actual_y = layer.get_offsets()
            return {
                "status": "success",
                "results": {
                    "layer_name":    layer.get_name(),
                    "layer_id":      layer.get_id(),
                    "width":         layer.get_width(),
                    "height":        layer.get_height(),
                    "offset_x":      actual_x,
                    "offset_y":      actual_y,
                    "position":      target.get_item_position(layer),
                    "source_width":  src_w,
                    "source_height": src_h,
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _save_xcf(self, params):
        """Save image as XCF."""
        try:
            from gi.repository import Gio
            image_index = int(params.get("image_index", 0))
            file_path = params.get("file_path", "")
            image = self._get_image(image_index)
            gio_file = Gio.File.new_for_path(file_path)
            pdb = Gimp.get_pdb()
            proc = pdb.lookup_procedure("gimp-xcf-save")
            if proc:
                cfg = proc.create_config()
                cfg.set_property("image", image)
                cfg.set_property("file", gio_file)
                self._run_pdb(proc, cfg)
            else:
                self._check_ok(Gimp.file_overwrite(Gimp.RunMode.NONINTERACTIVE, image, gio_file),
                               "Saving the image")
            return {"status": "success", "results": {"status": "success", "file_path": file_path}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _export_image(self, params):
        """Export image to raster format."""
        try:
            image_index = int(params.get("image_index", 0))
            file_path   = params.get("file_path", "")
            fmt         = params.get("format", "png")
            quality     = int(params.get("quality", 90))
            flatten     = bool(params.get("flatten", True))
            image = self._get_image(image_index)
            file_size = self._export_to_path(image, file_path, fmt, quality, flatten)
            Gimp.displays_flush()
            return {
                "status": "success",
                "results": {
                    "status": "success",
                    "file_path": file_path,
                    "format": fmt,
                    "file_size_bytes": file_size,
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _batch_export(self, params):
        """Export all (or one) open images to output_dir."""
        try:
            output_dir   = params.get("output_dir", "")
            fmt          = params.get("format", "png")
            quality      = int(params.get("quality", 90))
            name_pattern = params.get("name_pattern", "{name}")
            image_index  = params.get("image_index", None)
            self._check_export_args(fmt, quality)

            images = Gimp.get_images()
            if not images:
                return {"status": "error", "error": "No images open"}

            targets = [(i, img) for i, img in enumerate(images)]
            if image_index is not None:
                targets = [(image_index, images[int(image_index)])]

            os.makedirs(output_dir, exist_ok=True)
            exported = []
            errors = []

            for idx, image in targets:
                try:
                    gio_file = image.get_file()
                    raw_name = gio_file.get_basename().rsplit(".", 1)[0] if gio_file else f"image_{idx}"
                    filename = name_pattern.format(name=raw_name, index=idx) + f".{fmt}"
                    out_path = os.path.join(output_dir, filename)
                    self._export_to_path(image, out_path, fmt, quality, True)
                    exported.append({
                        "file_path": out_path,
                        "name": raw_name,
                        "width": image.get_width(),
                        "height": image.get_height(),
                    })
                except Exception as ex:
                    errors.append({"index": idx, "error": str(ex)})

            return {
                "status": "success",
                "results": {"exported": exported, "count": len(exported), "errors": errors}
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 2 — Image Adjustments
    # =========================================================================

    def _auto_levels(self, params):
        """Auto-stretch levels on a drawable."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                # No fallback: levels with fixed parameters can't auto-stretch,
                # so a missing procedure must be an error, not a no-op (#113).
                proc = self._lookup_pdb("gimp-drawable-levels-stretch")
                cfg = proc.create_config()
                cfg.set_property("drawable", drawable)
                self._run_pdb(proc, cfg)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _adjust_curves(self, params):
        """Adjust tonal curves."""
        try:
            PRESETS = {
                "s_curve":  [0, 0, 64, 50, 192, 210, 255, 255],
                "lighten":  [0, 0, 128, 180, 255, 255],
                "darken":   [0, 0, 128, 75, 255, 255],
                "contrast": [0, 0, 64, 40, 192, 215, 255, 255],
            }
            CHANNEL_MAP = {
                "value": Gimp.HistogramChannel.VALUE,
                "red":   Gimp.HistogramChannel.RED,
                "green": Gimp.HistogramChannel.GREEN,
                "blue":  Gimp.HistogramChannel.BLUE,
                "alpha": Gimp.HistogramChannel.ALPHA,
            }
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            preset      = params.get("preset", "s_curve")
            custom_pts  = params.get("points", None)
            channel_str = params.get("channel", "value")

            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            channel  = CHANNEL_MAP.get(channel_str.lower(), Gimp.HistogramChannel.VALUE)

            if custom_pts is not None:
                # Flatten [[in,out],...] -> [in,out,in,out,...]
                if custom_pts and isinstance(custom_pts[0], (list, tuple)):
                    flat = []
                    for pt in custom_pts:
                        flat.extend(pt)
                    control_pts = flat
                else:
                    control_pts = list(custom_pts)
            else:
                control_pts = PRESETS.get(preset, PRESETS["s_curve"])

            image.undo_group_start()
            try:
                pts_normalized = [p / 255.0 for p in control_pts]
                # set_property can't auto-convert Python list to GimpDoubleArray.
                # Try calling curves_spline as a direct method on the drawable
                # (GI exposes gimp-drawable-curves-spline as drawable.curves_spline).
                try:
                    ok = drawable.curves_spline(channel, pts_normalized)
                except Exception:
                    ok = None  # the binding rejected the list; use the PDB below
                if ok is not None:
                    self._check_ok(ok, "curves_spline")
                else:
                    # Fallback: use array.array typed buffer which GI may accept
                    import array as _arr
                    typed = _arr.array('d', pts_normalized)
                    proc = self._lookup_pdb("gimp-drawable-curves-spline")
                    cfg = proc.create_config()
                    cfg.set_property("drawable", drawable)
                    cfg.set_property("channel",  channel)
                    cfg.set_property("points",   typed)
                    self._run_pdb(proc, cfg)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _adjust_brightness_contrast(self, params):
        """Adjust brightness and contrast."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            brightness  = float(params.get("brightness", 0))
            contrast    = float(params.get("contrast", 0))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            # gimp-drawable-brightness-contrast works in linear light on GIMP 3,
            # so even +8/+12 crushes everything below ~56/255 to black. Apply the
            # same formula as a perceptual-space LUT through curves instead.
            import math
            b = max(-1.0, min(1.0, brightness / 127.0)) / 2.0
            slant = math.tan((max(-1.0, min(1.0, contrast / 127.0)) + 1.0) * math.pi / 4.0)
            lut = []
            for i in range(256):
                v = i / 255.0
                v = v * (1.0 + b) if b < 0.0 else v + (1.0 - v) * b
                v = (v - 0.5) * slant + 0.5
                lut.append(min(1.0, max(0.0, v)))
            image.undo_group_start()
            try:
                if not drawable.curves_explicit(Gimp.HistogramChannel.VALUE, lut):
                    raise Exception("gimp-drawable-curves-explicit failed")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _adjust_hue_saturation(self, params):
        """Adjust hue, saturation, lightness."""
        try:
            HUE_RANGE_MAP = {
                "all":     Gimp.HueRange.ALL,
                "red":     Gimp.HueRange.RED,
                "yellow":  Gimp.HueRange.YELLOW,
                "green":   Gimp.HueRange.GREEN,
                "cyan":    Gimp.HueRange.CYAN,
                "blue":    Gimp.HueRange.BLUE,
                "magenta": Gimp.HueRange.MAGENTA,
            }
            image_index  = int(params.get("image_index", 0))
            layer_name   = params.get("layer_name", None)
            hue          = float(params.get("hue", 0))
            saturation   = float(params.get("saturation", 0))
            lightness    = float(params.get("lightness", 0))
            color_range  = params.get("color_range", "all")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            hue_range = HUE_RANGE_MAP.get(color_range.lower(), Gimp.HueRange.ALL)
            image.undo_group_start()
            try:
                proc = self._lookup_pdb("gimp-drawable-hue-saturation")
                cfg = proc.create_config()
                cfg.set_property("drawable", drawable)
                cfg.set_property("hue-range", hue_range)
                cfg.set_property("hue-offset", hue)
                cfg.set_property("lightness",  lightness)
                cfg.set_property("saturation", saturation)
                cfg.set_property("overlap", 0.0)
                self._run_pdb(proc, cfg)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _adjust_color_balance(self, params):
        """Adjust color balance for shadows/midtones/highlights."""
        try:
            # GIMP 3.2 uses integer constants for color-range (0=shadows,1=midtones,2=highlights)
            RANGE_MAP = {
                "shadows":    0,
                "midtones":   1,
                "highlights": 2,
            }
            image_index    = int(params.get("image_index", 0))
            layer_name     = params.get("layer_name", None)
            cyan_red       = float(params.get("cyan_red", 0))
            magenta_green  = float(params.get("magenta_green", 0))
            yellow_blue    = float(params.get("yellow_blue", 0))
            range_str      = params.get("range", "midtones")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            color_range = RANGE_MAP.get(range_str.lower(), 1)
            image.undo_group_start()
            try:
                proc = self._lookup_pdb("gimp-drawable-color-balance")
                cfg = proc.create_config()
                cfg.set_property("drawable",      drawable)
                cfg.set_property("transfer-mode", color_range)
                cfg.set_property("cyan-red",      cyan_red)
                cfg.set_property("magenta-green", magenta_green)
                cfg.set_property("yellow-blue",   yellow_blue)
                cfg.set_property("preserve-lum",  True)
                self._run_pdb(proc, cfg)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _sharpen(self, params):
        """Sharpen using unsharp mask."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            amount      = float(params.get("amount", 50.0))
            radius      = float(params.get("radius", 3.0))
            threshold   = int(params.get("threshold", 0))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                pdb = Gimp.get_pdb()
                proc = pdb.lookup_procedure("plug-in-unsharp-mask")
                if proc:
                    cfg = proc.create_config()
                    cfg.set_property("image",     image)
                    cfg.set_property("drawable",  drawable)
                    cfg.set_property("radius",    radius)
                    cfg.set_property("amount",    amount / 100.0)
                    cfg.set_property("threshold", threshold)
                    self._run_pdb(proc, cfg)
                else:
                    self._apply_gegl_filter(image, drawable, "gegl:unsharp-mask", {
                        "std-dev": radius,
                        "scale":   amount / 100.0,
                        "threshold": threshold / 255.0,
                    })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _blur(self, params):
        """Gaussian blur."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            radius_x    = float(params.get("radius_x", 5.0))
            radius_y    = float(params.get("radius_y", 5.0))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                pdb = Gimp.get_pdb()
                proc = pdb.lookup_procedure("plug-in-gauss")
                if proc:
                    cfg = proc.create_config()
                    cfg.set_property("image",    image)
                    cfg.set_property("drawable", drawable)
                    cfg.set_property("horizontal", int(radius_x * 2 + 1))
                    cfg.set_property("vertical",   int(radius_y * 2 + 1))
                    cfg.set_property("method",     0)
                    self._run_pdb(proc, cfg)
                else:
                    self._apply_gegl_filter(image, drawable, "gegl:gaussian-blur", {
                        "std-dev-x": radius_x,
                        "std-dev-y": radius_y,
                    })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _denoise(self, params):
        """Noise reduction."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            strength    = int(params.get("strength", 50))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._apply_gegl_filter(image, drawable, "gegl:noise-reduction", {
                    "iterations": max(1, strength // 20),
                })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _desaturate(self, params):
        """Desaturate a layer."""
        try:
            # GIMP 3.2: LUMINOSITY was renamed to LUMINANCE
            MODE_MAP = {
                "luminosity": Gimp.DesaturateMode.LUMINANCE,
                "luminance":  Gimp.DesaturateMode.LUMINANCE,
                "luma":       Gimp.DesaturateMode.LUMA,
                "average":    Gimp.DesaturateMode.AVERAGE,
                "lightness":  Gimp.DesaturateMode.LIGHTNESS,
            }
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            mode_str    = params.get("mode", "luminosity")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            mode = MODE_MAP.get(mode_str.lower(), Gimp.DesaturateMode.LUMINANCE)
            image.undo_group_start()
            try:
                proc = self._lookup_pdb("gimp-drawable-desaturate")
                cfg = proc.create_config()
                cfg.set_property("drawable", drawable)
                cfg.set_property("desaturate-mode", mode)
                self._run_pdb(proc, cfg)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _invert_colors(self, params):
        """Invert all colors in a layer."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                proc = self._lookup_pdb("gimp-drawable-invert")
                cfg = proc.create_config()
                cfg.set_property("drawable", drawable)
                cfg.set_property("linear", False)
                self._run_pdb(proc, cfg)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 3 — Resize & Transform
    # =========================================================================

    def _scale_image(self, params):
        """Scale image to exact dimensions."""
        try:
            image_index   = int(params.get("image_index", 0))
            width         = int(params.get("width"))
            height        = int(params.get("height"))
            interpolation = params.get("interpolation", "cubic")
            image  = self._get_image(image_index)
            self._interp_from_string(interpolation)
            image.undo_group_start()
            try:
                self._check_ok(image.scale(width, height), "Scaling the image")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "width": width, "height": height}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _scale_to_fit(self, params):
        """Scale image to fit within a bounding box preserving aspect ratio."""
        try:
            image_index   = int(params.get("image_index", 0))
            max_width     = int(params.get("max_width"))
            max_height    = int(params.get("max_height"))
            interpolation = params.get("interpolation", "cubic")
            image  = self._get_image(image_index)
            self._interp_from_string(interpolation)
            src_w  = image.get_width()
            src_h  = image.get_height()
            aspect = src_w / src_h
            max_aspect = max_width / max_height
            if aspect > max_aspect:
                new_w = max_width
                new_h = max(1, int(max_width / aspect))
            else:
                new_h = max_height
                new_w = max(1, int(max_height * aspect))
            image.undo_group_start()
            try:
                self._check_ok(image.scale(new_w, new_h), "Scaling the image")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "width": new_w, "height": new_h}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _crop_to_selection(self, params):
        """Crop image to selection bounds."""
        try:
            image_index = int(params.get("image_index", 0))
            autocrop    = bool(params.get("autocrop", False))
            image = self._get_image(image_index)
            image.undo_group_start()
            try:
                if autocrop:
                    proc = self._lookup_pdb("gimp-image-autocrop")
                    cfg = proc.create_config()
                    cfg.set_property("image", image)
                    self._run_pdb(proc, cfg)
                else:
                    _ok, non_empty, x1, y1, x2, y2 = Gimp.Selection.bounds(image)
                    if non_empty:
                        self._check_ok(image.crop(x2 - x1, y2 - y1, x1, y1), "Cropping the image")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _crop_to_rect(self, params):
        """Crop image to explicit rectangle."""
        try:
            image_index = int(params.get("image_index", 0))
            x      = int(params.get("x", 0))
            y      = int(params.get("y", 0))
            width  = int(params.get("width"))
            height = int(params.get("height"))
            image = self._get_image(image_index)
            image.undo_group_start()
            try:
                self._check_ok(image.crop(width, height, x, y), "Cropping the image")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "x": x, "y": y, "width": width, "height": height}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _rotate_image(self, params):
        """Rotate image by angle."""
        try:
            image_index = int(params.get("image_index", 0))
            angle       = float(params.get("angle", 90))
            image = self._get_image(image_index)
            image.undo_group_start()
            try:
                rot_map = {
                    90.0:  Gimp.RotationType.DEGREES90,
                    180.0: Gimp.RotationType.DEGREES180,
                    270.0: Gimp.RotationType.DEGREES270,
                    -90.0: Gimp.RotationType.DEGREES270,
                }
                if angle in rot_map:
                    self._check_ok(image.rotate(rot_map[angle]), "Rotating the image")
                else:
                    import math
                    rad = math.radians(angle)
                    proc = self._lookup_pdb("gimp-item-transform-rotate-default")
                    for layer in image.get_layers():
                        cfg = proc.create_config()
                        cfg.set_property("item",      layer)
                        cfg.set_property("angle",     rad)
                        cfg.set_property("auto-center", True)
                        cfg.set_property("center-x",  0)
                        cfg.set_property("center-y",  0)
                        self._run_pdb(proc, cfg)
                    self._check_item(image.flatten(), "Flattening the rotated image")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "angle": angle}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _flip_image(self, params):
        """Flip image horizontally or vertically."""
        try:
            image_index = int(params.get("image_index", 0))
            direction   = params.get("direction", "horizontal").lower()
            image = self._get_image(image_index)
            orient = Gimp.OrientationType.HORIZONTAL if direction == "horizontal" else Gimp.OrientationType.VERTICAL
            image.undo_group_start()
            try:
                self._check_ok(image.flip(orient), "Flipping the image")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "direction": direction}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _resize_canvas(self, params):
        """Resize canvas without scaling content."""
        try:
            image_index = int(params.get("image_index", 0))
            new_w  = int(params.get("width"))
            new_h  = int(params.get("height"))
            anchor = params.get("anchor", "center").lower()
            fill   = params.get("fill", "transparent")
            # Parse the fill color before changing anything, so a bad color
            # can't leave the canvas resized but unfilled.
            bg = None if is_transparent(fill) else self._color(fill)
            image  = self._get_image(image_index)
            src_w  = image.get_width()
            src_h  = image.get_height()
            # Compute offset based on anchor
            dx = (new_w - src_w) // 2
            dy = (new_h - src_h) // 2
            anchor_offsets = {
                "center":       (dx,         dy),
                "top-left":     (0,          0),
                "top":          (dx,         0),
                "top-right":    (new_w - src_w, 0),
                "left":         (0,          dy),
                "right":        (new_w - src_w, dy),
                "bottom-left":  (0,          new_h - src_h),
                "bottom":       (dx,         new_h - src_h),
                "bottom-right": (new_w - src_w, new_h - src_h),
            }
            off_x, off_y = anchor_offsets.get(anchor, (dx, dy))
            image.undo_group_start()
            try:
                self._check_ok(image.resize(new_w, new_h, off_x, off_y), "Resizing the canvas")
                if bg is not None:
                    Gimp.context_push()
                    try:
                        Gimp.context_set_background(bg)
                        self._check_item(image.flatten(), "Flattening the resized canvas")
                    finally:
                        Gimp.context_pop()
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "width": new_w, "height": new_h, "offset_x": off_x, "offset_y": off_y}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 4 — Selections
    # =========================================================================

    def _select_rectangle(self, params):
        """Create a rectangular selection."""
        try:
            image_index = int(params.get("image_index", 0))
            x       = int(params.get("x", 0))
            y       = int(params.get("y", 0))
            width   = int(params.get("width"))
            height  = int(params.get("height"))
            operation = params.get("operation", "replace")
            feather   = float(params.get("feather", 0))
            image = self._get_image(image_index)
            op = self._channel_ops_from_string(operation)
            image.select_rectangle(op, x, y, width, height)
            if feather > 0:
                Gimp.Selection.feather(image, feather)
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _select_ellipse(self, params):
        """Create an elliptical selection."""
        try:
            image_index = int(params.get("image_index", 0))
            x       = int(params.get("x", 0))
            y       = int(params.get("y", 0))
            width   = int(params.get("width"))
            height  = int(params.get("height"))
            operation = params.get("operation", "replace")
            feather   = float(params.get("feather", 0))
            image = self._get_image(image_index)
            op = self._channel_ops_from_string(operation)
            image.select_ellipse(op, x, y, width, height)
            if feather > 0:
                Gimp.Selection.feather(image, feather)
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _select_by_color(self, params):
        """Select by color similarity."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            color_str   = params.get("color", "white")
            threshold   = int(params.get("threshold", 15))
            operation   = params.get("operation", "replace")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            op = self._channel_ops_from_string(operation)
            color = self._color(color_str)
            pdb = Gimp.get_pdb()
            proc = pdb.lookup_procedure("gimp-image-select-color")
            if proc is None:
                raise RuntimeError(
                    "PDB procedure 'gimp-image-select-color' not found"
                )
            Gimp.context_push()
            try:
                Gimp.context_set_antialias(True)
                Gimp.context_set_feather(False)
                Gimp.context_set_sample_threshold_int(threshold)
                Gimp.context_set_sample_merged(False)
                Gimp.context_set_sample_transparent(False)
                cfg = proc.create_config()
                cfg.set_property("image",     image)
                cfg.set_property("drawable",  drawable)
                cfg.set_property("color",     color)
                cfg.set_property("operation", op)
                self._run_pdb(proc, cfg)
            finally:
                Gimp.context_pop()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _select_all(self, params):
        """Select entire canvas."""
        try:
            image = self._get_image(int(params.get("image_index", 0)))
            Gimp.Selection.all(image)
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _select_none(self, params):
        """Remove all selections."""
        try:
            image = self._get_image(int(params.get("image_index", 0)))
            Gimp.Selection.none(image)
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _invert_selection(self, params):
        """Invert selection."""
        try:
            image = self._get_image(int(params.get("image_index", 0)))
            Gimp.Selection.invert(image)
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _modify_selection(self, params):
        """Grow/shrink/feather/border/sharpen selection."""
        try:
            image_index = int(params.get("image_index", 0))
            operation   = params.get("operation", "grow").lower()
            amount      = float(params.get("amount", 0))
            image = self._get_image(image_index)
            OP_MAP = {
                "grow":    Gimp.Selection.grow,
                "shrink":  Gimp.Selection.shrink,
                "feather": Gimp.Selection.feather,
                "border":  Gimp.Selection.border,
                "sharpen": Gimp.Selection.sharpen,
            }
            fn = OP_MAP.get(operation)
            if fn is None:
                return {"status": "error", "error": f"Unknown selection operation: {operation}"}
            if operation == "sharpen":
                fn(image)
            else:
                fn(image, amount)
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 5 — Layer Operations
    # =========================================================================

    def _blend_mode_from_string(self, mode_str):
        """Map blend mode name string to Gimp.LayerMode."""
        MODE_MAP = {
            "NORMAL":      Gimp.LayerMode.NORMAL,
            "MULTIPLY":    Gimp.LayerMode.MULTIPLY,
            "SCREEN":      Gimp.LayerMode.SCREEN,
            "OVERLAY":     Gimp.LayerMode.OVERLAY,
            "DARKEN":      Gimp.LayerMode.DARKEN_ONLY,
            "LIGHTEN":     Gimp.LayerMode.LIGHTEN_ONLY,
            "DODGE":       Gimp.LayerMode.DODGE,
            "BURN":        Gimp.LayerMode.BURN,
            "HARD_LIGHT":  Gimp.LayerMode.HARDLIGHT,
            "SOFT_LIGHT":  Gimp.LayerMode.SOFTLIGHT,
            "DIFFERENCE":  Gimp.LayerMode.DIFFERENCE,
            "HUE":         Gimp.LayerMode.HSV_HUE,
            "SATURATION":  Gimp.LayerMode.HSV_SATURATION,
            "COLOR":       Gimp.LayerMode.HSL_COLOR,
            "LUMINOSITY":  Gimp.LayerMode.HSV_VALUE,
            "DISSOLVE":    Gimp.LayerMode.DISSOLVE,
        }
        name = "_".join(mode_str.upper().replace("-", " ").split())
        if name not in MODE_MAP:
            raise ValueError(
                f"Unknown blend_mode {mode_str!r}. Supported modes: {', '.join(MODE_MAP)}"
            )
        return MODE_MAP[name]

    def _create_layer(self, params):
        """Create and insert a new layer."""
        try:
            image_index = int(params.get("image_index", 0))
            name        = params.get("name", "New Layer")
            opacity     = float(params.get("opacity", 100))
            blend_mode  = params.get("blend_mode", "NORMAL")
            position    = int(params.get("position", -1))
            fill        = params.get("fill", "transparent")
            # GIMP treats any value below -1 as "above the selected layer, but never
            # inside a group", which no client would guess, so allow only -1.
            if position < -1:
                return {"status": "error",
                        "error": f"position must be -1 (above the selected layer) "
                                 f"or >= 0 (0 = top), got {position}"}
            image = self._get_image(image_index)
            width  = params.get("width")
            height = params.get("height")
            width  = int(image.get_width() if width is None else width)
            height = int(image.get_height() if height is None else height)
            for dimension, value in (("width", width), ("height", height)):
                if not 1 <= value <= Gimp.MAX_IMAGE_SIZE:
                    return {"status": "error",
                            "error": f"{dimension} must be between 1 and "
                                     f"{Gimp.MAX_IMAGE_SIZE}, got {value}"}
            mode   = self._blend_mode_from_string(blend_mode)
            # The layer type must match the image's base type, or GIMP refuses the insert.
            layer_type = {
                Gimp.ImageBaseType.RGB:     Gimp.ImageType.RGBA_IMAGE,
                Gimp.ImageBaseType.GRAY:    Gimp.ImageType.GRAYA_IMAGE,
                Gimp.ImageBaseType.INDEXED: Gimp.ImageType.INDEXEDA_IMAGE,
            }[image.get_base_type()]
            # The layer is filled before it is inserted, so insert_layer() is the only
            # change to the image: a failure leaves it untouched (no undo step, not
            # marked dirty). Drawable.fill works on a layer not yet in an image, fills
            # all of it whatever the selection, and ignores the context's opacity and
            # paint mode.
            layer = Gimp.Layer.new(image, name, width, height, layer_type, opacity, mode)
            try:
                if is_transparent(fill):
                    self._check_ok(layer.fill(Gimp.FillType.TRANSPARENT), "Filling the new layer")
                else:
                    bg = self._color(fill)
                    Gimp.context_push()
                    try:
                        Gimp.context_set_background(bg)
                        self._check_ok(layer.fill(Gimp.FillType.BACKGROUND),
                                       "Filling the new layer")
                    finally:
                        Gimp.context_pop()
                self._check_ok(image.insert_layer(layer, None, position), "Inserting the layer")
            except Exception:
                # A layer that never made it into the image belongs to none; free it.
                try:
                    layer.delete()
                except Exception:
                    pass
                raise
            Gimp.displays_flush()
            parent = layer.get_parent()
            return {
                "status": "success",
                "results": {
                    "layer_name": layer.get_name(),
                    "layer_id":   layer.get_id(),
                    "width":      width,
                    "height":     height,
                    # Where the layer landed, which can differ from the request: -1
                    # depends on the selection, and a floating selection stays on top.
                    "position":   image.get_item_position(layer),
                    # -1 can put the layer inside a group; then position is its index
                    # in that group. GIMP keeps item names unique within an image.
                    "parent":     parent.get_name() if parent else None,
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _duplicate_layer(self, params):
        """Duplicate a layer."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            image    = self._get_image(image_index)
            layer    = self._resolve_layer(image, layer_name, None)
            layers   = image.get_layers()
            position = layers.index(layer) if layer in layers else 0
            image.undo_group_start()
            try:
                new_layer = layer.copy()
                image.insert_layer(new_layer, None, position)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"layer_name": new_layer.get_name(), "layer_id": new_layer.get_id()}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _delete_layer(self, params):
        """Delete a layer."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            layer_index = params.get("layer_index", None)
            if layer_index is not None:
                layer_index = int(layer_index)
            image = self._get_image(image_index)
            layer = self._resolve_layer(image, layer_name, layer_index)
            image.undo_group_start()
            try:
                image.remove_layer(layer)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _rename_layer(self, params):
        """Rename a layer."""
        try:
            image_index = int(params.get("image_index", 0))
            old_name    = params.get("old_name", None)
            layer_index = params.get("layer_index", None)
            new_name    = params.get("new_name", "")
            if layer_index is not None:
                layer_index = int(layer_index)
            image = self._get_image(image_index)
            layer = self._resolve_layer(image, old_name, layer_index)
            prev_name = layer.get_name()
            layer.set_name(new_name)
            Gimp.displays_flush()
            return {"status": "success", "results": {"old_name": prev_name, "new_name": new_name}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _set_layer_properties(self, params):
        """Set layer opacity, blend mode, and/or visibility."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            layer_index = params.get("layer_index", None)
            opacity     = params.get("opacity", None)
            blend_mode  = params.get("blend_mode", None)
            visible     = params.get("visible", None)
            mode = self._blend_mode_from_string(blend_mode) if blend_mode not in (None, "") else None
            if layer_index is not None:
                layer_index = int(layer_index)
            image = self._get_image(image_index)
            layer = self._resolve_layer(image, layer_name, layer_index)
            image.undo_group_start()
            try:
                if opacity is not None:
                    layer.set_opacity(float(opacity))
                if mode is not None:
                    layer.set_mode(mode)
                if visible is not None:
                    layer.set_visible(bool(visible))
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _reorder_layer(self, params):
        """Move a layer to a new stack position."""
        try:
            image_index  = int(params.get("image_index", 0))
            layer_name   = params.get("layer_name", None)
            layer_index  = params.get("layer_index", None)
            new_position = int(params.get("new_position", 0))
            if layer_index is not None:
                layer_index = int(layer_index)
            image = self._get_image(image_index)
            layer = self._resolve_layer(image, layer_name, layer_index)
            # One call, so one undo step without an undo group; an empty group
            # would mark the image dirty when GIMP refuses the move.
            self._check_ok(image.reorder_item(layer, None, new_position), "Moving the layer")
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _flatten_image(self, params):
        """Flatten all layers."""
        try:
            image = self._get_image(int(params.get("image_index", 0)))
            image.undo_group_start()
            try:
                self._check_item(image.flatten(), "Flattening the image")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _merge_visible_layers(self, params):
        """Merge visible layers."""
        try:
            image = self._get_image(int(params.get("image_index", 0)))
            image.undo_group_start()
            try:
                merged = self._check_item(image.merge_visible_layers(Gimp.MergeType.CLIP_TO_IMAGE),
                                          "Merging the visible layers")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"layer_name": merged.get_name(), "layer_id": merged.get_id()}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _list_layers(self, params):
        """List all layers with properties."""
        try:
            image  = self._get_image(int(params.get("image_index", 0)))
            layers = image.get_layers()
            layer_list = []
            for i, layer in enumerate(layers):
                try:
                    layer_list.append({
                        "index":      i,
                        "name":       layer.get_name(),
                        "id":         layer.get_id(),
                        "visible":    layer.get_visible(),
                        "opacity":    layer.get_opacity(),
                        "blend_mode": str(layer.get_mode()),
                        "width":      layer.get_width(),
                        "height":     layer.get_height(),
                        "has_alpha":  layer.has_alpha(),
                        "offsets":    list(layer.get_offsets()),
                    })
                except Exception as ex:
                    layer_list.append({"index": i, "error": str(ex)})
            return {"status": "success", "results": {"layers": layer_list, "count": len(layer_list)}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 6 — Color & Paint
    # =========================================================================

    def _fill_layer(self, params):
        """Fill entire layer with color."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            color_str   = params.get("color", "white")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            # Parse the color first, so a bad color fails before the selection
            # is touched (the cleanup below clears it).
            fg = self._color(color_str)
            image.undo_group_start()
            self._push_paint_context()
            try:
                Gimp.Selection.all(image)
                Gimp.context_set_foreground(fg)
                self._check_ok(Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND),
                               "Filling the layer")
            finally:
                # Clear the temporary selection even if the fill failed, so it
                # doesn't restrict later edits.
                Gimp.Selection.none(image)
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _fill_selection(self, params):
        """Fill current selection with color or transparency."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            fill_type   = (params.get("fill_type") or "foreground").lower()
            color_str   = params.get("color")
            image    = self._get_image(image_index)
            # With no active selection GIMP applies edit_clear()/edit_fill() to
            # the ENTIRE layer. That silently destroys image content, so refuse
            # it: whole-layer fills are what the fill_layer tool is for.
            _ok, non_empty, _x1, _y1, _x2, _y2 = Gimp.Selection.bounds(image)
            if not non_empty:
                return {
                    "status": "error",
                    "error": ("No active selection on this image: with no selection GIMP applies "
                              "the fill to the entire layer. Create a selection first, or use the "
                              "fill_layer tool for a whole-layer fill."),
                }
            drawable = self._resolve_layer(image, layer_name, None)
            # Parse the color before the undo group opens: starting a group
            # clears GIMP's redo history, even if nothing is painted.
            # With no color ("" or None: the MCP server sends None when it's
            # omitted), fill with the current foreground color.
            fg = None
            if fill_type not in ("transparent", "background", "pattern") and color_str not in (None, ""):
                fg = self._color(color_str)
            image.undo_group_start()
            self._push_paint_context()
            try:
                if fill_type == "transparent":
                    # Ensure layer has alpha channel before deleting pixels
                    if not drawable.has_alpha():
                        drawable.add_alpha()
                    self._check_ok(Gimp.Drawable.edit_clear(drawable), "Clearing the selection")
                elif fill_type == "background":
                    self._check_ok(Gimp.Drawable.edit_fill(drawable, Gimp.FillType.BACKGROUND),
                                   "Filling the selection")
                elif fill_type == "pattern":
                    self._check_ok(Gimp.Drawable.edit_fill(drawable, Gimp.FillType.PATTERN),
                                   "Filling the selection")
                else:
                    # foreground (default) or explicit color
                    if fg is not None:
                        Gimp.context_set_foreground(fg)
                    self._check_ok(Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND),
                                   "Filling the selection")
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _set_colors(self, params):
        """Set foreground and/or background color."""
        try:
            # "" counts as omitted: clients often send it for optional arguments.
            fg_str = params.get("foreground", None)
            bg_str = params.get("background", None)
            fg_str = None if fg_str == "" else fg_str
            bg_str = None if bg_str == "" else bg_str
            # Parse both before setting either, so a bad background can't
            # leave only the foreground changed.
            fg = self._color(fg_str) if fg_str is not None else None
            bg = self._color(bg_str) if bg_str is not None else None
            if fg is not None:
                Gimp.context_set_foreground(fg)
            if bg is not None:
                Gimp.context_set_background(bg)
            return {"status": "success", "results": {"foreground": fg_str, "background": bg_str}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _draw_line(self, params):
        """Draw a straight line."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            x1 = float(params.get("x1", 0))
            y1 = float(params.get("y1", 0))
            x2 = float(params.get("x2", 0))
            y2 = float(params.get("y2", 0))
            color_str  = params.get("color", None)
            line_width = float(params.get("width", 2.0))
            tool       = params.get("tool", "pencil").lower()
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            # Parse the color before the undo group opens: starting a group
            # clears GIMP's redo history, even if nothing is painted.
            # "" counts as omitted: clients often send it for optional arguments.
            fg = self._color(color_str) if color_str not in (None, "") else None
            image.undo_group_start()
            self._push_paint_context()
            try:
                if fg is not None:
                    Gimp.context_set_foreground(fg)
                Gimp.context_set_brush_size(line_width)
                coords = [x1, y1, x2, y2]
                if tool == "paintbrush":
                    self._check_ok(Gimp.paintbrush_default(drawable, coords), "Drawing the line")
                else:
                    self._check_ok(Gimp.pencil(drawable, coords), "Drawing the line")
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _draw_rectangle(self, params):
        """Draw a rectangle outline."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            x          = int(params.get("x", 0))
            y          = int(params.get("y", 0))
            width      = int(params.get("width"))
            height     = int(params.get("height"))
            color_str  = params.get("color", None)
            line_width = float(params.get("line_width", 2.0))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            # Parse the color first, so a bad color fails before the selection
            # is touched (the cleanup below clears it).
            # "" counts as omitted: clients often send it for optional arguments.
            fg = self._color(color_str) if color_str not in (None, "") else None
            image.undo_group_start()
            self._push_paint_context()
            try:
                if fg is not None:
                    Gimp.context_set_foreground(fg)
                Gimp.context_set_stroke_method(Gimp.StrokeMethod.LINE)
                Gimp.context_set_line_width(line_width)
                image.select_rectangle(Gimp.ChannelOps.REPLACE, x, y, width, height)
                proc = self._lookup_pdb("gimp-drawable-edit-stroke-selection")
                cfg = proc.create_config()
                cfg.set_property("drawable", drawable)
                self._run_pdb(proc, cfg)
            finally:
                # Clear the temporary selection even if stroking failed, so it
                # doesn't restrict later edits.
                Gimp.Selection.none(image)
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _draw_ellipse(self, params):
        """Draw an ellipse outline."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            x          = int(params.get("x", 0))
            y          = int(params.get("y", 0))
            width      = int(params.get("width"))
            height     = int(params.get("height"))
            color_str  = params.get("color", None)
            line_width = float(params.get("line_width", 2.0))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            # Parse the color first, so a bad color fails before the selection
            # is touched (the cleanup below clears it).
            # "" counts as omitted: clients often send it for optional arguments.
            fg = self._color(color_str) if color_str not in (None, "") else None
            image.undo_group_start()
            self._push_paint_context()
            try:
                if fg is not None:
                    Gimp.context_set_foreground(fg)
                Gimp.context_set_stroke_method(Gimp.StrokeMethod.LINE)
                Gimp.context_set_line_width(line_width)
                image.select_ellipse(Gimp.ChannelOps.REPLACE, x, y, width, height)
                proc = self._lookup_pdb("gimp-drawable-edit-stroke-selection")
                cfg = proc.create_config()
                cfg.set_property("drawable", drawable)
                self._run_pdb(proc, cfg)
            finally:
                # Clear the temporary selection even if stroking failed, so it
                # doesn't restrict later edits.
                Gimp.Selection.none(image)
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _fill_rectangle(self, params):
        """Fill a rectangular region with color."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            x       = int(params.get("x", 0))
            y       = int(params.get("y", 0))
            width   = int(params.get("width"))
            height  = int(params.get("height"))
            color_str = params.get("color", "white")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            # Parse the color first, so a bad color fails before the selection
            # is touched (the cleanup below clears it).
            fg = self._color(color_str)
            image.undo_group_start()
            self._push_paint_context()
            try:
                image.select_rectangle(Gimp.ChannelOps.REPLACE, x, y, width, height)
                Gimp.context_set_foreground(fg)
                self._check_ok(Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND),
                               "Filling the region")
            finally:
                # Clear the temporary selection even if the fill failed, so it
                # doesn't restrict later edits.
                Gimp.Selection.none(image)
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _fill_ellipse(self, params):
        """Fill an elliptical region with color."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            x       = int(params.get("x", 0))
            y       = int(params.get("y", 0))
            width   = int(params.get("width"))
            height  = int(params.get("height"))
            color_str = params.get("color", "white")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            # Parse the color first, so a bad color fails before the selection
            # is touched (the cleanup below clears it).
            fg = self._color(color_str)
            image.undo_group_start()
            self._push_paint_context()
            try:
                image.select_ellipse(Gimp.ChannelOps.REPLACE, x, y, width, height)
                Gimp.context_set_foreground(fg)
                self._check_ok(Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND),
                               "Filling the region")
            finally:
                # Clear the temporary selection even if the fill failed, so it
                # doesn't restrict later edits.
                Gimp.Selection.none(image)
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _gradient_fill(self, params):
        """Fill the selection (or whole layer) with an FG->BG gradient.

        Uses Gimp.Drawable.edit_gradient_fill (GIMP 3.2). This honours the active
        selection and writes correct sRGB pixels. The previous GEGL-graph approach
        rendered to a shadow buffer with normalized (0..1) coordinates and never
        produced visible output.
        """
        try:
            image_index   = int(params.get("image_index", 0))
            layer_name    = params.get("layer_name", None)
            color1        = params.get("color1", "black")
            color2        = params.get("color2", "white")
            gradient_type = params.get("gradient_type", "linear").lower()
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            w = image.get_width()
            h = image.get_height()

            def _coord(*keys, default):
                for k in keys:
                    v = params.get(k, None)
                    if v is not None:
                        return float(v)
                return float(default)

            x1 = _coord("x1", "start_x", default=0)
            y1 = _coord("y1", "start_y", default=0)
            x2 = _coord("x2", "end_x", default=w)
            y2 = _coord("y2", "end_y", default=h)

            gtype = (Gimp.GradientType.RADIAL if gradient_type == "radial"
                     else Gimp.GradientType.LINEAR)

            # Parse the colors before the undo group opens: starting a group
            # clears GIMP's redo history, even if nothing is painted.
            fg = self._color(color1)
            bg = self._color(color2)
            image.undo_group_start()
            self._push_paint_context()
            try:
                Gimp.context_set_foreground(fg)
                Gimp.context_set_background(bg)
                Gimp.context_set_gradient_fg_bg_rgb()
                # The gradient fill also uses these context settings, which an
                # earlier call may have changed.
                Gimp.context_set_gradient_reverse(False)
                Gimp.context_set_gradient_repeat_mode(Gimp.RepeatMode.NONE)
                # (gradient_type, offset, supersample, supersample_max_depth,
                #  supersample_threshold, dither, x1, y1, x2, y2)
                # Returns False when GIMP rejects the fill (e.g. on a layer group).
                self._check_ok(drawable.edit_gradient_fill(
                    gtype, 0.0, False, 3, 0.2, True, x1, y1, x2, y2
                ), "Filling the gradient")
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 7 — Text
    # =========================================================================

    def _default_font(self):
        """Return the first available default or installed Gimp.Font, or None."""
        for fallback in ("Sans-serif", "Serif", "Monospace"):
            font_obj = Gimp.Font.get_by_name(fallback)
            if font_obj is not None:
                return font_obj

        raw = Gimp.fonts_get_list("")
        if isinstance(raw, tuple):
            flist = list(raw[1]) if len(raw) > 1 else []
        else:
            flist = list(raw)
        return flist[0] if flist else None

    def _resolve_font(self, name):
        """Resolve a font name to (Gimp.Font, substituted), or (None, False).

        Surrounding whitespace is stripped, and an empty name selects the
        default font without reporting a substitution. Exact and GIMP 2.x alias
        matches return substituted=False; falling back to a default or the first
        installed font for an unknown name returns substituted=True.
        """
        if not isinstance(name, str):
            raise TypeError(f"font must be a string, got {type(name).__name__}")
        if not (hasattr(Gimp, "Font") and hasattr(Gimp.Font, "get_by_name")):
            return None, False

        name = name.strip()
        if not name:
            return self._default_font(), False

        font_obj = Gimp.Font.get_by_name(name)
        if font_obj is not None:
            return font_obj, False

        # GIMP 3.2 dropped GIMP 2.x aliases; e.g. "Sans" no longer resolves,
        # the 3.2 equivalent is "Sans-serif".
        for alias in (name + "-serif", name + " Regular",
                      name.replace(" ", "-")):
            if alias == name:
                continue
            font_obj = Gimp.Font.get_by_name(alias)
            if font_obj is not None:
                return font_obj, False

        font_obj = self._default_font()
        return (font_obj, True) if font_obj is not None else (None, False)

    def _font_result_fields(self, requested, font_obj, substituted):
        """Build the font result fields for add_text and edit_text."""
        applied = font_obj.get_name()
        fields = {"font": applied}
        clean_requested = requested.strip()
        if clean_requested and clean_requested != applied:
            fields["requested_font"] = clean_requested
        if substituted:
            fields["warning"] = (
                f"Font {clean_requested!r} not found; used {applied!r}. "
                "See list_fonts for installed fonts."
            )
        return fields

    def _create_text_layer_native(self, image, text, font_obj, size, x, y, color):
        """Create a text layer via Gimp.TextLayer.new; color is a Gegl.Color."""
        if not hasattr(Gimp, "TextLayer"):
            return None

        try:
            tl = Gimp.TextLayer.new(image, text, font_obj, float(size), Gimp.Unit.pixel())
        except Exception:
            return None
        if tl is None:
            return None

        try:
            image.insert_layer(tl, None, 0)
        except Exception:
            return None

        # Layer is now in the image; swallow offset/color failures so the
        # caller does not fall through to the PDB fallback and insert a
        # second layer.
        try:
            tl.set_offsets(x, y)
        except Exception:
            pass
        try:
            pdb   = Gimp.get_pdb()
            cproc = pdb.lookup_procedure("gimp-text-layer-set-color")
            if cproc:
                ccfg = cproc.create_config()
                ccfg.set_property("layer", tl)
                ccfg.set_property("color", color)
                cproc.run(ccfg)
        except Exception:
            pass
        return tl

    def _create_text_layer_pdb(self, image, text, font_obj, size, x, y):
        """Create a text layer via the gimp-text-font PDB procedure."""
        proc = Gimp.get_pdb().lookup_procedure("gimp-text-font")
        if proc is None:
            return
        cfg = proc.create_config()
        cfg.set_property("image",     image)
        cfg.set_property("drawable",  None)
        cfg.set_property("x",         float(x))
        cfg.set_property("y",         float(y))
        cfg.set_property("text",      text)
        cfg.set_property("border",    0)
        cfg.set_property("antialias", True)
        cfg.set_property("size",      float(size))
        cfg.set_property("font",      font_obj)
        self._run_pdb(proc, cfg)

    def _find_new_layer(self, image, before_ids):
        """Return the first layer whose id is not in before_ids."""
        for lyr in image.get_layers():
            if lyr.get_id() not in before_ids:
                return lyr
        return None

    def _add_text(self, params):
        """Add a text layer."""
        try:
            image_index = int(params.get("image_index", 0))
            text_str    = params.get("text", "")
            x           = int(params.get("x", 0))
            y           = int(params.get("y", 0))
            font        = params.get("font")
            if font is None:
                font = "Sans-serif"
            size        = int(params.get("size", 24))
            color_str   = params.get("color", "black")

            image      = self._get_image(image_index)
            font_obj, substituted = self._resolve_font(font)
            if font_obj is None:
                return {
                    "status": "error",
                    "error":  f"add_text: no font found for {font!r}",
                }

            before_ids = {lyr.get_id() for lyr in image.get_layers()}
            text_layer = None

            # Parse the color before the undo group opens: starting a group
            # clears GIMP's redo history, even if nothing is painted.
            fg = self._color(color_str)
            image.undo_group_start()
            Gimp.context_push()
            try:
                Gimp.context_set_foreground(fg)
                text_layer = self._create_text_layer_native(
                    image, text_str, font_obj, size, x, y, fg)
                if text_layer is None:
                    self._create_text_layer_pdb(
                        image, text_str, font_obj, size, x, y)
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()

            if text_layer is None:
                text_layer = self._find_new_layer(image, before_ids)
            if text_layer is None:
                # Issue #15: return an explicit error instead of a placeholder
                # success so clients never chain ops on a fake handle.
                return {
                    "status": "error",
                    "error":  "add_text: no text layer was created (no PDB procedure succeeded)",
                }

            results = {
                "layer_name":  text_layer.get_name(),
                "layer_id":    text_layer.get_id(),
                "text_width":  text_layer.get_width(),
                "text_height": text_layer.get_height(),
                "position":    [x, y],
            }
            results.update(self._font_result_fields(font, font_obj, substituted))
            return {"status": "success", "results": results}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _edit_text(self, params):
        """Edit an existing text layer."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", "")
            new_text    = params.get("text", None)
            new_font    = params.get("font", None)
            # A blank font leaves the font unchanged, as if it were omitted:
            # clients often send "" for optional arguments.
            if isinstance(new_font, str) and not new_font.strip():
                new_font = None
            new_size    = params.get("size", None)
            # A blank color, like a blank font, leaves it unchanged.
            new_color   = params.get("color", None)
            if new_color == "":
                new_color = None
            # Parse the color before any edit, so a bad color can't leave the
            # text, font or size changed.
            color_obj = self._color(new_color) if new_color is not None else None
            image    = self._get_image(image_index)
            layer    = self._resolve_layer(image, layer_name, None)
            # gimp-text-layer-set-font takes a Gimp.Font, not a name. Resolve it
            # before any edit. _resolve_font falls back to an alias or a default
            # font, so this only fails if GIMP has no fonts at all.
            font_obj = None
            substituted = False
            if new_font is not None:
                font_obj, substituted = self._resolve_font(new_font)
                if font_obj is None:
                    return {"status": "error",
                            "error": f"edit_text: no font found for {new_font!r}"}
            image.undo_group_start()
            try:
                if new_text is not None:
                    proc = self._lookup_pdb("gimp-text-layer-set-text")
                    cfg = proc.create_config()
                    cfg.set_property("layer", layer)
                    cfg.set_property("text",  new_text)
                    self._run_pdb(proc, cfg)
                if new_font is not None:
                    proc = self._lookup_pdb("gimp-text-layer-set-font")
                    cfg = proc.create_config()
                    cfg.set_property("layer", layer)
                    cfg.set_property("font",  font_obj)
                    self._run_pdb(proc, cfg)
                if new_size is not None:
                    proc = self._lookup_pdb("gimp-text-layer-set-font-size")
                    cfg = proc.create_config()
                    cfg.set_property("layer",     layer)
                    cfg.set_property("font-size", float(new_size))
                    cfg.set_property("unit",      Gimp.Unit.pixel())
                    self._run_pdb(proc, cfg)
                if color_obj is not None:
                    proc = self._lookup_pdb("gimp-text-layer-set-color")
                    cfg = proc.create_config()
                    cfg.set_property("layer", layer)
                    cfg.set_property("color", color_obj)
                    self._run_pdb(proc, cfg)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            results = {"status": "success"}
            if font_obj is not None:
                results.update(self._font_result_fields(new_font, font_obj, substituted))
            return {"status": "success", "results": results}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _warp_region(self, params):
        """Warp / liquify a region of pixels to deform facial features.

        Uses plug-in-iwarp (interactive warp) to push pixels in a direction.
        Useful for subtle expressions: turning a neutral mouth into a smile by
        pushing corners upward, etc.

        params:
          image_index  — which image
          layer_name   — optional layer
          vectors      — list of warp vectors: [{x, y, dx, dy, radius, amount}]
                         x/y: pixel coords of warp center
                         dx/dy: push direction in pixels (positive y = down)
                         radius: influence radius in pixels (default 40)
                         amount: deform strength 0-1 (default 0.3)
        """
        try:
            from gi.repository import Gegl
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            vectors     = params.get("vectors", [])
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            pdb      = Gimp.get_pdb()

            image.undo_group_start()
            try:
                for v in vectors:
                    x      = float(v.get("x", 0))
                    y      = float(v.get("y", 0))
                    dx     = float(v.get("dx", 0))
                    dy     = float(v.get("dy", 0))
                    radius = float(v.get("radius", 40))
                    amount = float(v.get("amount", 0.3))

                    # Try GEGL warp operation first (GIMP 3 native approach)
                    try:
                        Gegl.init(None)
                        buf        = drawable.get_buffer()
                        shadow_buf = drawable.get_shadow_buffer()
                        graph      = Gegl.Node()

                        src = graph.create_child("gegl:buffer-source")
                        src.set_property("buffer", buf)

                        warp = graph.create_child("gegl:warp")
                        warp.set_property("behavior",    0)        # 0 = move
                        warp.set_property("strength",    amount)
                        warp.set_property("size",        radius)
                        warp.set_property("hardness",    0.5)
                        # stamp one warp stroke at (x,y) → (x+dx, y+dy)
                        # GEGL warp builds strokes via the "stroke" property
                        stroke = [(x, y), (x + dx, y + dy)]
                        warp.set_property("stroke", stroke)

                        out = graph.create_child("gegl:write-buffer")
                        out.set_property("buffer", shadow_buf)

                        src.link(warp)
                        warp.link(out)
                        out.process()

                        shadow_buf.flush()
                        drawable.merge_shadow(True)
                        drawable.update(
                            max(0, int(x - radius - abs(dx))),
                            max(0, int(y - radius - abs(dy))),
                            int(radius * 2 + abs(dx) * 2 + 4),
                            int(radius * 2 + abs(dy) * 2 + 4),
                        )
                    except Exception as gegl_err:
                        # Fallback: plug-in-iwarp if GEGL warp fails. If that is
                        # missing or fails too, report it instead of a success.
                        proc = pdb.lookup_procedure("plug-in-iwarp")
                        if proc is None:
                            raise RuntimeError(
                                f"GEGL warp failed ({gegl_err}) and plug-in-iwarp is not available"
                            ) from gegl_err
                        cfg = proc.create_config()
                        cfg.set_property("run-mode",      Gimp.RunMode.NONINTERACTIVE)
                        cfg.set_property("image",         image)
                        cfg.set_property("drawable",      drawable)
                        cfg.set_property("cursor-x",      int(x))
                        cfg.set_property("cursor-y",      int(y))
                        cfg.set_property("pressure",      amount)
                        cfg.set_property("move-max-dist", int(radius))
                        cfg.set_property("deform-type",   0)  # 0 = MOVE
                        cfg.set_property("x",             int(x + dx))
                        cfg.set_property("y",             int(y + dy))
                        self._run_pdb(proc, cfg)
            finally:
                image.undo_group_end()

            Gimp.displays_flush()
            return {"status": "success", "results": {"warped_vectors": len(vectors)}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _list_fonts(self, params):
        """List available fonts."""
        try:
            filt  = params.get("filter", None) or ""
            limit = int(params.get("limit", 100))
            raw = Gimp.fonts_get_list(filt)
            # GIMP 3.2 returns (n, [Gimp.Font, ...]) or just [Gimp.Font, ...]
            if isinstance(raw, tuple):
                font_objs = list(raw[1]) if len(raw) > 1 else []
            else:
                font_objs = list(raw)
            names = []
            for f in font_objs[:limit]:
                if hasattr(f, "get_name"):
                    names.append(f.get_name())
                elif hasattr(f, "name"):
                    names.append(f.name)
                else:
                    names.append(str(f))
            return {"status": "success", "results": {"fonts": names, "count": len(names)}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 8 — Filters & Effects
    # =========================================================================

    def _apply_drop_shadow(self, params):
        """Apply drop shadow via manual layer compositing (GIMP 3.2 compatible).

        gegl:drop-shadow and plug-in-drop-shadow are not reliably available in
        GIMP 3.2, so we build the shadow manually:
          1. Duplicate source layer → shadow_layer
          2. Fill it with shadow color (alpha-locked so shape is preserved)
          3. Set opacity and offset
          4. Gaussian-blur (plug-in-gauss, GEGL gaussian-blur fallback)
        """
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            offset_x    = int(params.get("offset_x", 5))
            offset_y    = int(params.get("offset_y", 5))
            blur_radius = float(params.get("blur_radius", 10))
            color_str   = params.get("color", "black")
            opacity     = float(params.get("opacity", 60))
            # Parse the color before adding the shadow layer, so a bad color
            # can't leave an unfilled shadow layer in the image.
            shadow_color = self._color(color_str)
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:

                # 1. Duplicate source layer to use as shadow base
                shadow_layer = drawable.copy()
                src_pos = image.get_item_position(drawable)
                image.insert_layer(shadow_layer, None, src_pos + 1)

                # 2. Fill shadow layer with shadow color, preserving alpha shape
                Gimp.context_set_foreground(shadow_color)
                shadow_layer.set_lock_alpha(True)
                try:
                    self._check_ok(Gimp.Drawable.edit_fill(shadow_layer, Gimp.FillType.FOREGROUND),
                                   "Filling the shadow layer")
                finally:
                    shadow_layer.set_lock_alpha(False)

                # 3. Set opacity and offset
                shadow_layer.set_opacity(opacity)
                offs = shadow_layer.get_offsets()
                shadow_layer.set_offsets(offs.offset_x + offset_x, offs.offset_y + offset_y)

                # 4. Blur the shadow layer. plug-in-gauss is missing from some
                # GIMP 3.2 builds, so fall back to a GEGL gaussian blur.
                if blur_radius > 0:
                    size = max(3, int(blur_radius * 2) | 1)  # must be odd, ≥ 3
                    blur_proc = Gimp.get_pdb().lookup_procedure("plug-in-gauss")
                    if blur_proc:
                        cfg = blur_proc.create_config()
                        cfg.set_property("image",      image)
                        cfg.set_property("drawable",   shadow_layer)
                        cfg.set_property("horizontal", size)
                        cfg.set_property("vertical",   size)
                        cfg.set_property("method",     0)
                        self._run_pdb(blur_proc, cfg)
                    else:
                        # The GEGL filter takes the radius directly, the way
                        # blur and apply_gaussian_blur pass it.
                        self._apply_gegl_filter(image, shadow_layer,
                                                "gegl:gaussian-blur", {
                                "std-dev-x": blur_radius,
                                "std-dev-y": blur_radius,
                            })

                shadow_layer.set_name("Drop Shadow")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _apply_gaussian_blur(self, params):
        """Apply Gaussian blur."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            radius      = float(params.get("radius", 5.0))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._apply_gegl_filter(image, drawable, "gegl:gaussian-blur", {
                    "std-dev-x": radius,
                    "std-dev-y": radius,
                })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _apply_pixelate(self, params):
        """Apply pixelate effect."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            block_size  = int(params.get("block_size", 10))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._apply_gegl_filter(image, drawable, "gegl:pixelize", {
                    "size-x": block_size,
                    "size-y": block_size,
                })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _apply_emboss(self, params):
        """Apply emboss effect."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            azimuth     = float(params.get("azimuth", 315))
            elevation   = float(params.get("elevation", 45))
            depth       = float(params.get("depth", 2))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._apply_gegl_filter(image, drawable, "gegl:emboss", {
                    "azimuth":   azimuth,
                    "elevation": elevation,
                    "depth":     int(round(depth)),
                    "type":      "emboss",
                })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _apply_vignette(self, params):
        """Apply vignette effect."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            softness    = float(params.get("softness", 0.8))
            shape       = str(params.get("shape", "circle")).lower()
            if shape not in VIGNETTE_SHAPES:
                return {"status": "error", "error": f"shape must be one of {', '.join(VIGNETTE_SHAPES)}"}
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._apply_gegl_filter(image, drawable, "gegl:vignette", {
                    "softness": softness,
                    "shape":    shape,
                    "radius":   1.0,
                })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _apply_noise(self, params):
        """Add noise to a layer."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            amount      = float(params.get("amount", 0.2))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                self._apply_gegl_filter(image, drawable, "gegl:noise-hsv", {
                    "value-distance":      amount,
                    "saturation-distance": 0.0,
                    "hue-distance":        0.0,
                })
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 9 — Export Pipelines
    # =========================================================================

    def _export_icon_sizes(self, params):
        """Export icon size sets for Android or iOS."""
        try:
            output_dir   = params.get("output_dir", "")
            platform_str = params.get("platform", "android").lower()
            src_index    = int(params.get("source_image_index", 0))
            fmt          = params.get("format", "png")
            self._check_export_args(fmt, 95)

            ANDROID_SIZES = [
                (48,  "mdpi"), (72, "hdpi"), (96, "xhdpi"),
                (144, "xxhdpi"), (192, "xxxhdpi"), (512, "playstore"),
            ]
            IOS_SIZES = [
                (20, 1), (20, 2), (20, 3),
                (29, 1), (29, 2), (29, 3),
                (40, 2), (40, 3),
                (60, 2), (60, 3),
                (76, 1), (76, 2),
                (84, 2),   # 83.5x2 rounded
                (1024, 1),
            ]

            source_image = self._get_image(src_index)
            os.makedirs(output_dir, exist_ok=True)
            exported = []
            sizes = ANDROID_SIZES if platform_str == "android" else IOS_SIZES

            for entry in sizes:
                if platform_str == "android":
                    px, density = entry
                    out_name = f"icon_{density}_{px}.{fmt}"
                else:
                    px, scale = entry
                    actual_px = int(px * scale)
                    out_name = f"icon_{px}@{scale}x.{fmt}"
                    px = actual_px

                out_path = os.path.join(output_dir, out_name)
                dup = source_image.duplicate()
                try:
                    src_w = dup.get_width()
                    src_h = dup.get_height()
                    aspect = src_w / src_h
                    if aspect >= 1.0:
                        new_w = px
                        new_h = max(1, int(px / aspect))
                    else:
                        new_h = px
                        new_w = max(1, int(px * aspect))
                    self._check_ok(dup.scale(new_w, new_h), "Scaling the icon copy")
                    self._export_to_path(dup, out_path, fmt, 95, True)
                    exported.append({"size": px, "file_path": out_path})
                finally:
                    dup.delete()

            return {"status": "success", "results": {"exported": exported, "count": len(exported), "platform": platform_str}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _export_web_optimized(self, params):
        """Export as both JPEG and PNG, return comparison."""
        try:
            output_dir    = params.get("output_dir", "")
            jpeg_quality  = int(params.get("jpeg_quality", 85))
            image_index   = int(params.get("image_index", 0))
            max_width     = params.get("max_width", None)
            max_height    = params.get("max_height", None)
            self._check_export_args("jpeg", jpeg_quality)
            image = self._get_image(image_index)
            os.makedirs(output_dir, exist_ok=True)

            dup = image.duplicate()
            try:
                if max_width or max_height:
                    src_w = dup.get_width()
                    src_h = dup.get_height()
                    mw = int(max_width  or src_w)
                    mh = int(max_height or src_h)
                    aspect = src_w / src_h
                    if src_w / mw > src_h / mh:
                        new_w, new_h = mw, max(1, int(mw / aspect))
                    else:
                        new_h, new_w = mh, max(1, int(mh * aspect))
                    self._check_ok(dup.scale(new_w, new_h), "Scaling the export copy")

                gio_file = dup.get_file()
                raw_name = gio_file.get_basename().rsplit(".", 1)[0] if gio_file else "image"
                jpeg_path = os.path.join(output_dir, f"{raw_name}.jpg")
                png_path  = os.path.join(output_dir, f"{raw_name}.png")
                jpeg_size = self._export_to_path(dup, jpeg_path, "jpeg", jpeg_quality, True)
                png_size  = self._export_to_path(dup, png_path,  "png",  95,           True)
            finally:
                dup.delete()

            recommendation = "jpeg" if jpeg_size < png_size else "png"
            return {
                "status": "success",
                "results": {
                    "jpeg_path": jpeg_path, "jpeg_size": jpeg_size,
                    "png_path":  png_path,  "png_size":  png_size,
                    "recommendation": recommendation,
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _batch_resize(self, params):
        """Resize all open images."""
        try:
            width           = params.get("width", None)
            height          = params.get("height", None)
            scale_factor    = params.get("scale_factor", None)
            maintain_aspect = bool(params.get("maintain_aspect", True))
            images  = Gimp.get_images()
            results = []
            for img in images:
                src_w = img.get_width()
                src_h = img.get_height()
                if scale_factor is not None:
                    new_w = max(1, int(src_w * float(scale_factor)))
                    new_h = max(1, int(src_h * float(scale_factor)))
                else:
                    tw = int(width  or src_w)
                    th = int(height or src_h)
                    if maintain_aspect:
                        if width and not height:
                            new_w = tw
                            new_h = max(1, int(src_h * tw / src_w))
                        elif height and not width:
                            new_h = th
                            new_w = max(1, int(src_w * th / src_h))
                        else:
                            aspect = src_w / src_h
                            if tw / th > aspect:
                                new_h = th
                                new_w = max(1, int(th * aspect))
                            else:
                                new_w = tw
                                new_h = max(1, int(tw / aspect))
                    else:
                        new_w, new_h = tw, th
                self._check_ok(img.scale(new_w, new_h), f"Scaling image {img.get_id()}")
                results.append({"image_id": img.get_id(), "old_width": src_w, "old_height": src_h, "new_width": new_w, "new_height": new_h})
            Gimp.displays_flush()
            return {"status": "success", "results": {"results": results, "count": len(results)}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _render_frame(self, src, width, height, keep_position, dest_image, what):
        """Render a drawable as GIMP shows it into a new layer of dest_image.

        Used for sprite sheet frames. The source is copied into a scratch image
        of the frame's size, and the copy's visible result becomes the frame
        (Gimp.Layer.new_from_visible). GIMP's own rendering then applies the
        mask, live filters and group children, and the scratch canvas crops
        the frame to its cell, so effects can't spill into the padding.

        new_from_drawable() also copies settings that only control how a layer
        is composited, and some would change or hide the frame. Checked against
        every setter Gimp.Layer has in GIMP 3.2, the copy is made visible, 100%
        opacity, NORMAL mode, AUTO composite mode, blend and composite spaces,
        with no locks. Masks shown for editing are hidden on the copy and on
        every layer inside it. Whether the mask is applied is kept, as GIMP
        shows it. Settings that don't affect pixels (name, color tag, tattoo,
        expanded, edit mask) are left alone.

        With keep_position the copy keeps its offsets (its place on its own
        canvas); otherwise its top-left corner goes to the frame's origin.
        Returns the new layer, not yet inserted into dest_image.
        """
        scratch = Gimp.Image.new(width, height, Gimp.ImageBaseType.RGB)
        try:
            scratch.undo_disable()
            layer = self._check_item(Gimp.Layer.new_from_drawable(src, scratch),
                                     f"Copying {what}")
            self._check_ok(scratch.insert_layer(layer, None, 0), f"Adding {what}")
            # Unlock first: a locked visibility makes set_visible() a silent
            # no-op, and the other locks would block the move and alpha below.
            # A group can't move while a child is locked, so go through the
            # whole copied tree. Only the copy changes; the source keeps its
            # locks and mask settings.
            items = [layer]
            for item in items:
                items.extend(item.get_children() if isinstance(item, Gimp.GroupLayer) else [])
                for unlock in (item.set_lock_visibility, item.set_lock_position,
                               item.set_lock_content, item.set_lock_alpha):
                    self._check_ok(unlock(False), f"Unlocking {what}")
                # A mask shown for editing (Alt-click), on the frame or a layer
                # in it, would render as the mask itself.
                if item.get_mask():
                    self._check_ok(item.set_show_mask(False), f"Hiding the mask of {what}")
            if not keep_position:
                self._check_ok(layer.set_offsets(0, 0), f"Placing {what}")
            if not layer.has_alpha():
                # Without alpha the scratch image would render opaque.
                self._check_ok(layer.add_alpha(), f"Adding alpha to {what}")
            self._check_ok(layer.set_visible(True), f"Showing {what}")
            self._check_ok(layer.set_opacity(100.0), f"Setting the opacity of {what}")
            self._check_ok(layer.set_mode(Gimp.LayerMode.NORMAL), f"Setting the mode of {what}")
            # set_mode() keeps the composite mode when the mode was already
            # NORMAL; CLIP_TO_BACKDROP would hide the frame on the empty canvas.
            self._check_ok(layer.set_composite_mode(Gimp.LayerCompositeMode.AUTO),
                           f"Setting the composite mode of {what}")
            self._check_ok(layer.set_blend_space(Gimp.LayerColorSpace.AUTO),
                           f"Setting the blend space of {what}")
            self._check_ok(layer.set_composite_space(Gimp.LayerColorSpace.AUTO),
                           f"Setting the composite space of {what}")
            return self._check_item(Gimp.Layer.new_from_visible(scratch, dest_image, what),
                                    f"Rendering {what}")
        finally:
            scratch.delete()

    def _export_sprite_sheet(self, params):
        """Combine frames into a sprite sheet."""
        try:
            output_path = params.get("output_path", "")
            columns     = params.get("columns", None)
            padding     = int(params.get("padding", 0))
            source      = params.get("source", "layers").lower()
            image_index = int(params.get("image_index", 0))
            import math

            if source == "images":
                frames = Gimp.get_images()
            else:
                src_image = self._get_image(image_index)
                frames = src_image.get_layers()

            if not frames:
                return {"status": "error", "error": "No frames found"}

            # Use first frame dimensions as the cell size
            frame_w = frames[0].get_width()
            frame_h = frames[0].get_height()
            n = len(frames)
            cols = int(columns) if columns else max(1, math.ceil(math.sqrt(n)))
            rows = math.ceil(n / cols)

            sheet_w = cols * frame_w + (cols - 1) * padding
            sheet_h = rows * frame_h + (rows - 1) * padding

            # ImageBaseType has no RGBA; alpha comes from the RGBA_IMAGE layer below.
            sheet = Gimp.Image.new(sheet_w, sheet_h, Gimp.ImageBaseType.RGB)
            try:
                # A scratch image: don't keep undo history for its edits.
                sheet.undo_disable()
                bg_layer = Gimp.Layer.new(sheet, "Background", sheet_w, sheet_h, Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)
                self._check_ok(sheet.insert_layer(bg_layer, None, 0),
                               "Adding the sprite sheet background")
                # TRANSPARENT fill doesn't use the context background color.
                self._check_ok(Gimp.Drawable.edit_fill(bg_layer, Gimp.FillType.TRANSPARENT),
                               "Clearing the sprite sheet")

                # Render each frame into a layer of the sheet. Unlike copy/paste,
                # this leaves the user's selection and clipboard alone.
                for i, frame in enumerate(frames):
                    col = i % cols
                    row = i // cols
                    dest_x = col * (frame_w + padding)
                    dest_y = row * (frame_h + padding)
                    if source == "images":
                        src_layers = frame.get_layers()
                        if not src_layers:
                            continue
                        # Keep the layer where it sits in its image.
                        src_drawable, keep_position = src_layers[0], True
                    else:
                        src_drawable, keep_position = frame, False
                    frame_layer = self._render_frame(src_drawable, frame_w, frame_h,
                                                     keep_position, sheet, f"frame {i}")
                    self._check_ok(sheet.insert_layer(frame_layer, None, 0), f"Adding frame {i}")
                    self._check_ok(frame_layer.set_offsets(dest_x, dest_y),
                                   f"Placing frame {i}")

                # flatten=False keeps the transparent background between frames;
                # the PNG export merges the visible layers.
                self._export_to_path(sheet, output_path, "png", 95, False)
            finally:
                sheet.delete()
            return {
                "status": "success",
                "results": {
                    "file_path":    output_path,
                    "columns":      cols,
                    "rows":         rows,
                    "frame_width":  frame_w,
                    "frame_height": frame_h,
                    "count":        n,
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _export_social_media_kit(self, params):
        """Export for multiple social media platforms."""
        try:
            output_dir  = params.get("output_dir", "")
            platforms   = params.get("platforms", None)
            image_index = int(params.get("image_index", 0))

            PLATFORM_SIZES = {
                "instagram_square":   (1080, 1080),
                "instagram_story":    (1080, 1920),
                "twitter_header":     (1500, 500),
                "facebook_cover":     (820,  312),
                "youtube_thumbnail":  (1280, 720),
            }

            target_platforms = platforms if platforms else list(PLATFORM_SIZES.keys())
            source_image = self._get_image(image_index)
            os.makedirs(output_dir, exist_ok=True)
            exported = []

            for platform_name in target_platforms:
                if platform_name not in PLATFORM_SIZES:
                    continue
                target_w, target_h = PLATFORM_SIZES[platform_name]
                dup = source_image.duplicate()
                try:
                    src_w = dup.get_width()
                    src_h = dup.get_height()
                    # Scale to cover target (crop to exact size)
                    scale = max(target_w / src_w, target_h / src_h)
                    scaled_w = max(1, int(src_w * scale))
                    scaled_h = max(1, int(src_h * scale))
                    self._check_ok(dup.scale(scaled_w, scaled_h), "Scaling the export copy")
                    # Crop to exact target
                    crop_x = (scaled_w - target_w) // 2
                    crop_y = (scaled_h - target_h) // 2
                    self._check_ok(dup.crop(target_w, target_h, crop_x, crop_y), "Cropping the export copy")
                    out_path = os.path.join(output_dir, f"{platform_name}.png")
                    self._export_to_path(dup, out_path, "png", 95, True)
                    exported.append({"platform": platform_name, "file_path": out_path, "width": target_w, "height": target_h})
                finally:
                    dup.delete()

            return {"status": "success", "results": {"exported": exported, "count": len(exported)}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 10 — Utility
    # =========================================================================

    def _list_images(self, params):
        """List all open images."""
        try:
            images = Gimp.get_images()
            image_list = []
            base_type_map = {
                Gimp.ImageBaseType.RGB:     "RGB",
                Gimp.ImageBaseType.GRAY:    "Grayscale",
                Gimp.ImageBaseType.INDEXED: "Indexed",
            }
            for i, img in enumerate(images):
                try:
                    gio_file = img.get_file()
                    file_path = gio_file.get_path() if gio_file else "Untitled"
                    image_list.append({
                        "index":      i,
                        "image_id":   img.get_id(),
                        "name":       gio_file.get_basename() if gio_file else f"Untitled_{i}",
                        "width":      img.get_width(),
                        "height":     img.get_height(),
                        "color_mode": base_type_map.get(img.get_base_type(), "Unknown"),
                        "num_layers": len(img.get_layers()),
                        "file_path":  file_path,
                        "is_dirty":   img.is_dirty() if hasattr(img, "is_dirty") else None,
                    })
                except Exception as ex:
                    image_list.append({"index": i, "error": str(ex)})
            return {"status": "success", "results": {"images": image_list, "count": len(image_list)}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _set_active_image(self, params):
        """Raise a specific image to the front."""
        try:
            image_index = int(params.get("image_index", 0))
            image = self._get_image(image_index)
            image_id = image.get_id()
            # GIMP 3.2 has no Gimp.get_displays(), so only a window the plugin
            # opened (see _create_display) can be raised.
            display = self._displays.get(image_id)
            if display is not None and not display.is_valid():
                # Its window was closed by hand.
                del self._displays[image_id]
                display = None
            if display is None:
                return {"status": "error", "error": (
                    f"Image {image_id} has no window opened by the plugin (it was opened "
                    "in GIMP directly or by call_api code, its window was closed, or GIMP "
                    "runs without a display), so it can't be raised from here. Use GIMP's "
                    "Windows menu to switch to it."
                )}
            display.present()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "image_id": image_id}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # GIMP 3.x libgimp exposes only undo *grouping* (undo_group_start/end,
    # undo_freeze/thaw) — there is no scriptable API to step the undo history,
    # so undo/redo cannot be implemented. Report that clearly instead of
    # crashing with AttributeError: 'Image' object has no attribute 'undo'.
    _UNDO_UNSUPPORTED = (
        "GIMP 3.x does not expose a scriptable undo/redo. libgimp offers undo "
        "grouping only (undo_group_start/end); there is no API to step the undo "
        "history. Use Edit > Undo/Redo (Ctrl+Z / Ctrl+Y) in GIMP, or re-open the "
        "source file to revert."
    )

    def _undo(self, params):
        """Not supported: GIMP 3.x has no scriptable undo."""
        return {"status": "error", "error": self._UNDO_UNSUPPORTED}

    def _redo(self, params):
        """Not supported: GIMP 3.x has no scriptable redo."""
        return {"status": "error", "error": self._UNDO_UNSUPPORTED}

    def _convert_color_mode(self, params):
        """Convert image color mode."""
        try:
            image_index = int(params.get("image_index", 0))
            mode        = params.get("mode", "RGB").upper()
            num_colors  = int(params.get("num_colors", 256))
            image = self._get_image(image_index)
            image.undo_group_start()
            try:
                if mode in ("RGB", "RGBA"):
                    image.convert_rgb()
                    if mode == "RGBA":
                        # Add alpha channel to all layers
                        for layer in image.get_layers():
                            if not layer.has_alpha():
                                layer.add_alpha()
                elif mode in ("GRAY", "GRAYA"):
                    image.convert_grayscale()
                    if mode == "GRAYA":
                        for layer in image.get_layers():
                            if not layer.has_alpha():
                                layer.add_alpha()
                elif mode == "INDEXED":
                    image.convert_indexed(
                        Gimp.ConvertDitherType.NO_DITHER,
                        Gimp.ConvertPaletteType.GENERATE,
                        num_colors, False, False, ""
                    )
                else:
                    return {"status": "error", "error": f"Unknown mode: {mode}"}
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "mode": mode}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _close_image(self, params):
        """Close an image, optionally saving first."""
        try:
            from gi.repository import Gio
            image_index = int(params.get("image_index", 0))
            save_first  = bool(params.get("save_first", False))
            image = self._get_image(image_index)
            if save_first:
                img_file = image.get_file()
                if img_file:
                    xcf_path = img_file.get_path().rsplit(".", 1)[0] + ".xcf"
                else:
                    import tempfile
                    xcf_path = os.path.join(tempfile.gettempdir(), f"gimp_backup_{image.get_id()}.xcf")
                gio_file = Gio.File.new_for_path(xcf_path)
                proc = self._lookup_pdb("gimp-xcf-save")
                cfg = proc.create_config()
                cfg.set_property("image", image)
                cfg.set_property("file", gio_file)
                self._run_pdb(proc, cfg)
            # image.delete() leaves an image that still has a display open, so
            # close the window the plugin opened first (see _create_display).
            # Deleting an image's last display deletes the image too, so the
            # image is only deleted here if it is still valid afterwards;
            # deleting it again would make GIMP log an "invalid ID" error.
            display = self._displays.pop(image.get_id(), None)
            if display is not None and display.is_valid():
                try:
                    display.delete()
                except Exception as e:
                    print(f"[MCP] close_image: deleting the display failed: {e}")
            if image.is_valid():
                image.delete()
            Gimp.displays_flush()
            # Never report success while the image is still open.
            if image.is_valid():
                return {"status": "error", "error": (
                    "Could not close image: it still has an open display not tracked "
                    "by the plugin (e.g. opened in GIMP directly, by call_api code, or "
                    "before this session started). Close the window manually with Ctrl+W."
                )}
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _get_selection_bounds(self, params):
        """Get selection bounding rectangle."""
        try:
            image = self._get_image(int(params.get("image_index", 0)))
            _ok, non_empty, x1, y1, x2, y2 = Gimp.Selection.bounds(image)
            return {
                "status": "success",
                "results": {
                    "has_selection": bool(non_empty),
                    "x":      x1,
                    "y":      y1,
                    "width":  x2 - x1,
                    "height": y2 - y1,
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _get_pixel_color(self, params):
        """Get color of a single pixel."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            x           = int(params.get("x", 0))
            y           = int(params.get("y", 0))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            pixel    = drawable.get_pixel(x, y)
            # GIMP 3.2: get_pixel returns a Gegl.Color object
            if hasattr(pixel, 'get_rgba'):
                r, g, b, a = srgb_bytes_from_color(pixel)
            else:
                # Fallback: old tuple format (num_channels, bytes_array)
                channels = list(pixel[1]) if hasattr(pixel[1], '__iter__') else []
                r = channels[0] if len(channels) > 0 else 0
                g = channels[1] if len(channels) > 1 else 0
                b = channels[2] if len(channels) > 2 else 0
                a = channels[3] if len(channels) > 3 else 255
            color_hex = f"#{r:02x}{g:02x}{b:02x}"
            return {
                "status": "success",
                "results": {
                    "color_hex": color_hex,
                    "color_rgb": [r, g, b],
                    "alpha":     a,
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _get_histogram(self, params):
        """Get histogram statistics for a layer channel."""
        try:
            CHANNEL_MAP = {
                "value": Gimp.HistogramChannel.VALUE,
                "red":   Gimp.HistogramChannel.RED,
                "green": Gimp.HistogramChannel.GREEN,
                "blue":  Gimp.HistogramChannel.BLUE,
                "alpha": Gimp.HistogramChannel.ALPHA,
            }
            image_index = int(params.get("image_index", 0))
            channel_str = params.get("channel", "value")
            image    = self._get_image(image_index)
            drawable = (image.get_selected_layers() or image.get_layers() or [None])[0]
            channel  = CHANNEL_MAP.get(channel_str.lower(), Gimp.HistogramChannel.VALUE)
            pdb = Gimp.get_pdb()
            proc = pdb.lookup_procedure("gimp-drawable-histogram")
            if proc:
                cfg = proc.create_config()
                cfg.set_property("drawable",    drawable)
                cfg.set_property("channel",     channel)
                cfg.set_property("start-range", 0.0)
                cfg.set_property("end-range",   1.0)
                result = proc.run(cfg)
                # Index 0 holds the PDB status; the return values documented as
                # mean, std-dev, median, pixels, count, percentile start at 1.
                if result.index(0) != Gimp.PDBStatusType.SUCCESS:
                    return {"status": "error", "error": "gimp-drawable-histogram did not succeed"}

                def _safe(idx):
                    try:
                        return result.index(idx)
                    except Exception:
                        return 0
                return {
                    "status": "success",
                    "results": {
                        "mean":    _safe(1),
                        "std_dev": _safe(2),
                        "median":  _safe(3),
                        "pixels":  _safe(4),
                        "count":   _safe(5),
                    }
                }
            else:
                return {"status": "error", "error": "gimp-drawable-histogram not available"}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


Gimp.main(MCPPlugin.__gtype__, sys.argv)
