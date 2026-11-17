#!/usr/bin/env python3
"""Tests for font resolution and substitution reporting in add_text / edit_text (#124).

- _resolve_font strips surrounding whitespace, treats empty names as the default
  font without warning, treats exact and GIMP 2.x alias matches as matches
  (substituted=False), and treats step-3 fallbacks ("Sans-serif", "Serif",
  "Monospace", or the first installed font) as substitutions (substituted=True).
- add_text defaults to "Sans-serif" (so omitting `font` returns `font` only,
  without `requested_font` or `warning`).
- add_text and edit_text report the applied font in `font`, include
  `requested_font` when the stripped non-empty request differs from `font`, and
  add `warning` only when step 3 substituted a fallback font.
- edit_text omits font fields when `font` was not passed.
- Tool descriptions in gimp_mcp_server.py document unknown-font fallback and
  point to list_fonts.

No GIMP needed: stubs gi.repository.
Run with: uv run tests/test_font_substitution.py
Exits 0 on pass, 1 on fail.
"""
import contextlib
import importlib.util
import inspect
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
PLUGIN = os.path.join(ROOT, "gimp-mcp-plugin.py")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_MISSING = object()


class FakeFont:
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


class Status:
    def __init__(self, nick):
        self.value_nick = nick


SUCCESS = Status("success")
EXECUTION_ERROR = Status("execution-error")

INSTALLED_FONTS = {}   # name -> FakeFont
FONTS_LIST_RAW = None  # optional override for Gimp.fonts_get_list("")
NATIVE_TEXT_OK = True  # False forces _add_text onto the gimp-text-font PDB path
PDB_PROCS = {}         # proc_name -> (status, err_msg)
PDB_CONFIGS = []       # [(proc_name, dict_of_props), ...]


class FakeTextLayer:
    _next_id = 10

    def __init__(self, name, font_obj, width=80, height=24):
        FakeTextLayer._next_id += 1
        self._id = FakeTextLayer._next_id
        self._name = name
        self.font = font_obj
        self._width = width
        self._height = height
        self.offsets = (0, 0)

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name

    def get_width(self):
        return self._width

    def get_height(self):
        return self._height

    def get_font(self):
        return self.font

    def set_offsets(self, x, y):
        self.offsets = (x, y)
        return True


class FakeImage:
    def __init__(self):
        self.layers = []
        self.undo_depth = 0
        self.undo_groups_started = 0

    def get_layers(self):
        return list(self.layers)

    def insert_layer(self, layer, parent, position):
        self.layers.insert(position, layer)
        return True

    def undo_group_start(self):
        self.undo_depth += 1
        self.undo_groups_started += 1

    def undo_group_end(self):
        self.undo_depth -= 1


class FakeValueArray:
    def __init__(self, *values):
        self._values = values

    def index(self, i):
        return self._values[i]

    def length(self):
        return len(self._values)


class FakeProc:
    def __init__(self, name, status, message=None, image=None):
        self._name = name
        self._status = status
        self._message = message
        self._image = image
        self._props = {}

    def get_name(self):
        return self._name

    def create_config(self):
        self._props = {}
        return types.SimpleNamespace(set_property=self._props.__setitem__)

    def run(self, cfg):
        PDB_CONFIGS.append((self._name, dict(self._props)))
        if self._status is SUCCESS:
            if self._name == "gimp-text-font" and self._image is not None:
                tl = FakeTextLayer(
                    self._props.get("text", "text"),
                    self._props.get("font"),
                )
                self._image.insert_layer(tl, None, 0)
            elif self._name == "gimp-text-layer-set-font":
                layer = self._props.get("layer")
                if layer is not None:
                    layer.font = self._props.get("font")
        if self._message is None:
            return FakeValueArray(self._status)
        return FakeValueArray(self._status, self._message)


OPEN_IMAGE = FakeImage()


