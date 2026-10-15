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
import base64
import tempfile
import re
import os
import platform
import signal

# Constants for configuration and thresholds
LARGE_SCALING_THRESHOLD = 4.0  # Warn if scaling ratio exceeds this value
MAX_REGION_SIZE = 8192  # Maximum region dimension in pixels
DEFAULT_TIMEOUT_SECONDS = 30  # Default timeout for operations


def N_(message): return message
def _(message): return GLib.dgettext(None, message)


def exec_and_get_results(command, context):
    buffer = io.StringIO()
    original_stdout = sys.stdout
    sys.stdout = buffer
    try:
        exec(command, context)
    finally:
        # Without this, an exception leaves the whole plug-in printing into a
        # discarded StringIO and every later log line silently disappears.
        sys.stdout = original_stdout
    output = buffer.getvalue()
    return output


class MCPPlugin(Gimp.PlugIn):
    def __init__(self, host='localhost', port=9877):
        super().__init__()
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None
        self._needs_rebind = False
        self.context = {}
        exec("from gi.repository import Gimp", self.context)
        self.auto_disconnect_client = True
        # GIMP 3.2 gives plug-ins no way to enumerate displays, so remember the
        # ones we create (image_id -> display_id) for raise/close.
        self.display_of_image = {}

    def do_set_i18n(self, procname):
        # Plugin has no translations; tell GIMP so it stops logging
        # "catalog directory does not exist" for every registered procedure.
        return False

    def do_query_procedures(self):
        """Register the plugin procedures."""
        return ["plug-in-mcp-server", "plug-in-mcp-check", "plug-in-mcp-restart"]

    def do_create_procedure(self, name):
        """Define the procedure properties."""
        if name == "plug-in-mcp-check":
            procedure = Gimp.Procedure.new(self, name, Gimp.PDBProcType.PLUGIN, self._run_check, None)
            procedure.set_menu_label(_("Check MCP Server"))
            procedure.set_documentation(_("Check whether the MCP server is running"),
                                        _("Prints MCP server status to the GIMP console"),
                                        name)
            procedure.set_attribution("Viesar Lab", "Viesar Lab", "2026")
            procedure.add_enum_argument("run-mode", _("Run mode"), _("The run mode"),
                                        Gimp.RunMode, Gimp.RunMode.INTERACTIVE,
                                        GObject.ParamFlags.READWRITE)
            procedure.add_menu_path('<Image>/Tools/MCP')
            return procedure

        if name == "plug-in-mcp-restart":
            procedure = Gimp.Procedure.new(self, name, Gimp.PDBProcType.PLUGIN, self._run_restart, None)
            procedure.set_menu_label(_("Restart MCP Server"))
            procedure.set_documentation(_("Restart the MCP server socket"),
                                        _("Drops and re-binds the MCP server socket on port 9877"),
                                        name)
            procedure.set_attribution("Viesar Lab", "Viesar Lab", "2026")
            procedure.add_enum_argument("run-mode", _("Run mode"), _("The run mode"),
                                        Gimp.RunMode, Gimp.RunMode.INTERACTIVE,
                                        GObject.ParamFlags.READWRITE)
            procedure.add_menu_path('<Image>/Tools/MCP')
            return procedure

        # Default: plug-in-mcp-server
        procedure = Gimp.Procedure.new(self, name, Gimp.PDBProcType.PLUGIN, self.run, None)
        procedure.set_menu_label(_("Start MCP Server"))
        procedure.set_documentation(_("Starts an MCP server to control GIMP externally"),
                                    _("Starts an MCP server to control GIMP externally"),
                                    name)
        procedure.set_attribution("Viesar Lab", "Viesar Lab", "2026")
        procedure.add_enum_argument("run-mode", _("Run mode"), _("The run mode"),
                                    Gimp.RunMode, Gimp.RunMode.INTERACTIVE,
                                    GObject.ParamFlags.READWRITE)
        procedure.add_menu_path('<Image>/Tools/MCP')
        return procedure

    def _run_check(self, procedure, config, run_data):
        """Menu action: print server status."""
        status = "RUNNING" if self.running else "STOPPED"
        print(f"[MCP] Server status: {status} on port {self.port}")
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

    def _run_restart(self, procedure, config, run_data):
        """Menu action: restart the server socket."""
        result = self._restart_server()
        print(f"[MCP] Restart result: {result}")
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

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

    def _make_listener(self):
        """Create and bind the MCP listening socket."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)
        sock.bind((self.host, self.port))
        # A backlog of 1 makes a burst of concurrent commands fail with
        # "connection reset" even though the server is healthy.
        sock.listen(16)
        return sock

    def _start_server_thread(self):
        """Core server loop — runs in a background thread."""
        self.running = True
        try:
            print("Creating socket...")
            self.socket = self._make_listener()
            print(f"GimpMCP server started on {self.host}:{self.port}")

            while self.running:
                if self._needs_rebind:
                    # Only this thread may own the listener; see _restart_server.
                    self._needs_rebind = False
                    try:
                        self.socket = self._make_listener()
                        print(f"GimpMCP listener rebound on {self.host}:{self.port}")
                    except Exception as e:
                        print(f"Error rebinding MCP socket: {str(e)}")
                        self.running = False
                        break
                try:
                    client, address = self.socket.accept()
                    print(f"Connected to client: {address}")
                except socket.timeout:
                    continue
                except OSError:
                    if self.running:
                        # A restart closed the listener; loop rebinds it next pass.
                        continue
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

    def run(self, procedure, config, run_data):
        """Menu handler: start the server."""
        if self.running:
            print("MCP Server is already running")
            return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

        signal.signal(signal.SIGTERM, self.shutdown_server)
        signal.signal(signal.SIGINT, self.shutdown_server)

        # Server socket runs in a background thread
        server_thread = threading.Thread(target=self._start_server_thread, daemon=True)
        server_thread.start()

        # GLib main loop runs in the main thread — required for GIMP API calls
        # (all Gimp.* calls go over the wire protocol which needs GLib to dispatch)
        self._glib_loop = GLib.MainLoop()
        self._glib_loop.run()

        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

    _MAIN_TIMEOUT = 300

    def _run_on_main(self, fn):
        """Run fn on the plug-in's GLib main thread and wait for its result.

        Every libgimp call rides the plug-in wire protocol that the main loop
        owns. Running two commands from client threads at the same time crashes
        the plug-in process (and with it GIMP), so commands are serialised here.
        """
        box = {}
        done = threading.Event()

        def _idle():
            try:
                box['value'] = fn()
            except BaseException as e:      # noqa: BLE001 - reported to the client
                box['error'] = e
            done.set()
            return False

        GLib.idle_add(_idle)
        if not done.wait(self._MAIN_TIMEOUT):
            raise RuntimeError(
                f"GIMP did not answer within {self._MAIN_TIMEOUT}s; the main loop "
                f"may be blocked by a modal dialog")
        if 'error' in box:
            raise box['error']
        return box.get('value')

    def _recv_request(self, client, buf):
        """Pull one complete JSON document (or the disable_auto_disconnect
        sentinel) off the stream.

        Returns (request_text, leftover_bytes) or (None, buf) at end of stream.
        Reading one document at a time is what lets a client keep the connection
        open and pipeline commands without this handler blocking forever on a
        buffer that can never parse.
        """
        decoder = json.JSONDecoder()
        while True:
            text = buf.decode('utf-8', 'replace').strip() if buf else ''
            if text == "disable_auto_disconnect":
                return text, b''
            if text:
                try:
                    _obj, idx = decoder.raw_decode(text)
                    return text[:idx], text[idx:].lstrip().encode('utf-8')
                except ValueError:
                    pass
            try:
                data = client.recv(4096)
            except (socket.timeout, OSError, UnicodeDecodeError):
                return None, buf
            if not data:
                return None, buf
            buf += data

    def _handle_client(self, client):
        """Serve commands on one connection until the client hangs up."""
        buf = b''
        try:
            # A client that stops talking must not hold this thread forever.
            client.settimeout(300)
            while True:
                request, buf = self._recv_request(client, buf)
                if request is None:
                    if buf:
                        print(f"[MCP] Discarding unread request bytes: {buf[:80]!r}")
                    break

                try:
                    response = self._run_on_main(lambda: self.execute_command(request))
                except Exception as e:
                    response = {"status": "error", "error": f"{type(e).__name__}: {e}",
                                "traceback": traceback.format_exc()}

                if isinstance(response, dict):
                    try:
                        response_str = json.dumps(response)
                    except (TypeError, ValueError) as e:
                        # A stray Gimp object must not leave the client with an
                        # empty reply it cannot parse.
                        response_str = json.dumps({
                            "status": "error",
                            "error": f"response was not JSON serializable: {e}",
                        })
                else:
                    response_str = str(response)

                payload = (response_str + "\n").encode('utf-8')
                try:
                    client.sendall(payload)
                except OSError as e:
                    print(f"[MCP] Could not deliver {len(payload)}-byte response: {e}")
                    break
        except Exception as e:
            print(f"[MCP] Client handler error: {e}")
            traceback.print_exc()
        finally:
            if self.auto_disconnect_client:
                try:
                    client.close()
                except Exception:
                    pass
        return

    def execute_command(self, request):
        """Execute commands in GIMP's main thread."""
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
            elif "type" in j and j["type"] == "check_server":
                return {"status": "success", "results": {"running": True, "port": self.port}}
            elif "type" in j and j["type"] == "restart_server":
                return self._restart_server()
            elif "type" in j and j["type"] == "new_canvas":
                params = j.get("params", {})
                return self._new_canvas(params)
            # ── Category 1: File Operations ──────────────────────────────────
            elif "type" in j and j["type"] == "open_image":
                return self._open_image(j.get("params", {}))
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

            if a[0] in ('python-fu-eval', 'pyGObject-eval'):
                # the MCP tool docstring documents "pyGObject-eval"; accept both
                # spellings rather than exec()'ing the expression and reporting OK
                if len(a) > 1:
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
            
            # Extract region parameters if provided
            region = params.get("region", {})
            
            # Validate region parameters if provided
            if region:
                # Validate region parameter types
                for key, expected_type in [("origin_x", int), ("origin_y", int), 
                                         ("width", int), ("height", int),
                                         ("max_width", int), ("max_height", int)]:
                    if key in region and region[key] is not None:
                        if not isinstance(region[key], expected_type):
                            return {
                                "status": "error",
                                "error": f"Region parameter '{key}' must be of type {expected_type.__name__}, got {type(region[key]).__name__}"
                            }
                        if region[key] < 0:
                            return {
                                "status": "error", 
                                "error": f"Region parameter '{key}' must be non-negative, got {region[key]}"
                            }
            
            origin_x = region.get("origin_x")
            origin_y = region.get("origin_y")
            region_width = region.get("width")
            region_height = region.get("height")
            scaled_to_width = region.get("max_width")  # Region scaling uses max_width/max_height
            scaled_to_height = region.get("max_height")

            # Get the requested image (index 0 = most recently added)
            images = Gimp.get_images()
            if not images:
                return {
                    "status": "error",
                    "error": "No images are currently open in GIMP"
                }
            image_index = int(params.get("image_index", 0))
            if not (0 <= image_index < len(images)):
                return {
                    "status": "error",
                    "error": f"image_index {image_index} out of range (only {len(images)} images open)"
                }
            original_image = images[image_index]
            
            # Get original image dimensions
            orig_img_width = original_image.get_width()
            orig_img_height = original_image.get_height()
            
            # Determine working image and region
            working_image = None
            should_delete_working = False
            
            # Case 1: Region selection
            if any(param is not None for param in [origin_x, origin_y, region_width, region_height]):
                print("Processing region extraction...")
                
                # Validate region parameters
                if origin_x is None or origin_y is None or region_width is None or region_height is None:
                    return {
                        "status": "error",
                        "error": "For region selection, all parameters are required: origin_x, origin_y, width, height"
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
                
                # Create new image with the region
                working_image = Gimp.Image.new(region_width, region_height, original_image.get_base_type())
                should_delete_working = True
                
                # Copy the region from original image
                # First, select the region in the original image
                original_image.select_rectangle(Gimp.ChannelOps.REPLACE, origin_x, origin_y, region_width, region_height)
                
                # Get the active layer from original image
                orig_layers = original_image.get_layers()
                if not orig_layers:
                    return {
                        "status": "error",
                        "error": "No layers found in original image"
                    }
                
                # Create a new layer in working image
                # In GIMP 3.0+, use the image's base type instead of layer.get_image_type()
                try:
                    # Try to get layer type - fallback to image base type
                    if hasattr(orig_layers[0], 'get_type'):
                        layer_type = orig_layers[0].get_type()
                    else:
                        # Use image base type as fallback
                        layer_type = original_image.get_base_type()
                except AttributeError:
                    # Final fallback - use RGB
                    layer_type = Gimp.ImageBaseType.RGB
                
                new_layer = Gimp.Layer.new(working_image, 'Region', region_width, region_height, 
                                         layer_type, 100, Gimp.LayerMode.NORMAL)
                working_image.insert_layer(new_layer, None, 0)
                
                # Copy and paste the selection
                Gimp.edit_copy([orig_layers[0]])
                floating_sel = Gimp.edit_paste(new_layer, True)[0]
                Gimp.floating_sel_anchor(floating_sel)
                
                # Clear selection
                try:
                    # Try different methods to clear selection based on GIMP version
                    if hasattr(original_image, 'select_none'):
                        original_image.select_none()
                    else:
                        # Use Gimp.Selection.none() for GIMP 3.0+
                        Gimp.Selection.none(original_image)
                except (AttributeError, RuntimeError) as e:
                    print(f"Warning: Could not clear selection: {e}")
                
            else:
                # Case 2: Full image
                print("Processing full image...")
                working_image = original_image
                should_delete_working = False
            
            # Now handle scaling if needed
            final_image = working_image
            should_delete_final = should_delete_working
            
            # Calculate target dimensions
            current_width = working_image.get_width()
            current_height = working_image.get_height()
            target_width = current_width
            target_height = current_height
            
            # Determine scaling target — either bound alone is enough
            if scaled_to_width is not None or scaled_to_height is not None:
                max_w, max_h = scaled_to_width, scaled_to_height
            elif max_width is not None or max_height is not None:
                max_w, max_h = max_width, max_height
            else:
                max_w = max_h = None

            # Apply center inside scaling if target dimensions provided
            if max_w is not None or max_h is not None:
                aspect_ratio = current_width / current_height
                if max_w is not None and max_h is not None:
                    if aspect_ratio > max_w / max_h:
                        target_width, target_height = max_w, int(max_w / aspect_ratio)
                    else:
                        target_height, target_width = max_h, int(max_h * aspect_ratio)
                elif max_w is not None:
                    target_width = max_w
                    target_height = max(1, int(round(max_w / aspect_ratio)))
                else:
                    target_height = max_h
                    target_width = max(1, int(round(max_h * aspect_ratio)))
                
                print(f"Scaling from {current_width}x{current_height} to {target_width}x{target_height}")
                
                # Scale the image if dimensions changed
                if target_width != current_width or target_height != current_height:
                    # Create scaled image
                    final_image = working_image.duplicate()
                    should_delete_final = True
                    
                    # Scale the image with timeout consideration for large operations
                    scaling_ratio = (target_width * target_height) / (current_width * current_height)
                    if scaling_ratio > LARGE_SCALING_THRESHOLD:  # Scaling up significantly
                        print(f"Warning: Large scaling operation detected (ratio: {scaling_ratio:.2f}). This may take time.")
                    
                    try:
                        final_image.scale(target_width, target_height)
                    except (RuntimeError) as scale_error:
                        # Clean up and return error for scaling failures
                        if should_delete_final:
                            try:
                                final_image.delete()
                            except (AttributeError, RuntimeError):
                                pass
                        raise RuntimeError(f"Failed to scale image from {current_width}x{current_height} to {target_width}x{target_height}: {scale_error}")
        
            # Create a temporary file for export
            temp_fd, temp_path = tempfile.mkstemp(suffix='.png')
            os.close(temp_fd)  # Close the file descriptor as GIMP will handle the file
            
            try:
                # Export the final image as PNG
                # Get all layers - we'll export the flattened image
                layers = final_image.get_layers()
                if not layers:
                    return {
                        "status": "error", 
                        "error": "No layers found in the processed image"
                    }
                
                # For PNG export, we can use all layers or the active layer
                try:
                    drawable = (final_image.get_selected_layers() or final_image.get_layers() or [None])[0]
                except (AttributeError, RuntimeError):
                    # If get_active_layer doesn't exist or fails, use the first layer
                    drawable = layers[0]
                
                if not drawable:
                    drawable = layers[0]
                
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
                    # Try different property names that might exist
                    try:
                        export_config.set_property('drawable', drawable)
                    except Exception:
                        try:
                            export_config.set_property('drawables', [drawable])
                        except Exception:
                            # Some export procedures might not need drawable specification
                            pass
                    
                    result = export_proc.run(export_config)
                    self._check_run(result, 'file-png-export')
                    print(f"Export result: {result}")
                    
                except Exception as export_error:
                    print(f"Export error: {export_error}")
                    # Fallback: try using the PDB directly with correct arguments
                    try:
                        from gi.repository import Gio
                        file_obj = Gio.File.new_for_path(temp_path)
                        
                        # Try alternative approach using Gimp.file_save with correct number of arguments
                        Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, final_image, file_obj)
                        print("Fallback export successful")
                    except Exception as fallback_error:
                        print(f"Fallback export error: {fallback_error}")
                        # Try another fallback using gimp-file-save PDB procedure
                        try:
                            pdb = Gimp.get_pdb()
                            save_proc = pdb.lookup_procedure('gimp-file-save')
                            if save_proc:
                                save_config = save_proc.create_config()
                                save_config.set_property('run-mode',
                                                          Gimp.RunMode.NONINTERACTIVE)
                                save_config.set_property('image', final_image)
                                save_config.set_property('file', file_obj)
                                save_result = save_proc.run(save_config)
                                self._check_run(save_result, 'gimp-file-save')
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
                            "region_extracted": any(param is not None for param in [origin_x, origin_y, region_width, region_height]),
                            "scaled": target_width != current_width or target_height != current_height,
                            "region_coords": {"x": origin_x, "y": origin_y, "w": region_width, "h": region_height} if origin_x is not None else None
                        }
                    }
                }
                
            finally:
                # Clean up temporary images
                if should_delete_final and final_image != working_image:
                    try:
                        final_image.delete()
                    except (AttributeError, RuntimeError) as e:
                        print(f"Warning: Failed to delete final temporary image: {e}")
                if should_delete_working and working_image != original_image:
                    try:
                        working_image.delete()
                    except (AttributeError, RuntimeError) as e:
                        print(f"Warning: Failed to delete working temporary image: {e}")
                        
                # Clean up the temporary file
                if os.path.exists(temp_path):
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
                        # every layer inherits get_children, so hasattr() was
                        # always True here; ask GIMP instead
                        "is_group": layer.is_group_layer(),
                        "layer_type": self._get_layer_type_string(layer)
                    }
                    # Try to get layer mode if available
                    try:
                        layer_info["blend_mode"] = layer.get_mode().value_nick.upper()
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
            
            # Get resolution information — gimp_image_get_resolution() returns
            # (success, xres, yres), so unpacking into two names always failed.
            resolution_x = resolution_y = None
            try:
                _ok, resolution_x, resolution_y = image.get_resolution()
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
                    "color_mode": base_type_str,
                    "precision": precision_str,
                    "num_layers": len(layers),
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
        """Describe the layer's pixel type.

        GimpLayer has no get_type()/get_image_type() in the 3.2 bindings, and the
        GObject `type` attribute is a bound method — derive the type from the
        image base plus whether the layer carries alpha, which is what a caller
        actually needs to know.
        """
        try:
            base = layer.get_image().get_base_type()
            alpha = "_A" if layer.has_alpha() else ""
            if base == Gimp.ImageBaseType.RGB:
                return f"RGB{alpha}"
            if base == Gimp.ImageBaseType.GRAY:
                return f"GRAY{alpha}"
            if base == Gimp.ImageBaseType.INDEXED:
                return f"INDEXED{alpha}"
            return str(base)
        except Exception as e:
            print(f"Warning: Could not determine layer type: {e}")
            return "unknown"

    def _get_gimp_info(self):
        """Get comprehensive information about GIMP installation and environment."""
        try:
            print("Getting GIMP environment information...")
            
            gimp_info = {}
            
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

    def _restart_server(self):
        """Restart the MCP socket server in-place.

        The accept thread owns the listening socket. Re-binding here would close
        the socket under a thread blocked in accept() and leave it with a dead
        listener — the restart is requested and the thread performs it.
        """
        import time
        try:
            if not self.running:
                return {"status": "error",
                        "error": "MCP server is not running — start it from "
                                 "Tools > MCP > Start MCP Server"}
            print("Restarting MCP server socket...")
            old, self.socket = self.socket, None
            self._needs_rebind = True
            try:
                old.close()
            except Exception:
                pass

            for _ in range(50):
                time.sleep(0.1)
                if self.socket is not None or not self.running:
                    break
            if self.socket is None:
                return {"status": "error",
                        "error": f"server did not re-bind {self.host}:{self.port} within 5s"}
            print(f"MCP server restarted on {self.host}:{self.port}")
            return {
                "status": "success",
                "results": {"restarted": True, "host": self.host, "port": self.port}
            }
        except Exception as e:
            return {
                "status": "error",
                "error": f"Restart failed: {str(e)}",
                "traceback": traceback.format_exc()
            }

    def _new_canvas(self, params):
        """Create a new blank canvas and open it in a GIMP display window."""
        try:
            width  = int(params.get("width", 1024))
            height = int(params.get("height", 1024))
            name   = str(params.get("name", "Untitled"))
            color_mode = str(params.get("color_mode", "RGB")).upper()
            fill   = str(params.get("fill", "white"))
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
            if color_mode not in mode_map:
                return {"status": "error",
                        "error": f"unknown color_mode '{color_mode}' "
                                 f"(supported: {', '.join(mode_map)})"}
            base_type  = mode_map[color_mode]
            layer_type = layer_type_map[color_mode]

            image = Gimp.Image.new(width, height, base_type)
            image.set_resolution(resolution, resolution)

            layer = Gimp.Layer.new(image, name, width, height, layer_type, 100, Gimp.LayerMode.NORMAL)
            image.insert_layer(layer, None, 0)

            if fill.lower() == "transparent":
                layer.add_alpha()
                Gimp.Drawable.edit_fill(layer, Gimp.FillType.TRANSPARENT)
            else:
                bg_color = self._color(fill)
                Gimp.context_set_background(bg_color)
                Gimp.Drawable.edit_fill(layer, Gimp.FillType.BACKGROUND)

            display_opened = self._attach_display(image)

            print(f"New canvas created: {width}x{height} {color_mode} fill={fill}")
            return {
                "status": "success",
                "results": {
                    "image_id": image.get_id(),
                    "name": image.get_name(),
                    "width": width,
                    "height": height,
                    "color_mode": color_mode,
                    "fill": fill,
                    "resolution": resolution,
                    "display_opened": display_opened
                }
            }
        except Exception as e:
            # Never leave a half-built, layer-less image behind — later exports
            # of it can crash GIMP.
            try:
                if locals().get("image") is not None:
                    image.delete()
            except Exception:
                pass
            return {
                "status": "error",
                "error": f"new_canvas failed: {str(e)}",
                "traceback": traceback.format_exc()
            }


    # =========================================================================
    # SHARED HELPERS
    # =========================================================================

    def _get_image(self, image_index):
        """Return the image at image_index from Gimp.get_images(), raise if none open."""
        if image_index < 0:
            raise RuntimeError(f"image_index must be >= 0, got {image_index}")
        images = Gimp.get_images()
        if not images:
            raise RuntimeError("No images are currently open in GIMP")
        if image_index >= len(images):
            raise RuntimeError(f"image_index {image_index} out of range (only {len(images)} images open)")
        return images[image_index]

    def _attach_display(self, image):
        """Open a window for an image. A window is cosmetic: in batch or
        no-interface GIMP it fails, and that must not fail the operation that
        already succeeded."""
        try:
            display = Gimp.Display.new(image)
            Gimp.displays_flush()
            self.display_of_image[image.get_id()] = display.get_id()
            return True
        except Exception as e:
            print(f"[MCP] No window for image {image.get_id()}: {e}")
            return False

    def _resolve_layer(self, image, layer_name, layer_index):
        """Resolve a layer by name, index, or fall back to the active layer."""
        if layer_name is not None:
            layers = image.get_layers()
            for layer in layers:
                if layer.get_name() == layer_name:
                    return layer
            raise RuntimeError(f"Layer '{layer_name}' not found")
        if layer_index is not None:
            if layer_index < 0:
                raise RuntimeError(f"layer_index must be >= 0, got {layer_index}")
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
        """Map operation string to Gimp.ChannelOps enum value.

        An unknown string must not quietly become REPLACE: that wipes whatever
        selection the caller already had.
        """
        ops = {
            "replace":   Gimp.ChannelOps.REPLACE,
            "add":       Gimp.ChannelOps.ADD,
            "subtract":  Gimp.ChannelOps.SUBTRACT,
            "intersect": Gimp.ChannelOps.INTERSECT,
        }
        value = ops.get(str(op).lower())
        if value is None:
            raise RuntimeError(f"unknown selection operation '{op}' "
                               f"(supported: {', '.join(ops)})")
        return value

    _RGB_RE = re.compile(r"^\s*(rgba?)\s*\(([^)]*)\)\s*$", re.I)
    _HSL_RE = re.compile(r"^\s*(hsla?)\s*\(([^)]*)\)\s*$", re.I)
    _COLOR_RE = re.compile(r"^\s*#?([0-9a-f]{3,8})\s*$", re.I)

    # Gegl only understands a small colour vocabulary and quietly returns cyan
    # (0,1,1,0.67) for anything else — "lightsteelblue" included. Named colours
    # are resolved here so a wrong colour can never be painted silently.
    CSS_COLORS = {
        name: int(value, 16) for name, value in (
            line.split("=") for line in (
                "aliceblue=f0f8ff|antiquewhite=faebd7|aqua=00ffff|aquamarine=7fffd4|azure=f0ffff|"
                "beige=f5f5dc|bisque=ffe4c4|black=000000|blanchedalmond=ffebcd|blue=0000ff|"
                "blueviolet=8a2be2|brown=a52a2a|burlywood=deb887|cadetblue=5f9ea0|chartreuse=7fff00|"
                "chocolate=d2691e|coral=ff7f50|cornflowerblue=6495ed|cornsilk=fff8dc|crimson=dc143c|"
                "cyan=00ffff|darkblue=00008b|darkcyan=008b8b|darkgoldenrod=b8860b|darkgray=a9a9a9|"
                "darkgreen=006400|darkgrey=a9a9a9|darkkhaki=bdb76b|darkmagenta=8b008b|"
                "darkolivegreen=556b2f|darkorange=ff8c00|darkorchid=9932cc|darkred=8b0000|"
                "darksalmon=e9967a|darkseagreen=8fbc8f|darkslateblue=483d8b|darkslategray=2f4f4f|"
                "darkturquoise=00ced1|darkviolet=9400d3|deeppink=ff1493|deepskyblue=00bfff|"
                "dimgray=696969|dodgerblue=1e90ff|firebrick=b22222|floralwhite=fffaf0|forestgreen=228b22|"
                "fuchsia=ff00ff|gainsboro=dcdcdc|ghostwhite=f8f8ff|gold=ffd700|goldenrod=daa520|"
                "gray=808080|green=008000|greenyellow=adff2f|grey=808080|honeydew=f0fff0|"
                "hotpink=ff69b4|indianred=cd5c5c|indigo=4b0082|ivory=fffff0|khaki=f0e68c|lavender=e6e6fa|"
                "lavenderblush=fff0f5|lawngreen=7cfc00|lemonchiffon=fffacd|lightblue=add8e6|"
                "lightcoral=f08080|lightcyan=e0ffff|lightgoldenrodyellow=fafad2|lightgray=d3d3d3|"
                "lightgreen=90ee90|lightpink=ffb6c1|lightsalmon=ffa07a|lightseagreen=20b2aa|"
                "lightskyblue=87cefa|lightslategray=778899|lightsteelblue=b0c4de|lightyellow=ffffe0|"
                "lime=00ff00|limegreen=32cd32|linen=faf0e6|magenta=ff00ff|maroon=800000|"
                "mediumaquamarine=66cdaa|mediumblue=0000cd|mediumorchid=ba55d3|mediumpurple=9370db|"
                "mediumseagreen=3cb371|mediumslateblue=7b68ee|mediumspringgreen=00fa9a|"
                "mediumturquoise=48d1cc|mediumvioletred=c71585|midnightblue=191970|mintcream=f5fffa|"
                "mistyrose=ffe4e1|moccasin=ffe4b5|navajowhite=ffdead|navy=000080|oldlace=fdf5e6|"
                "olive=808000|olivedrab=6b8e23|orange=ffa500|orangered=ff4500|orchid=da70d6|"
                "palegoldenrod=eee8aa|palegreen=98fb98|paleturquoise=afeeee|palevioletred=db7093|"
                "papayawhip=ffefd5|peachpuff=ffdab9|peru=cd853f|pink=ffc0cb|plum=dda0dd|"
                "powderblue=b0e0e6|purple=800080|rebeccapurple=663399|red=ff0000|rosybrown=bc8f8f|"
                "saddlebrown=8b4513|salmon=fa8072|sandybrown=f4a460|seagreen=2e8b57|seashell=fff5ee|"
                "sienna=a0522d|silver=c0c0c0|skyblue=87ceeb|slateblue=6a5acd|slategray=708090|"
                "snow=fffafa|springgreen=00ff7f|steelblue=4682b4|tan=d2b48c|teal=008080|"
                "thistle=d8bfd8|tomato=ff6347|turquoise=40e0d0|violet=ee82ee|wheat=f5deb7|"
                "white=ffffff|whitesmoke=f5f5f5|yellow=ffff00|yellowgreen=9acd32"
            ).split("|"))
    }

    @staticmethod
    def _hsl_to_rgb(hue, sat, light):
        """Convert hue 0-360, saturation/lightness 0-1 to 0-255 RGB."""
        h = hue % 360.0
        c = (1 - abs(2 * light - 1)) * sat
        x = c * (1 - abs((h / 60.0) % 2 - 1))
        m = light - c / 2
        r, g, b = [(c, x, 0), (x, c, 0), (0, c, x),
                   (0, x, c), (x, 0, c), (c, 0, x)][int(h // 60) % 6]
        return tuple(int(round((v + m) * 255)) for v in (r, g, b))

    def _color(self, spec):
        """Parse a CSS colour name, #hex, rgb()/rgba() or hsl()/hsla() into a Gegl.Color.

        Gegl reads rgb() components as 0.0-1.0 *linear* floats, so the 0-255 form
        advertised in this project's docs (rgb(100,200,50)) silently becomes white,
        and any name outside its small vocabulary silently becomes cyan. Parsing is
        done here so an unusable colour raises instead of painting the wrong thing.
        """
        from gi.repository import Gegl
        if spec is None or not str(spec).strip():
            raise RuntimeError("no colour given")
        text = str(spec).strip()
        alpha = None

        m = self._RGB_RE.match(text)
        if m:
            parts = [p.strip() for p in m.group(2).split(",") if p.strip()]
            try:
                nums = [float(p[:-1]) / 100.0 if p.endswith("%") else float(p)
                        for p in parts]
            except ValueError:
                raise RuntimeError(f"could not parse colour '{spec}'")
            if len(nums) not in (3, 4) or any(v < 0 for v in nums):
                raise RuntimeError(f"colour '{spec}' must have 3 or 4 components")
            if len(nums) == 4:
                alpha = nums[3] / 255.0 if nums[3] > 1.0 else nums[3]
            rgb = [int(round(min(255.0, max(0.0, v)))) if max(nums[:3]) > 1.0
                   else int(round(min(1.0, max(0.0, v)) * 255)) for v in nums[:3]]
        else:
            m = self._HSL_RE.match(text)
            if m:
                parts = [p.strip() for p in m.group(2).split(",") if p.strip()]
                try:
                    h = float(parts[0].rstrip("deg"))
                    sat = float(parts[1].rstrip("%")) / 100.0
                    light = float(parts[2].rstrip("%")) / 100.0
                    if len(parts) > 3:
                        alpha = float(parts[3].rstrip("%"))
                        alpha = alpha / 100.0 if alpha > 1.0 else alpha
                except (ValueError, IndexError):
                    raise RuntimeError(f"could not parse colour '{spec}'")
                rgb = list(self._hsl_to_rgb(h, sat, light))
            else:
                named = self.CSS_COLORS.get(text.lower().replace(" ", ""))
                hx = None
                if named is not None:
                    hx = f"{named:06x}"
                else:
                    m = self._COLOR_RE.match(text)
                    if m:
                        digits = m.group(1).lower()
                        if len(digits) in (3, 4):
                            digits = "".join(d * 2 for d in digits)
                        if len(digits) == 8:
                            alpha = int(digits[6:8], 16) / 255.0
                            digits = digits[:6]
                        if len(digits) == 6:
                            hx = digits
                if hx is None:
                    raise RuntimeError(
                        f"could not parse colour '{spec}' — use a CSS colour name, "
                        f"#rgb/#rrggbb, rgb()/rgba() or hsl()/hsla()")
                rgb = [int(hx[i:i + 2], 16) for i in (0, 2, 4)]

        color = Gegl.Color.new("#%02x%02x%02x" % tuple(rgb))
        if color is None:
            raise RuntimeError(f"could not parse colour '{spec}'")
        if alpha is not None:
            # set_rgba takes *linear* floats: feeding it sRGB/255 darkens the
            # colour. Reuse the components Gegl already decoded from the hex.
            r, g, b, _a = color.get_rgba()
            color.set_rgba(r, g, b, min(1.0, max(0.0, alpha)))
        return color

    def _interp_from_string(self, interp):
        """Map interpolation string to Gimp.InterpolationType."""
        interps = {
            "cubic":  Gimp.InterpolationType.CUBIC,
            "linear": Gimp.InterpolationType.LINEAR,
            "none":   Gimp.InterpolationType.NONE,
        }
        value = interps.get(str(interp).lower())
        if value is None:
            raise RuntimeError(f"unknown interpolation '{interp}' "
                               f"(supported: {', '.join(interps)})")
        return value

    _EXPORT_RETURN = getattr(Gimp, "ExportReturn", None)

    def _check_run(self, result, what):
        """Validate a PDB run(): index(0) is the execution status, and export
        procedures add a Gimp.ExportReturn at index(1)."""
        if result is None:
            raise RuntimeError(f"{what} returned no result")
        status = result.index(0)
        if status != Gimp.PDBStatusType.SUCCESS:
            raise RuntimeError(f"{what} failed ({status})")
        if result.length() > 1 and self._EXPORT_RETURN is not None:
            second = result.index(1)
            if isinstance(second, self._EXPORT_RETURN) and second != self._EXPORT_RETURN.SUCCESS:
                raise RuntimeError(f"{what} did not write the file ({second})")
        return result

    def _export_to_path(self, image, file_path, fmt, quality, flatten):
        """Export image to file_path in the given format. Returns file size in bytes.

        GIMP 3.x renamed the per-format writers from file-*-save to file-*-export;
        an unknown format raises rather than quietly writing PNG bytes under a
        .jpg/.webp name.
        """
        from gi.repository import Gio
        export_procs = {
            "png":  "file-png-export",
            "jpeg": "file-jpeg-export",
            "jpg":  "file-jpeg-export",
            "webp": "file-webp-export",
            "tiff": "file-tiff-export",
            "tif":  "file-tiff-export",
            "bmp":  "file-bmp-export",
        }
        fmt_lower = str(fmt).lower()
        proc_name = export_procs.get(fmt_lower)
        if proc_name is None:
            raise RuntimeError(
                f"Unsupported export format '{fmt}' (supported: {sorted(set(export_procs))})"
            )

        work = image
        should_delete = False
        if flatten:
            work = image.duplicate()
            work.flatten()
            should_delete = True
        if not work.get_layers():
            # Exporting a layer-less image crashes GIMP's file-png plug-in with
            # a segfault, which takes the whole session with it.
            if should_delete:
                try:
                    work.delete()
                except Exception:
                    pass
            raise RuntimeError("refusing to export an image with no layers")
        try:
            gio_file = Gio.File.new_for_path(file_path)
            proc = Gimp.get_pdb().lookup_procedure(proc_name)
            if proc is None:
                raise RuntimeError(f"PDB procedure '{proc_name}' is not available in this GIMP")
            cfg = proc.create_config()
            if cfg.find_property("run-mode") is not None:
                cfg.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
            cfg.set_property("image", work)
            cfg.set_property("file", gio_file)
            if cfg.find_property("quality") is not None:
                # GIMP 3.x jpeg/webp export quality is a 0-100 double
                cfg.set_property("quality", float(quality))
            self._check_run(proc.run(cfg), proc_name)
            if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
                raise RuntimeError(f"{proc_name} reported success but wrote no file at {file_path}")
            return os.path.getsize(file_path)
        finally:
            if should_delete:
                try:
                    work.delete()
                except Exception:
                    pass

    def _apply_gegl_filter(self, image, drawable, op_name, props):
        """Apply a GEGL operation to a drawable and bake it into the pixels.

        The GIMP 3.2 sequence is: create the filter, set the GEGL properties on
        the filter *config* object (not on the filter itself), append the filter
        to the drawable, then merge it. Anything that does not survive this
        sequence raises instead of reporting a silent success.
        """
        pdb = Gimp.get_pdb()
        proc = pdb.lookup_procedure("gimp-drawable-filter-new")
        if proc is None:
            raise RuntimeError("PDB procedure 'gimp-drawable-filter-new' not available")

        cfg = proc.create_config()
        cfg.set_property("drawable", drawable)
        cfg.set_property("operation-name", op_name)
        cfg.set_property("name", op_name)
        res = proc.run(cfg)
        # index(0) is the PDB status, the filter object is index(1)
        if res.index(0) != Gimp.PDBStatusType.SUCCESS or res.length() < 2:
            raise RuntimeError(f"{op_name}: gimp-drawable-filter-new failed ({res.index(0)})")
        filtr = res.index(1)
        if filtr is None:
            raise RuntimeError(f"{op_name}: no filter object returned")

        try:
            fcfg = filtr.get_config()
            valid = {g.get_name() for g in fcfg.list_properties()}
            unknown = [k for k in props if k not in valid]
            if unknown:
                raise RuntimeError(
                    f"{op_name}: unknown properties {unknown} (available: {sorted(valid)})"
                )
            for key, value in props.items():
                fcfg.set_property(key, value)

            drawable.append_filter(filtr)
            filtr.update()

            merge = pdb.lookup_procedure("gimp-drawable-merge-filters")
            if merge is None:
                raise RuntimeError("PDB procedure 'gimp-drawable-merge-filters' not available")
            mcfg = merge.create_config()
            mcfg.set_property("drawable", drawable)
            mres = merge.run(mcfg)
            if mres.index(0) != Gimp.PDBStatusType.SUCCESS:
                raise RuntimeError(f"{op_name}: merging the filter failed ({mres.index(0)})")
            if drawable.get_filters():
                raise RuntimeError(f"{op_name}: filter is still attached after merge")
        except Exception:
            try:
                filtr.delete()
            except Exception:
                pass
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
            display_opened = self._attach_display(image)
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

    def _save_xcf(self, params):
        """Save image as XCF."""
        try:
            from gi.repository import Gio
            image_index = int(params.get("image_index", 0))
            file_path = params.get("file_path", "")
            image = self._get_image(image_index)
            gio_file = Gio.File.new_for_path(file_path)
            proc = Gimp.get_pdb().lookup_procedure("gimp-xcf-save")
            if proc is None:
                raise RuntimeError("PDB procedure 'gimp-xcf-save' not available")
            cfg = proc.create_config()
            cfg.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
            cfg.set_property("image", image)
            cfg.set_property("file", gio_file)
            self._check_run(proc.run(cfg), "gimp-xcf-save")
            # gimp-xcf-save can report success without writing (unwritable path,
            # missing parent directory); the caller asks for a saved file, so
            # confirm it exists and is non-empty.
            if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
                raise RuntimeError(f"gimp-xcf-save did not write {file_path}")
            return {"status": "success",
                    "results": {"status": "success", "file_path": file_path,
                                "file_size_bytes": os.path.getsize(file_path)}}
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

            images = Gimp.get_images()
            if not images:
                return {"status": "error", "error": "No images open"}

            targets = [(i, img) for i, img in enumerate(images)]
            if image_index is not None:
                image_index = int(image_index)
                targets = [(image_index, self._get_image(image_index))]

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

            if not exported:
                # Nothing was written: report the failure instead of a success
                # whose count happens to be zero.
                return {"status": "error",
                        "error": "no image could be exported",
                        "details": errors}
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
                pdb = Gimp.get_pdb()
                proc = pdb.lookup_procedure("gimp-drawable-levels-stretch")
                if proc is None:
                    raise RuntimeError(
                        "PDB procedure 'gimp-drawable-levels-stretch' not available"
                    )
                cfg = proc.create_config()
                cfg.set_property("drawable", drawable)
                res = proc.run(cfg)
                if res.index(0) != Gimp.PDBStatusType.SUCCESS:
                    raise RuntimeError(f"gimp-drawable-levels-stretch failed ({res.index(0)})")
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
            channel = CHANNEL_MAP.get(str(channel_str).lower())
            if channel is None:
                return {"status": "error",
                        "error": f"unknown channel '{channel_str}' "
                                 f"(supported: {', '.join(CHANNEL_MAP)})"}
            if not PRESETS.get(preset):
                return {"status": "error",
                        "error": f"unknown preset '{preset}' "
                                 f"(supported: {', '.join(PRESETS)}, or pass points)"}

            if custom_pts is not None:
                # Flatten [[in,out],...] -> [in,out,in,out,...]
                if custom_pts and isinstance(custom_pts[0], (list, tuple)):
                    flat = []
                    for pt in custom_pts:
                        flat.extend(pt)
                    control_pts = flat
                else:
                    control_pts = list(custom_pts)
                if len(control_pts) < 4 or len(control_pts) % 2:
                    return {"status": "error",
                            "error": "points must be [input, output] pairs — at least "
                                     "two pairs, given as a flat list or a list of pairs"}
                if any(v < 0 or v > 255 for v in control_pts):
                    return {"status": "error",
                            "error": "curve points are 0-255 input/output pairs; "
                                     "values outside that range are rejected rather than clipped"}
            else:
                control_pts = PRESETS[preset]

            image.undo_group_start()
            try:
                pts_normalized = [p / 255.0 for p in control_pts]
                # set_property can't auto-convert Python list to GimpDoubleArray.
                # Try calling curves_spline as a direct method on the drawable
                # (GI exposes gimp-drawable-curves-spline as drawable.curves_spline).
                try:
                    if not drawable.curves_spline(channel, pts_normalized):
                        raise RuntimeError("gimp-drawable-curves-spline returned failure")
                except AttributeError:
                    # Fallback: use array.array typed buffer which GI may accept
                    import array as _arr
                    typed = _arr.array('d', pts_normalized)
                    pdb = Gimp.get_pdb()
                    proc = pdb.lookup_procedure("gimp-drawable-curves-spline")
                    if proc is None:
                        raise RuntimeError(
                            "PDB procedure 'gimp-drawable-curves-spline' not available")
                    cfg = proc.create_config()
                    cfg.set_property("drawable", drawable)
                    cfg.set_property("channel",  channel)
                    cfg.set_property("points",   typed)
                    self._check_run(proc.run(cfg), "gimp-drawable-curves-spline")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success",
                    "results": {"status": "success", "channel": channel_str,
                                "points": control_pts}}
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
            image.undo_group_start()
            try:
                pdb = Gimp.get_pdb()
                proc = pdb.lookup_procedure("gimp-drawable-brightness-contrast")
                if proc is None:
                    raise RuntimeError(
                        "PDB procedure 'gimp-drawable-brightness-contrast' is not available in this GIMP"
                    )
                cfg = proc.create_config()
                cfg.set_property("drawable", drawable)
                cfg.set_property("brightness", brightness / 127.0)
                cfg.set_property("contrast",   contrast   / 127.0)
                self._check_run(proc.run(cfg), "gimp-drawable-brightness-contrast")
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
            hue_range = HUE_RANGE_MAP.get(str(color_range).lower())
            if hue_range is None:
                return {"status": "error",
                        "error": f"unknown color_range '{color_range}' "
                                 f"(supported: {', '.join(HUE_RANGE_MAP)})"}
            image.undo_group_start()
            try:
                pdb = Gimp.get_pdb()
                proc = pdb.lookup_procedure("gimp-drawable-hue-saturation")
                if proc is None:
                    raise RuntimeError(
                        "PDB procedure 'gimp-drawable-hue-saturation' is not available in this GIMP"
                    )
                cfg = proc.create_config()
                cfg.set_property("drawable", drawable)
                cfg.set_property("hue-range", hue_range)
                cfg.set_property("hue-offset", hue)
                cfg.set_property("lightness",  lightness)
                cfg.set_property("saturation", saturation)
                cfg.set_property("overlap", 0.0)
                self._check_run(proc.run(cfg), "gimp-drawable-hue-saturation")
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
            color_range = RANGE_MAP.get(str(range_str).lower())
            if color_range is None:
                return {"status": "error",
                        "error": f"unknown range '{range_str}' "
                                 f"(supported: {', '.join(RANGE_MAP)})"}
            image.undo_group_start()
            try:
                pdb = Gimp.get_pdb()
                proc = pdb.lookup_procedure("gimp-drawable-color-balance")
                if proc is None:
                    raise RuntimeError(
                        "PDB procedure 'gimp-drawable-color-balance' is not available in this GIMP"
                    )
                cfg = proc.create_config()
                cfg.set_property("drawable",      drawable)
                cfg.set_property("transfer-mode", color_range)
                cfg.set_property("cyan-red",      cyan_red)
                cfg.set_property("magenta-green", magenta_green)
                cfg.set_property("yellow-blue",   yellow_blue)
                cfg.set_property("preserve-lum",  True)
                self._check_run(proc.run(cfg), "gimp-drawable-color-balance")
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
                # plug-in-unsharp-mask was removed in GIMP 3; gegl:unsharp-mask
                # takes a single std-dev and a 0-5 scale factor.
                self._apply_gegl_filter(image, drawable, "gegl:unsharp-mask", {
                    "std-dev":   radius,
                    "scale":     amount / 100.0,
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
                # plug-in-gauss was removed in GIMP 3; gegl:gaussian-blur takes
                # a standard deviation per axis, in pixels.
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
            mode = MODE_MAP.get(str(mode_str).lower())
            if mode is None:
                return {"status": "error",
                        "error": f"unknown desaturate mode '{mode_str}' "
                                 f"(supported: {', '.join(MODE_MAP)})"}
            image.undo_group_start()
            try:
                pdb = Gimp.get_pdb()
                proc = pdb.lookup_procedure("gimp-drawable-desaturate")
                if proc is None:
                    raise RuntimeError(
                        "PDB procedure 'gimp-drawable-desaturate' is not available in this GIMP"
                    )
                cfg = proc.create_config()
                cfg.set_property("drawable", drawable)
                cfg.set_property("desaturate-mode", mode)
                self._check_run(proc.run(cfg), "gimp-drawable-desaturate")
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
                pdb = Gimp.get_pdb()
                proc = pdb.lookup_procedure("gimp-drawable-invert")
                if proc is None:
                    raise RuntimeError(
                        "PDB procedure 'gimp-drawable-invert' is not available in this GIMP"
                    )
                cfg = proc.create_config()
                cfg.set_property("drawable", drawable)
                cfg.set_property("linear", False)
                self._check_run(proc.run(cfg), "gimp-drawable-invert")
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
            interp = self._interp_from_string(interpolation)
            image.undo_group_start()
            Gimp.context_push()
            try:
                Gimp.context_set_interpolation(interp)
                image.scale(width, height)
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "width": width, "height": height, "interpolation": interpolation}}
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
            interp = self._interp_from_string(interpolation)
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
            if new_w >= src_w and new_h >= src_h:
                # "fit within" must never enlarge an image that already fits.
                Gimp.displays_flush()
                return {"status": "success",
                        "results": {"status": "success", "width": src_w, "height": src_h,
                                    "unchanged": True}}
            image.undo_group_start()
            Gimp.context_push()
            try:
                Gimp.context_set_interpolation(interp)
                image.scale(new_w, new_h)
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "width": new_w, "height": new_h, "interpolation": interpolation}}
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
                    pdb = Gimp.get_pdb()
                    proc = pdb.lookup_procedure("gimp-image-autocrop")
                    if proc is None:
                        raise RuntimeError("PDB procedure 'gimp-image-autocrop' not available")
                    cfg = proc.create_config()
                    cfg.set_property("image", image)
                    self._check_run(proc.run(cfg), "gimp-image-autocrop")
                    result = {"status": "success",
                              "width": image.get_width(), "height": image.get_height()}
                else:
                    _ok, non_empty, x1, y1, x2, y2 = Gimp.Selection.bounds(image)
                    if not non_empty:
                        raise RuntimeError(
                            "no selection to crop to; make a selection first "
                            "or pass autocrop=true"
                        )
                    crop_w, crop_h = x2 - x1, y2 - y1
                    image.crop(crop_w, crop_h, x1, y1)
                    Gimp.Selection.none(image)
                    result = {"status": "success", "x": x1, "y": y1,
                              "width": crop_w, "height": crop_h}
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": result}
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
                image.crop(width, height, x, y)
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "x": x, "y": y, "width": width, "height": height}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _rotate_image(self, params):
        """Rotate image by angle."""
        try:
            import math
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
                    -180.0: Gimp.RotationType.DEGREES180,
                    -270.0: Gimp.RotationType.DEGREES90,
                }
                if angle in rot_map:
                    image.rotate(rot_map[angle])
                else:
                    # gimp-item-transform-rotate takes DEGREES, not radians. Rotate
                    # every layer about the image centre, then grow the canvas to the
                    # rotated bounding box so no corner is cropped. Flattening here
                    # would destroy the layer stack for a geometric edit.
                    w = image.get_width()
                    h = image.get_height()
                    cx, cy = w / 2.0, h / 2.0
                    rad = math.radians(angle)
                    cos_a, sin_a = abs(math.cos(rad)), abs(math.sin(rad))
                    new_w = max(1, int(round(w * cos_a + h * sin_a)))
                    new_h = max(1, int(round(w * sin_a + h * cos_a)))
                    pdb = Gimp.get_pdb()
                    proc = pdb.lookup_procedure("gimp-item-transform-rotate")
                    if proc is None:
                        raise RuntimeError("PDB procedure 'gimp-item-transform-rotate' not available")
                    for layer in image.get_layers():
                        if not layer.has_alpha():
                            layer.add_alpha()
                        cfg = proc.create_config()
                        cfg.set_property("item", layer)
                        cfg.set_property("angle", angle)
                        cfg.set_property("auto-center", False)
                        cfg.set_property("center-x", cx)
                        cfg.set_property("center-y", cy)
                        self._check_run(proc.run(cfg), "gimp-item-transform-rotate")
                    if (new_w, new_h) != (w, h):
                        image.resize(new_w, new_h, int((w - new_w) / 2), int((h - new_h) / 2))
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success",
                    "results": {"status": "success", "angle": angle,
                                "width": image.get_width(), "height": image.get_height()}}
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
                image.flip(orient)
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
            if anchor not in anchor_offsets:
                return {"status": "error",
                        "error": f"unknown anchor '{anchor}' "
                                 f"(supported: {', '.join(anchor_offsets)})"}
            off_x, off_y = anchor_offsets[anchor]
            image.undo_group_start()
            try:
                image.resize(new_w, new_h, off_x, off_y)
                if fill.lower() != "transparent":
                    # Fill the newly exposed canvas area by adding a coloured
                    # layer underneath everything — flattening here would
                    # destroy the layer stack as a side effect of a resize.
                    Gimp.context_push()
                    try:
                        bg = self._color(fill)
                        base_type = image.get_base_type()
                        layer_type = (Gimp.ImageType.RGB_IMAGE
                                      if base_type == Gimp.ImageBaseType.RGB
                                      else Gimp.ImageType.GRAY_IMAGE)
                        bottom = Gimp.Layer.new(image, "Canvas Background",
                                                new_w, new_h, layer_type, 100,
                                                Gimp.LayerMode.NORMAL)
                        image.insert_layer(bottom, None, len(image.get_layers()))
                        Gimp.context_set_background(bg)
                        Gimp.Drawable.edit_fill(bottom, Gimp.FillType.BACKGROUND)
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
                self._check_run(proc.run(cfg), "gimp-image-select-color")
            finally:
                Gimp.context_pop()
            _ok, non_empty, x1, y1, x2, y2 = Gimp.Selection.bounds(image)
            if not non_empty:
                # A "success" with an empty selection makes every later fill/crop
                # a silent no-op, so report it as an error instead.
                return {
                    "status": "error",
                    "error": (f"select_by_color matched no pixels for '{color_str}' "
                              f"at threshold {threshold}; raise the threshold or "
                              f"pick a colour actually present"),
                }
            Gimp.displays_flush()
            return {"status": "success",
                    "results": {"status": "success", "x": x1, "y": y1,
                                "width": x2 - x1, "height": y2 - y1}}
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
            # Border/grow/shrink of a selection touching the canvas edge can empty
            # it; report the real result instead of just "success".
            _ok, non_empty, x1, y1, x2, y2 = Gimp.Selection.bounds(image)
            return {"status": "success",
                    "results": {"status": "success", "has_selection": bool(non_empty),
                                "x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 5 — Layer Operations
    # =========================================================================

    BLEND_MODES = {
        "NORMAL":      "NORMAL",
        "MULTIPLY":    "MULTIPLY",
        "SCREEN":      "SCREEN",
        "OVERLAY":     "OVERLAY",
        "DARKEN":      "DARKEN_ONLY",
        "LIGHTEN":     "LIGHTEN_ONLY",
        "DODGE":       "DODGE",
        "BURN":        "BURN",
        "HARD_LIGHT":  "HARDLIGHT",
        "SOFT_LIGHT":  "SOFTLIGHT",
        "DIFFERENCE":  "DIFFERENCE",
        "HUE":         "HSV_HUE",
        "SATURATION":  "HSV_SATURATION",
        "COLOR":       "HSL_COLOR",
        "LUMINOSITY":  "HSV_VALUE",
        "DISSOLVE":    "DISSOLVE",
    }

    def _blend_mode_from_string(self, mode_str):
        """Map blend mode name string to Gimp.LayerMode.

        An unrecognised name raises: quietly painting in NORMAL when the caller
        asked for GRAIN_EXTRACT is worse than an error they can fix.
        """
        attr = self.BLEND_MODES.get(str(mode_str).upper())
        if attr is None or not hasattr(Gimp.LayerMode, attr):
            raise RuntimeError(
                f"unknown blend_mode '{mode_str}' "
                f"(supported: {', '.join(sorted(self.BLEND_MODES))})"
            )
        return getattr(Gimp.LayerMode, attr)

    def _create_layer(self, params):
        """Create and insert a new layer."""
        try:
            image_index = int(params.get("image_index", 0))
            name        = params.get("name", "New Layer")
            opacity     = float(params.get("opacity", 100))
            blend_mode  = params.get("blend_mode", "NORMAL")
            position    = int(params.get("position", -1))
            fill        = params.get("fill", "transparent")
            image = self._get_image(image_index)
            width  = int(params.get("width")  or image.get_width())
            height = int(params.get("height") or image.get_height())
            mode   = self._blend_mode_from_string(blend_mode)
            # Determine layer type
            base_type  = image.get_base_type()
            layer_type = Gimp.ImageType.RGBA_IMAGE if base_type == Gimp.ImageBaseType.RGB else Gimp.ImageType.GRAYA_IMAGE
            image.undo_group_start()
            try:
                layer = Gimp.Layer.new(image, name, width, height, layer_type, opacity, mode)
                image.insert_layer(layer, None, position)
                Gimp.context_push()
                try:
                    if fill.lower() == "transparent":
                        layer.add_alpha()
                        Gimp.Drawable.edit_fill(layer, Gimp.FillType.TRANSPARENT)
                    else:
                        bg = self._color(fill)
                        Gimp.context_set_background(bg)
                        Gimp.Drawable.edit_fill(layer, Gimp.FillType.BACKGROUND)
                finally:
                    Gimp.context_pop()
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            return {
                "status": "success",
                "results": {
                    "layer_name": layer.get_name(),
                    "layer_id":   layer.get_id(),
                    "width":      width,
                    "height":     height,
                    "position":   position,
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
            if layer_index is not None:
                layer_index = int(layer_index)
            image = self._get_image(image_index)
            layer = self._resolve_layer(image, layer_name, layer_index)
            image.undo_group_start()
            try:
                if opacity is not None:
                    layer.set_opacity(float(opacity))
                if blend_mode is not None:
                    layer.set_mode(self._blend_mode_from_string(blend_mode))
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
            image.undo_group_start()
            try:
                image.reorder_item(layer, None, new_position)
            finally:
                image.undo_group_end()
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
                image.flatten()
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
                merged = image.merge_visible_layers(Gimp.MergeType.CLIP_TO_IMAGE)
            finally:
                image.undo_group_end()
            if merged is None:
                # GIMP returns NULL when there is nothing to merge; say so rather
                # than crashing on the None or claiming a layer that does not exist.
                return {"status": "error",
                        "error": "nothing to merge — the image has a single visible layer"}
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
                        "blend_mode": layer.get_mode().value_nick.upper(),
                        "width":      layer.get_width(),
                        "height":     layer.get_height(),
                        "has_alpha":  layer.has_alpha(),
                        "offsets":    [layer.get_offsets().offset_x,
                                       layer.get_offsets().offset_y],
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
            image.undo_group_start()
            Gimp.context_push()
            try:
                Gimp.Selection.all(image)
                if str(color_str).strip().lower() == "transparent":
                    if not drawable.has_alpha():
                        drawable.add_alpha()
                    Gimp.Drawable.edit_fill(drawable, Gimp.FillType.TRANSPARENT)
                else:
                    fg = self._color(color_str)
                    Gimp.context_set_foreground(fg)
                    Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND)
                Gimp.Selection.none(image)
            finally:
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
            fill_type   = (params.get("fill_type") or "").lower()
            color_str   = params.get("color")
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            _ok, non_empty, _x1, _y1, _x2, _y2 = Gimp.Selection.bounds(image)
            if not non_empty:
                return {"status": "error",
                        "error": "no selection to fill; make a selection first "
                                 "(or use fill_layer to paint the whole layer)"}
            if not fill_type and color_str is None:
                fill_type = "foreground"
            image.undo_group_start()
            Gimp.context_push()
            try:
                if fill_type == "transparent":
                    # Ensure layer has alpha channel before deleting pixels
                    if not drawable.has_alpha():
                        drawable.add_alpha()
                    Gimp.Drawable.edit_clear(drawable)
                elif color_str is not None:
                    fg = self._color(color_str)
                    Gimp.context_set_foreground(fg)
                    Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND)
                elif fill_type == "background":
                    Gimp.Drawable.edit_fill(drawable, Gimp.FillType.BACKGROUND)
                elif fill_type == "pattern":
                    Gimp.Drawable.edit_fill(drawable, Gimp.FillType.PATTERN)
                elif fill_type == "foreground":
                    Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND)
                else:
                    raise RuntimeError(
                        f"unknown fill_type '{fill_type}' "
                        "(use foreground, background, pattern or transparent)")
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
            fg_str = params.get("foreground", None)
            bg_str = params.get("background", None)
            if fg_str is not None:
                Gimp.context_set_foreground(self._color(fg_str))
            if bg_str is not None:
                Gimp.context_set_background(self._color(bg_str))
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
            image.undo_group_start()
            Gimp.context_push()
            try:
                if color_str:
                    Gimp.context_set_foreground(self._color(color_str))
                Gimp.context_set_brush_size(line_width)
                Gimp.context_set_opacity(100.0)
                coords = [x1, y1, x2, y2]
                if tool == "paintbrush":
                    Gimp.paintbrush_default(drawable, coords)
                else:
                    Gimp.pencil(drawable, coords)
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
            image.undo_group_start()
            Gimp.context_push()
            try:
                if color_str:
                    Gimp.context_set_foreground(self._color(color_str))
                Gimp.context_set_stroke_method(Gimp.StrokeMethod.LINE)
                Gimp.context_set_line_width(line_width)
                Gimp.context_set_opacity(100.0)
                image.select_rectangle(Gimp.ChannelOps.REPLACE, x, y, width, height)
                pdb = Gimp.get_pdb()
                proc = pdb.lookup_procedure("gimp-drawable-edit-stroke-selection")
                if proc is None:
                    raise RuntimeError(
                        "PDB procedure 'gimp-drawable-edit-stroke-selection' is not available in this GIMP"
                    )
                cfg = proc.create_config()
                cfg.set_property("drawable", drawable)
                self._check_run(proc.run(cfg), "gimp-drawable-edit-stroke-selection")
                Gimp.Selection.none(image)
            finally:
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
            image.undo_group_start()
            Gimp.context_push()
            try:
                if color_str:
                    Gimp.context_set_foreground(self._color(color_str))
                Gimp.context_set_stroke_method(Gimp.StrokeMethod.LINE)
                Gimp.context_set_line_width(line_width)
                Gimp.context_set_opacity(100.0)
                image.select_ellipse(Gimp.ChannelOps.REPLACE, x, y, width, height)
                pdb = Gimp.get_pdb()
                proc = pdb.lookup_procedure("gimp-drawable-edit-stroke-selection")
                if proc is None:
                    raise RuntimeError(
                        "PDB procedure 'gimp-drawable-edit-stroke-selection' is not "
                        "available in this GIMP"
                    )
                cfg = proc.create_config()
                cfg.set_property("drawable", drawable)
                self._check_run(proc.run(cfg), "gimp-drawable-edit-stroke-selection")
                Gimp.Selection.none(image)
            finally:
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
            image.undo_group_start()
            Gimp.context_push()
            try:
                image.select_rectangle(Gimp.ChannelOps.REPLACE, x, y, width, height)
                Gimp.context_set_foreground(self._color(color_str))
                Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND)
                Gimp.Selection.none(image)
            finally:
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
            image.undo_group_start()
            Gimp.context_push()
            try:
                image.select_ellipse(Gimp.ChannelOps.REPLACE, x, y, width, height)
                Gimp.context_set_foreground(self._color(color_str))
                Gimp.Drawable.edit_fill(drawable, Gimp.FillType.FOREGROUND)
                Gimp.Selection.none(image)
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success"}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _gradient_fill(self, params):
        """Fill a layer (or its selection) with a two-colour gradient.

        GIMP 3.2 paints the gradient stored in the context, so build a
        throwaway two-stop gradient rather than depending on a built-in whose
        display name is translated (gimp-blend and gegl:linear-gradient are gone).
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

            def _coord(key, alt, default):
                # `or` would throw away an explicit 0 coordinate
                value = params.get(key)
                if value is None:
                    value = params.get(alt)
                return float(default if value is None else value)

            x1 = _coord("x1", "start_x", 0)
            y1 = _coord("y1", "start_y", 0)
            x2 = _coord("x2", "end_x", w)
            y2 = _coord("y2", "end_y", h)

            choices = [m for m in dir(Gimp.GradientType) if m[:1].isupper()]
            gtype = getattr(Gimp.GradientType, gradient_type.upper(), None)
            if gtype is None:
                return {"status": "error",
                        "error": f"unknown gradient_type '{gradient_type}' "
                                 f"(supported: {', '.join(sorted(c.lower() for c in choices))})"}

            proc = Gimp.get_pdb().lookup_procedure("gimp-drawable-edit-gradient-fill")
            if proc is None:
                raise RuntimeError("PDB procedure 'gimp-drawable-edit-gradient-fill' not available")

            gradient = Gimp.Gradient.new("MCP two-colour")
            if gradient is None:
                raise RuntimeError("could not create a temporary gradient")
            gradient.segment_set_left_color(0, self._color(color1))
            gradient.segment_set_right_color(0, self._color(color2))
            gradient.segment_set_left_pos(0, 0.0)
            gradient.segment_set_right_pos(0, 1.0)

            image.undo_group_start()
            Gimp.context_push()
            try:
                Gimp.context_set_gradient(gradient)
                cfg = proc.create_config()
                cfg.set_property("drawable",      drawable)
                cfg.set_property("gradient-type", gtype)
                cfg.set_property("offset",        0.0)
                cfg.set_property("supersample",   True)
                cfg.set_property("dither",        True)
                cfg.set_property("x1", x1)
                cfg.set_property("y1", y1)
                cfg.set_property("x2", x2)
                cfg.set_property("y2", y2)
                self._check_run(proc.run(cfg), "gimp-drawable-edit-gradient-fill")
            finally:
                Gimp.context_pop()
                image.undo_group_end()
            # GIMP keeps every created gradient in the global list, so a
            # long-running session would otherwise fill the gradient dialog.
            try:
                gradient.delete()
            except Exception:
                pass
            Gimp.displays_flush()
            return {"status": "success",
                    "results": {"status": "success", "gradient_type": gradient_type,
                                "from": [x1, y1], "to": [x2, y2]}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    # =========================================================================
    # CATEGORY 7 — Text
    # =========================================================================

    def _resolve_font(self, name):
        """Resolve a font name to a Gimp.Font."""
        if not (hasattr(Gimp, "Font") and hasattr(Gimp.Font, "get_by_name")):
            return None

        font_obj = Gimp.Font.get_by_name(name)
        if font_obj is not None:
            return font_obj

        # GIMP 3.2 dropped GIMP 2.x aliases; e.g. "Sans" no longer resolves,
        # the 3.2 equivalent is "Sans-serif".
        for alias in (name + "-serif", name + " Regular",
                      name.replace(" ", "-"),
                      "Sans-serif", "Serif", "Monospace"):
            font_obj = Gimp.Font.get_by_name(alias)
            if font_obj is not None:
                return font_obj

        raw = Gimp.fonts_get_list("")
        flist = list(raw[1]) if isinstance(raw, tuple) and len(raw) > 1 else list(raw)
        return flist[0] if flist else None

    def _create_text_layer_native(self, image, text, font_obj, size, x, y, color_str):
        """Create a text layer via Gimp.TextLayer.new."""
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
                ccfg.set_property("color", self._color(color_str))
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
        proc.run(cfg)

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
            font        = params.get("font", "Sans")
            size        = int(params.get("size", 24))
            color_str   = params.get("color", "black")

            image      = self._get_image(image_index)
            before_ids = {lyr.get_id() for lyr in image.get_layers()}
            text_layer = None

            image.undo_group_start()
            Gimp.context_push()
            try:
                Gimp.context_set_foreground(self._color(color_str))
                font_obj = self._resolve_font(font)
                if font_obj is not None:
                    text_layer = self._create_text_layer_native(
                        image, text_str, font_obj, size, x, y, color_str)
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

            return {
                "status": "success",
                "results": {
                    "layer_name":  text_layer.get_name(),
                    "layer_id":    text_layer.get_id(),
                    "text_width":  text_layer.get_width(),
                    "text_height": text_layer.get_height(),
                    "position":    [x, y],
                }
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _edit_text(self, params):
        """Edit an existing text layer."""
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", "")
            new_text    = params.get("text", None)
            new_font    = params.get("font", None)
            new_size    = params.get("size", None)
            new_color   = params.get("color", None)
            image = self._get_image(image_index)
            layer = self._resolve_layer(image, layer_name, None)
            if not (isinstance(layer, Gimp.TextLayer) or
                    (hasattr(layer, "is_text_layer") and layer.is_text_layer())):
                # Bail out before mutating anything: a half-applied edit leaves a
                # text layer renamed to a value the caller can no longer address.
                return {"status": "error",
                        "error": f"layer '{layer.get_name()}' is not a text layer, "
                                 f"so it has no text/font/size to edit"}
            applied = []
            image.undo_group_start()
            try:
                if new_text is not None:
                    layer.set_text(new_text)
                    applied.append("text")
                if new_font is not None:
                    font_obj = self._resolve_font(new_font)
                    if font_obj is None:
                        raise RuntimeError(f"font '{new_font}' not found")
                    layer.set_font(font_obj)
                    applied.append("font")
                if new_size is not None:
                    layer.set_font_size(float(new_size), Gimp.Unit.pixel())
                    applied.append("size")
                if new_color is not None:
                    layer.set_color(self._color(new_color))
                    applied.append("color")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            size = layer.get_font_size()
            font = layer.get_font()
            return {"status": "success",
                    "results": {"status": "success", "applied": applied,
                                "layer_name": layer.get_name(),
                                "text": layer.get_text(),
                                # these two return Gimp objects / tuples — flatten
                                # them before json.dumps or the response is lost
                                "font": font.get_name() if hasattr(font, "get_name") else str(font),
                                "font_size": float(size[0] if isinstance(size, tuple) else size)}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _warp_region(self, params):
        """Warp / liquify a region of pixels by smudging them along a vector.

        GIMP 3.2 removed plug-in-iwarp and GEGL has no stroke-warp operation;
        the callable equivalent of a liquify "move" stroke is the smudge tool,
        driven with a soft round brush of the requested radius.

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
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            vectors     = params.get("vectors", [])
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            if not vectors:
                return {"status": "error", "error": "warp_region: no vectors given"}
            for i, v in enumerate(vectors):
                missing = [k for k in ("x", "y", "dx", "dy") if k not in v]
                if missing:
                    return {"status": "error",
                            "error": f"warp_region: vector {i} is missing {missing}"}

            applied = []
            image.undo_group_start()
            Gimp.context_push()
            try:
                Gimp.context_set_brush_hardness(0.5)
                Gimp.context_set_opacity(100.0)
                for v in vectors:
                    x      = float(v["x"])
                    y      = float(v["y"])
                    dx     = float(v["dx"])
                    dy     = float(v["dy"])
                    radius = float(v.get("radius", 40))
                    amount = float(v.get("amount", 0.3))
                    if dx == 0.0 and dy == 0.0:
                        continue
                    Gimp.context_set_brush_size(max(1.0, radius * 2.0))
                    Gimp.smudge(drawable, min(1.0, max(0.0, amount)),
                                [x, y, x + dx, y + dy])
                    applied.append({"from": [x, y], "to": [x + dx, y + dy],
                                    "radius": radius, "amount": amount})
            finally:
                Gimp.context_pop()
                image.undo_group_end()

            if not applied:
                return {"status": "error",
                        "error": "warp_region: every vector had a zero displacement"}
            Gimp.displays_flush()
            return {"status": "success",
                    "results": {"warped_vectors": len(applied), "vectors": applied}}
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
          4. Soften it with gegl:gaussian-blur
        """
        try:
            image_index = int(params.get("image_index", 0))
            layer_name  = params.get("layer_name", None)
            offset_x    = int(params.get("offset_x", 5))
            offset_y    = int(params.get("offset_y", 5))
            blur_radius = float(params.get("blur_radius", 10))
            color_str   = params.get("color", "black")
            opacity     = float(params.get("opacity", 60))
            image    = self._get_image(image_index)
            drawable = self._resolve_layer(image, layer_name, None)
            image.undo_group_start()
            try:
                # 1. Duplicate source layer to use as shadow base
                shadow_layer = drawable.copy()
                src_pos = image.get_item_position(drawable)
                image.insert_layer(shadow_layer, None, src_pos + 1)

                # 2. Fill shadow layer with shadow color, preserving alpha shape
                Gimp.context_set_foreground(self._color(color_str))
                shadow_layer.set_lock_alpha(True)
                Gimp.Drawable.edit_fill(shadow_layer, Gimp.FillType.FOREGROUND)
                shadow_layer.set_lock_alpha(False)

                # 3. Set opacity and offset
                shadow_layer.set_opacity(opacity)
                offs = shadow_layer.get_offsets()
                shadow_layer.set_offsets(offs.offset_x + offset_x, offs.offset_y + offset_y)

                # 4. Soften the shadow
                if blur_radius > 0:
                    self._apply_gegl_filter(image, shadow_layer, "gegl:gaussian-blur", {
                        "std-dev-x": blur_radius,
                        "std-dev-y": blur_radius,
                    })

                shadow_layer.set_name("Drop Shadow")
            finally:
                image.undo_group_end()
            Gimp.displays_flush()
            note = None
            if not drawable.has_alpha():
                # The shadow goes *under* the source layer; if that layer has no
                # transparency at all the shadow can never be seen.
                note = ("the shadow layer sits beneath an opaque layer with no alpha, "
                        "so it will not be visible — apply drop_shadow to a layer with "
                        "transparent areas (text, a cut-out shape)")
            return {"status": "success",
                    "results": {"status": "success", "shadow_visible": note is None,
                                "note": note}}
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
                    "type":      0,          # 0 = emboss, 1 = engrave, 2 = mipmap
                    "azimuth":   azimuth,
                    "elevation": elevation,
                    "depth":     depth,
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
            softness    = float(params.get("softness", 3.0))
            shape       = float(params.get("shape", 1.0))
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
                    "holdness":             1.0 - amount,
                    "value-distance":       amount,
                    "saturation-distance":  0.0,
                    "hue-distance":         0.0,
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
                    dup.scale(new_w, new_h)
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
                    dup.scale(new_w, new_h)

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
            if width is None and height is None and scale_factor is None:
                # Without this the tool silently "resized" every open image to
                # its current size and reported success.
                return {"status": "error",
                        "error": "batch_resize needs width, height or scale_factor"}
            if scale_factor is not None and float(scale_factor) <= 0:
                return {"status": "error",
                        "error": f"scale_factor must be > 0, got {scale_factor}"}
            if (width is not None and int(width) <= 0) or (height is not None and int(height) <= 0):
                return {"status": "error",
                        "error": "width and height must be > 0"}
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
                img.scale(new_w, new_h)
                results.append({"image_id": img.get_id(), "old_width": src_w, "old_height": src_h, "new_width": new_w, "new_height": new_h})
            Gimp.displays_flush()
            return {"status": "success", "results": {"results": results, "count": len(results)}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

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
            frame_w = frames[0].get_width()  if hasattr(frames[0], 'get_width')  else frames[0].get_width()
            frame_h = frames[0].get_height() if hasattr(frames[0], 'get_height') else frames[0].get_height()
            n = len(frames)
            cols = int(columns) if columns else max(1, math.ceil(math.sqrt(n)))
            rows = math.ceil(n / cols)

            sheet_w = cols * frame_w + (cols - 1) * padding
            sheet_h = rows * frame_h + (rows - 1) * padding

            sheet = Gimp.Image.new(sheet_w, sheet_h, Gimp.ImageBaseType.RGB)
            bg_layer = Gimp.Layer.new(sheet, "Background", sheet_w, sheet_h, Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)
            sheet.insert_layer(bg_layer, None, 0)
            bg_layer.add_alpha()
            Gimp.Drawable.edit_fill(bg_layer, Gimp.FillType.TRANSPARENT)

            for i, frame in enumerate(frames):
                col = i % cols
                row = i // cols
                dest_x = col * (frame_w + padding)
                dest_y = row * (frame_h + padding)
                if source == "images":
                    src_layers = frame.get_layers()
                    if not src_layers:
                        continue
                    src_drawable = src_layers[0]
                    frame.select_rectangle(Gimp.ChannelOps.REPLACE, 0, 0, frame.get_width(), frame.get_height())
                else:
                    src_drawable = frame
                    frame.get_image().select_rectangle(Gimp.ChannelOps.REPLACE, 0, 0, frame_w, frame_h)
                Gimp.edit_copy([src_drawable])
                pasted = Gimp.edit_paste(bg_layer, True)[0]
                pasted.set_offsets(dest_x, dest_y)
                Gimp.floating_sel_anchor(pasted)

            # flatten=False: a sprite sheet must keep its transparent gaps
            self._export_to_path(sheet, output_path, "png", 95, False)
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
                    dup.scale(scaled_w, scaled_h)
                    # Crop to exact target
                    crop_x = (scaled_w - target_w) // 2
                    crop_y = (scaled_h - target_h) // 2
                    dup.crop(target_w, target_h, crop_x, crop_y)
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
        """Raise a specific image's window to the front."""
        try:
            image_index = int(params.get("image_index", 0))
            image = self._get_image(image_index)
            display_id = self.display_of_image.get(image.get_id())
            if display_id is None:
                # GIMP 3.2 has no display-enumeration procedure, so we can only
                # raise windows this server opened.
                return {"status": "error",
                        "error": f"no window is tracked for image_index {image_index} "
                                 f"(opened outside this server); every other tool takes "
                                 f"image_index, so you do not need to raise it"}
            display = Gimp.Display.get_by_id(display_id)
            if display is None:
                self.display_of_image.pop(image.get_id(), None)
                return {"status": "error", "error": "the window for that image is gone"}
            display.present()
            Gimp.displays_flush()
            return {"status": "success", "results": {"status": "success", "image_id": image.get_id()}}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    _UNDO_UNSUPPORTED = (
        "GIMP 3.2 exposes no undo/redo procedure to plug-ins "
        "(gimp-image-undo was removed; only undo-group start/end exist). "
        "Revert by editing from a duplicate/save, not by undoing."
    )

    def _undo(self, params):
        """Undo N steps."""
        try:
            image_index = int(params.get("image_index", 0))
            self._get_image(image_index)
            return {"status": "error", "error": self._UNDO_UNSUPPORTED}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

    def _redo(self, params):
        """Redo N steps."""
        try:
            image_index = int(params.get("image_index", 0))
            self._get_image(image_index)
            return {"status": "error", "error": self._UNDO_UNSUPPORTED}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

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
                        Gimp.ConvertDitherType.NONE,
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
                pdb = Gimp.get_pdb()
                proc = pdb.lookup_procedure("gimp-xcf-save")
                if proc is None:
                    raise RuntimeError("PDB procedure 'gimp-xcf-save' not available")
                cfg = proc.create_config()
                cfg.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
                cfg.set_property("image", image)
                cfg.set_property("file", gio_file)
                self._check_run(proc.run(cfg), "gimp-xcf-save")
            # Deleting the image closes its windows; GIMP 3.2 gives plug-ins no
            # display list to walk.
            self.display_of_image.pop(image.get_id(), None)
            image.delete()
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
            if not (0 <= x < drawable.get_width() and 0 <= y < drawable.get_height()):
                raise RuntimeError(
                    f"({x}, {y}) is outside layer '{drawable.get_name()}' "
                    f"({drawable.get_width()}x{drawable.get_height()} at offset "
                    f"{drawable.get_offsets().offset_x},{drawable.get_offsets().offset_y})"
                )
            pixel = drawable.get_pixel(x, y)
            if pixel is None:
                raise RuntimeError(f"could not read pixel ({x}, {y}) of layer '{drawable.get_name()}'")

            def _byte(frac):
                # get_pixel hands back the *linear* float; report what a viewer or
                # PNG export shows, i.e. the sRGB-encoded byte value.
                v = min(1.0, max(0.0, float(frac)))
                s = 12.92 * v if v <= 0.0031308 else 1.055 * (v ** (1.0 / 2.4)) - 0.055
                return int(round(s * 255.0))

            # GIMP 3.2: get_pixel returns a Gegl.Color object
            if hasattr(pixel, 'get_rgba'):
                rf, gf, bf, af = pixel.get_rgba()
                r, g, b, a = _byte(rf), _byte(gf), _byte(bf), int(round(min(1.0, max(0.0, float(af))) * 255))
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
                name.lower(): getattr(Gimp.HistogramChannel, name)
                for name in ("VALUE", "RED", "GREEN", "BLUE", "ALPHA", "HUE",
                             "SATURATION", "LUMINANCE", "CYAN", "MAGENTA", "YELLOW")
                if hasattr(Gimp.HistogramChannel, name)
            }
            image_index = int(params.get("image_index", 0))
            channel_str = params.get("channel", "value")
            image    = self._get_image(image_index)
            drawable = (image.get_selected_layers() or image.get_layers() or [None])[0]
            channel = CHANNEL_MAP.get(str(channel_str).lower())
            if channel is None:
                # Silently falling back to VALUE would report the wrong channel
                # as if it were the one that was asked for.
                return {"status": "error",
                        "error": f"unknown histogram channel '{channel_str}' "
                                 f"(available: {sorted(CHANNEL_MAP)})"}
            pdb = Gimp.get_pdb()
            proc = pdb.lookup_procedure("gimp-drawable-histogram")
            if proc:
                cfg = proc.create_config()
                cfg.set_property("drawable",    drawable)
                cfg.set_property("channel",     channel)
                cfg.set_property("start-range", 0.0)
                cfg.set_property("end-range",   1.0)
                result = proc.run(cfg)
                self._check_run(result, "gimp-drawable-histogram")
                # index(0) is the PDB status; the values follow as
                # mean, std-dev, median, pixels, count, percentile.
                def _v(idx):
                    try:
                        return result.index(idx)
                    except Exception:
                        return 0
                return {
                    "status": "success",
                    "results": {
                        "mean":    _v(1),
                        "std_dev": _v(2),
                        "median":  _v(3),
                        "pixels":  _v(4),
                        "count":   _v(5),
                    }
                }
            else:
                return {"status": "error", "error": "gimp-drawable-histogram not available"}
        except Exception as e:
            return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


Gimp.main(MCPPlugin.__gtype__, sys.argv)
