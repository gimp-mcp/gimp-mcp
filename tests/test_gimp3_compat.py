#!/usr/bin/env python3
"""In-process regression test for the fill_selection safety guard.

Problem found against GIMP 3.2.6: with no active selection, GIMP applies
edit_clear() / edit_fill() to the ENTIRE layer. _fill_selection used to
report success while destroying the image content on a leftover empty
selection; it must now reject an empty selection with an error instead of
touching the layer.

Stubs gi.repository (GIMP 3 shaped — the plugin requires
gi.require_version('Gimp', '3.0')) so the plugin imports without a running
GIMP, then calls the handler directly. Exits 0 on pass, 1 on fail — no
GIMP needed.
"""
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, os.pardir, "gimp-mcp-plugin.py")


class FakeLayer:
    def __init__(self, name="layer"):
        self._name = name

    def get_name(self):
        return self._name

    def has_alpha(self):
        return True

    def add_alpha(self):
        pass


class FakeImage3:
    """GIMP 3 shaped image."""

    def __init__(self, ident=1):
        self._id = ident
        self._layer = FakeLayer()
        self.undo_groups = 0

    def get_id(self):
        return self._id

    def get_layers(self):
        return [self._layer]

    def get_selected_layers(self):
        return [self._layer]

    def undo_group_start(self):
        self.undo_groups += 1

    def undo_group_end(self):
        self.undo_groups -= 1

def install_gi_stub(images, state):
    """Importable gi shaped like GIMP 3.

    images: list returned by Gimp.get_images().
    state: dict with "bounds" (the Gimp.Selection.bounds() return value)
           and "calls" (fill op names performed on a layer).
    """
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **k: None
    repository = types.ModuleType("gi.repository")

    class Gimp:
        class PlugIn:
            # MCPPlugin.__gtype__ is read at module import (Gimp.main call).
            __gtype__ = "stub-gtype"

        class RunMode:
            NONINTERACTIVE = 1
            INTERACTIVE = 0

        class ImageBaseType:
            RGB, GRAY, INDEXED = "RGB", "Gray", "Indexed"

        class ChannelOps:
            REPLACE, ADD, SUBTRACT, INTERSECT = range(4)

        class LayerMode:
            NORMAL = 0

        class FillType:
            FOREGROUND, BACKGROUND, PATTERN = range(3)

        class PDBStatusType:
            SUCCESS = 0

        class Selection:
            @staticmethod
            def bounds(image):
                return state["bounds"]

            @staticmethod
            def all(image):
                pass

            @staticmethod
            def none(image):
                pass

        class Drawable:
            @staticmethod
            def edit_clear(d):
                state["calls"].append("edit_clear")
                return True

            @staticmethod
            def edit_fill(d, fill_type):
                state["calls"].append("edit_fill")
                return True

        @staticmethod
        def get_images():
            return images

        @staticmethod
        def displays_flush():
            pass

        @staticmethod
        def context_push():
            pass

        @staticmethod
        def context_pop():
            pass

        @staticmethod
        def context_set_opacity(v):
            pass

        @staticmethod
        def context_set_paint_mode(m):
            pass

        @staticmethod
        def context_set_foreground(c):
            pass

        @staticmethod
        def main(gtype, argv):
            pass

    class GLib:
        class Error(Exception):
            pass

        @staticmethod
        def dtext(domain, msg):
            return msg

    GLib.dgettext = GLib.dtext

    class GObject:
        class ParamFlags:
            READWRITE = 0

    class Gegl:
        class Color:
            @staticmethod
            def new(s):
                return ("color", s)

    repository.Gimp, repository.GLib, repository.GObject, repository.Gegl = \
        Gimp, GLib, GObject, Gegl
    gi.repository = repository
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repository


def load_plugin():
    spec = importlib.util.spec_from_file_location("gimp_mcp_plugin_compat", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    failures = []

    def check(name, cond, detail=""):
        if cond:
            print(f"  PASS {name}")
        else:
            failures.append(name)
            print(f"  FAIL {name}: {str(detail)[:100]}")

    state = {"bounds": (True, False, 0, 0, 0, 0), "calls": []}
    img = FakeImage3()
    install_gi_stub([img], state)
    p = load_plugin().MCPPlugin()

    # fill_selection with no selection must be refused, and must not touch
    # the layer (GIMP would otherwise clear/fill the entire layer).
    r = p._fill_selection({"image_index": 0, "fill_type": "transparent"})
    check("fill_selection_no_selection_refused",
          r.get("status") == "error" and "selection" in r.get("error", ""), r)
    check("fill_selection_no_selection_no_layer_access",
          state["calls"] == [] and img.undo_groups == 0,
          {"calls": state["calls"], "undo_groups": img.undo_groups})
    r = p._fill_selection({"image_index": 0, "fill_type": "foreground", "color": "#ff0000"})
    check("fill_selection_color_no_selection_refused",
          r.get("status") == "error" and state["calls"] == [], r)

    # With a selection the fill proceeds as before.
    state["bounds"] = (True, True, 5, 5, 20, 20)
    r = p._fill_selection({"image_index": 0, "fill_type": "transparent"})
    check("fill_selection_with_selection_succeeds",
          r.get("status") == "success" and state["calls"] == ["edit_clear"]
          and img.undo_groups == 0, r)

    if failures:
        print(f"FAIL: test_gimp3_compat — {len(failures)} failure(s): {failures}")
        return 1
    print("PASS: test_gimp3_compat — fill_selection refuses empty selections")
    return 0


if __name__ == "__main__":
    sys.exit(main())