def install_gi_stub():
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **k: None
    repository = types.ModuleType("gi.repository")

    class FakePDB:
        @staticmethod
        def get_last_error():
            return None

        @staticmethod
        def lookup_procedure(name):
            if name not in PDB_PROCS:
                return None
            status, msg = PDB_PROCS[name]
            return FakeProc(name, status, msg, image=OPEN_IMAGE)

    class Gimp:
        class PlugIn:
            __gtype__ = "stub-gtype"

        class RunMode:
            INTERACTIVE, NONINTERACTIVE = 0, 1

        class PDBStatusType:
            pass

        class Unit:
            @staticmethod
            def pixel():
                return "px"

        class Font:
            @staticmethod
            def get_by_name(name):
                return INSTALLED_FONTS.get(name)

        class TextLayer:
            @staticmethod
            def new(image, text, font_obj, size, unit):
                if not NATIVE_TEXT_OK:
                    return None
                return FakeTextLayer(text or "Text", font_obj)

        @staticmethod
        def fonts_get_list(filt):
            if FONTS_LIST_RAW is not None:
                return FONTS_LIST_RAW
            return list(INSTALLED_FONTS.values())

        @staticmethod
        def get_pdb():
            return FakePDB()

        @staticmethod
        def get_images():
            return [OPEN_IMAGE]

        @staticmethod
        def context_push():
            pass

        @staticmethod
        def context_pop():
            pass

        @staticmethod
        def context_set_foreground(color):
            pass

        @staticmethod
        def displays_flush():
            pass

        @staticmethod
        def main(gtype, argv):
            pass

    Gimp.PDBStatusType.SUCCESS = SUCCESS
    Gimp.PDBStatusType.EXECUTION_ERROR = EXECUTION_ERROR

    class GLib:
        class Error(Exception):
            pass

        @staticmethod
        def dgettext(domain, msg):
            return msg

    class GObject:
        class ParamFlags:
            READWRITE = 0

    repository.Gimp = Gimp
    repository.GLib = GLib
    repository.GObject = GObject
    repository.Gegl = types.SimpleNamespace(Color=types.SimpleNamespace(new=lambda s: s))
    gi.repository = repository
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repository


@contextlib.contextmanager
def stubbed_gi():
    saved_gi = sys.modules.get("gi", _MISSING)
    saved_repo = sys.modules.get("gi.repository", _MISSING)
    install_gi_stub()
    try:
        yield
    finally:
        for key, prev in (("gi", saved_gi), ("gi.repository", saved_repo)):
            if prev is _MISSING:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = prev


def load_plugin():
    spec = importlib.util.spec_from_file_location("gimp_mcp_plugin_font_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def set_fonts(*names, list_raw=None):
    global FONTS_LIST_RAW
    INSTALLED_FONTS.clear()
    for n in names:
        INSTALLED_FONTS[n] = FakeFont(n)
    FONTS_LIST_RAW = list_raw


def reset_scene(*font_names, native_ok=True):
    global NATIVE_TEXT_OK
    NATIVE_TEXT_OK = native_ok
    set_fonts(*font_names)
    OPEN_IMAGE.layers.clear()
    OPEN_IMAGE.undo_depth = 0
    OPEN_IMAGE.undo_groups_started = 0
    PDB_CONFIGS.clear()
    PDB_PROCS.clear()
    PDB_PROCS.update({
        "gimp-text-layer-set-color": (SUCCESS, None),
        "gimp-text-layer-set-text": (SUCCESS, None),
        "gimp-text-layer-set-font": (SUCCESS, None),
        "gimp-text-layer-set-font-size": (SUCCESS, None),
        "gimp-text-font": (SUCCESS, None),
    })


def main():
    global FONTS_LIST_RAW
    failures = []

    def check(name, condition, detail=""):
        status = "PASS" if condition else "FAIL"
        suffix = f" ({detail})" if detail and not condition else ""
        print(f"{status}: {name}{suffix}")
        if not condition:
            failures.append(name)

    with stubbed_gi():
        mod = load_plugin()
        plugin = mod.MCPPlugin()
        import gimp_mcp_server as server

        # ── 1. _resolve_font ──────────────────────────────────────────────────
        set_fonts("Sans-serif", "Serif", "Monospace", "DejaVu Sans Regular")

        f, sub = plugin._resolve_font("Serif")
        check("_resolve_font exact match returns substituted=False",
              f is not None and f.get_name() == "Serif" and sub is False, f"{f}, {sub}")

        f, sub = plugin._resolve_font("  Serif \t")
        check("_resolve_font strips surrounding whitespace before lookup",
              f is not None and f.get_name() == "Serif" and sub is False, f"{f}, {sub}")

        f, sub = plugin._resolve_font("Sans")
        check("_resolve_font 'Sans' -> 'Sans-serif' alias returns substituted=False",
              f is not None and f.get_name() == "Sans-serif" and sub is False, f"{f}, {sub}")

        f, sub = plugin._resolve_font("DejaVu Sans")
        check("_resolve_font '<name> Regular' alias returns substituted=False",
              f is not None and f.get_name() == "DejaVu Sans Regular" and sub is False,
              f"{f}, {sub}")

        f, sub = plugin._resolve_font("Sans serif")
        check("_resolve_font space-to-hyphen alias returns substituted=False",
              f is not None and f.get_name() == "Sans-serif" and sub is False, f"{f}, {sub}")

        f, sub = plugin._resolve_font("Comic Snas")
        check("_resolve_font unknown name falls back to 'Sans-serif' with substituted=True",
              f is not None and f.get_name() == "Sans-serif" and sub is True, f"{f}, {sub}")

        for blank in ("", "   "):
            f, sub = plugin._resolve_font(blank)
            check(f"_resolve_font {blank!r} selects default font with substituted=False",
                  f is not None and f.get_name() == "Sans-serif" and sub is False, f"{f}, {sub}")

        set_fonts("Monospace")
        f, sub = plugin._resolve_font("Comic Snas")
        check("_resolve_font falls through to 'Monospace' with substituted=True",
              f is not None and f.get_name() == "Monospace" and sub is True, f"{f}, {sub}")

        set_fonts("Cantarell")
        f, sub = plugin._resolve_font("Comic Snas")
        check("_resolve_font falls back to first installed font (list) with substituted=True",
              f is not None and f.get_name() == "Cantarell" and sub is True, f"{f}, {sub}")

        f, sub = plugin._resolve_font("")
        check("_resolve_font empty name uses first installed font with substituted=False",
              f is not None and f.get_name() == "Cantarell" and sub is False, f"{f}, {sub}")

        set_fonts()
        FONTS_LIST_RAW = (1, [FakeFont("TupleFont")])
        f, sub = plugin._resolve_font("Comic Snas")
        check("_resolve_font handles GIMP (count, fonts) tuple return with substituted=True",
              f is not None and f.get_name() == "TupleFont" and sub is True, f"{f}, {sub}")

        set_fonts()
        FONTS_LIST_RAW = (0, [])
        f, sub = plugin._resolve_font("Comic Snas")
        check("_resolve_font returns (None, False) when no fonts exist",
              f is None and sub is False, f"{f}, {sub}")

        f, sub = plugin._resolve_font("")
        check("_resolve_font empty name returns (None, False) when no fonts exist",
              f is None and sub is False, f"{f}, {sub}")

        # ── 2. _add_text ──────────────────────────────────────────────────────
        reset_scene("Sans-serif", "Serif", "Monospace")

        r = plugin._add_text({"text": "Default"})
        res = r.get("results", {})
        check("add_text omitted font defaults to 'Sans-serif' without requested_font or warning",
              r["status"] == "success"
              and res.get("font") == "Sans-serif"
              and "requested_font" not in res
              and "warning" not in res
              and OPEN_IMAGE.layers[0].get_font().get_name() == "Sans-serif",
              str(r))

        for blank in (None, "", "   "):
            r = plugin._add_text({"text": "Blank", "font": blank})
            res = r.get("results", {})
            check(f"add_text font={blank!r} uses default 'Sans-serif' without warning",
                  r["status"] == "success"
                  and res.get("font") == "Sans-serif"
                  and "requested_font" not in res
                  and "warning" not in res,
                  str(r))

        r = plugin._add_text({"text": "Exact", "font": "Serif", "x": 12, "y": 34})
        res = r.get("results", {})
        check("add_text exact match reports font without requested_font or warning",
              r["status"] == "success"
              and res.get("font") == "Serif"
              and "requested_font" not in res
              and "warning" not in res
              and OPEN_IMAGE.layers[0].get_font().get_name() == "Serif",
              str(r))

        r = plugin._add_text({"text": "Trimmed exact", "font": "Serif "})
        res = r.get("results", {})
        check("add_text 'Serif ' matches 'Serif' without requested_font or warning",
              r["status"] == "success"
              and res.get("font") == "Serif"
              and "requested_font" not in res
              and "warning" not in res,
              str(r))

        r = plugin._add_text({"text": "Explicit alias", "font": "Sans"})
        res = r.get("results", {})
        check("add_text explicit 'Sans' reports font='Sans-serif', requested_font='Sans', no warning",
              r["status"] == "success"
              and res.get("font") == "Sans-serif"
              and res.get("requested_font") == "Sans"
              and "warning" not in res,
              str(r))

        r = plugin._add_text({"text": "Typo", "font": "Comic Snas"})
        res = r.get("results", {})
        expected_warning = (
            "Font 'Comic Snas' not found; used 'Sans-serif'. See list_fonts for installed fonts."
        )
        check("add_text unknown font reports font, requested_font and warning",
              r["status"] == "success"
              and res.get("font") == "Sans-serif"
              and res.get("requested_font") == "Comic Snas"
              and res.get("warning") == expected_warning,
              str(r))

        r = plugin._add_text({"text": "Trimmed typo", "font": "  Comic Snas  "})
        res = r.get("results", {})
        check("add_text trims whitespace in requested_font and warning on unknown font",
              r["status"] == "success"
              and res.get("requested_font") == "Comic Snas"
              and res.get("warning") == expected_warning,
              str(r))

        # PDB fallback path in _add_text
        reset_scene("Sans-serif", "Serif", native_ok=False)
        r = plugin._add_text({"text": "PDB fallback", "font": "Comic Snas"})
        res = r.get("results", {})
        check("add_text via gimp-text-font PDB fallback still reports substitution warning",
              r["status"] == "success"
              and res.get("font") == "Sans-serif"
              and res.get("requested_font") == "Comic Snas"
              and res.get("warning") == expected_warning,
              str(r))

        # No fonts at all in _add_text
        reset_scene()
        r = plugin._add_text({"text": "No fonts", "font": "Comic Snas"})
        check("add_text returns an error without opening an undo group when GIMP has no fonts",
              r["status"] == "error"
              and "Comic Snas" in r.get("error", "")
              and OPEN_IMAGE.undo_groups_started == 0
              and not OPEN_IMAGE.layers,
              str(r))

        # Non-string font in _add_text
        reset_scene("Sans-serif")
        r = plugin._add_text({"text": "Bad font type", "font": 123})
        check("add_text rejects non-string font",
              r["status"] == "error" and "string" in r.get("error", ""), str(r))

        # ── 3. _edit_text ─────────────────────────────────────────────────────
        reset_scene("Sans-serif", "Serif", "Monospace")
        seed = plugin._add_text({"text": "Seed", "font": "Sans-serif"})
        layer_name = seed["results"]["layer_name"]
        layer_obj = OPEN_IMAGE.layers[0]

        r = plugin._edit_text({"layer_name": layer_name, "font": "Serif"})
        res = r.get("results", {})
        check("edit_text exact match reports font='Serif' without requested_font or warning",
              r["status"] == "success"
              and res == {"status": "success", "font": "Serif"}
              and layer_obj.get_font().get_name() == "Serif",
              str(r))

        r = plugin._edit_text({"layer_name": layer_name, "font": "Serif "})
        res = r.get("results", {})
        check("edit_text 'Serif ' matches 'Serif' without requested_font or warning",
              r["status"] == "success"
              and res == {"status": "success", "font": "Serif"}
              and layer_obj.get_font().get_name() == "Serif",
              str(r))

        for blank in ("", "   "):
            r = plugin._edit_text({"layer_name": layer_name, "font": blank})
            res = r.get("results", {})
            check(f"edit_text blank font {blank!r} leaves the font unchanged and adds no font fields",
                  r["status"] == "success"
                  and res == {"status": "success"}
                  and layer_obj.get_font().get_name() == "Serif",
                  str(r))

        r = plugin._edit_text({"layer_name": layer_name, "font": "Sans"})
        res = r.get("results", {})
        check("edit_text alias 'Sans' reports font='Sans-serif', requested_font='Sans', no warning",
              r["status"] == "success"
              and res == {"status": "success", "font": "Sans-serif", "requested_font": "Sans"}
              and layer_obj.get_font().get_name() == "Sans-serif",
              str(r))

        r = plugin._edit_text({"layer_name": layer_name, "font": "Comic Snas"})
        res = r.get("results", {})
        check("edit_text unknown font reports font, requested_font and warning",
              r["status"] == "success"
              and res == {
                  "status": "success",
                  "font": "Sans-serif",
                  "requested_font": "Comic Snas",
                  "warning": expected_warning,
              }
              and layer_obj.get_font().get_name() == "Sans-serif",
              str(r))

        r = plugin._edit_text({"layer_name": layer_name, "text": "Renamed only"})
        res = r.get("results", {})
        check("edit_text without font omits font, requested_font and warning",
              r["status"] == "success" and res == {"status": "success"},
              str(r))

        set_fonts()
        r = plugin._edit_text({"layer_name": layer_name, "font": "Comic Snas"})
        check("edit_text returns error when GIMP has no fonts",
              r["status"] == "error" and "Comic Snas" in r.get("error", ""), str(r))

        # ── 4. gimp_mcp_server wrappers & tool descriptions ───────────────────
        reset_scene("Sans-serif", "Serif")
        sig_default = inspect.signature(server.add_text).parameters["font"].default
        check("server.add_text default font parameter is 'Sans-serif'",
              sig_default == "Sans-serif", repr(sig_default))

        real_get_conn = server.get_gimp_connection

        class DirectConn:
            def send_command(self, command_type, params=None):
                if command_type == "add_text":
                    return plugin._add_text(params or {})
                if command_type == "edit_text":
                    return plugin._edit_text(params or {})
                raise AssertionError(f"unexpected command: {command_type}")

        server.get_gimp_connection = DirectConn
        try:
            default_added = server.add_text(None, text="Default via server")
            check("server.add_text with default font omits requested_font and warning",
                  default_added.get("font") == "Sans-serif"
                  and "requested_font" not in default_added
                  and "warning" not in default_added,
                  str(default_added))

            added = server.add_text(None, text="Via server", font="Comic Snas")
            check("server.add_text forwards font, requested_font and warning",
                  added.get("font") == "Sans-serif"
                  and added.get("requested_font") == "Comic Snas"
                  and added.get("warning") == expected_warning,
                  str(added))

            edited = server.edit_text(None, layer_name=added["layer_name"], font="Comic Snas")
            check("server.edit_text forwards font, requested_font and warning",
                  edited.get("font") == "Sans-serif"
                  and edited.get("requested_font") == "Comic Snas"
                  and edited.get("warning") == expected_warning,
                  str(edited))
        finally:
            server.get_gimp_connection = real_get_conn

        for tool_name in ("add_text", "edit_text"):
            desc = server.mcp._tool_manager.get_tool(tool_name).description or ""
            check(f"{tool_name} MCP description mentions list_fonts and warning",
                  "list_fonts" in desc and "warning" in desc and "requested_font" in desc,
                  desc)

    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(failures)} failure(s)")
    return 1 if failures else 0


def test_font_substitution():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
