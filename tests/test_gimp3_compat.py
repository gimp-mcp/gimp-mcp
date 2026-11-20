#!/usr/bin/env python3
"""In-process regression tests for GIMP 3 compatibility of fill safety and
window management.

The plugin requires Gimp 3.0 (gi.require_version('Gimp', '3.0') at the
top), so only GIMP 3 shaped stubs are needed.

Contracts tested:

1. fill_selection with no active selection must be refused: GIMP applies
   edit_clear()/edit_fill() with no selection to the ENTIRE layer, which
   silently destroys image content (#138).

2. fill_layer fills the whole layer and leaves the selection as it was:
   with a selection it keeps a copy, selects none (with no selection
   edit_fill() fills the whole layer, also outside the canvas, where select
   all would stop), fills, and puts the copy back with combine_masks() (also
   when the fill fails); with none it just fills. If the copy or the restore
   fails, the context and the undo group still close. The copy must never be added to
   the image: an added channel becomes the selected item, and once removed
   it leaves no layer selected, so later strokes in a displayed image fail
   with "There is no selection to stroke".

3. undo/redo are not exposed by GIMP 3's Python API (libgimp offers undo
   grouping only); they must return the clear "not supported" error (#41,
   real undo is tracked in #86), not an AttributeError traceback.

4. close_image and set_active_image reach display windows only through the
   windows the plugin itself opened (#41): GIMP 3 gives plugins no way to
   enumerate displays or map a display to its image. set_active_image must
   raise the tracked window, drop entries for windows the user closed, and
   return an error for windows opened elsewhere; close_image must delete
   the tracked window, skip deleting an image GIMP already deleted (the
   last window closing deletes the image), and report an error instead of
   success while a window it cannot close still shows the image.

The fakes model both of GIMP's display rules: an image cannot be deleted
while any display still shows it, and GIMP deletes the image when its last
display window closes.

Stubs gi.repository so the plugin imports without a running GIMP, then
calls the handlers directly. Exits 0 on pass, 1 on fail — no GIMP needed.
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


# Live display windows per image id, mirroring GIMP's rules that an image
# cannot be deleted while any display still shows it, and that GIMP deletes
# the image when its last display window closes.
_LIVE = {"displays": {}}


class FakeDisplay3:
    """GIMP 3 shaped: no get_image, only validity / delete / present."""

    def __init__(self, image):
        self._image = image
        self.deleted = False
        self.presented = 0

    def is_valid(self):
        return not self.deleted

    def delete(self):
        self.deleted = True
        displays = _LIVE["displays"].get(self._image.get_id(), [])
        if self in displays:
            displays.remove(self)
        # GIMP deletes an image when its last display window closes.
        if not displays:
            self._image.deleted = True

    def present(self):
        self.presented += 1


class FakeChannel:
    _next_id = [500]

    def __init__(self):
        FakeChannel._next_id[0] += 1
        self._id = FakeChannel._next_id[0]

    def get_id(self):
        return self._id


class FakeSelectionCopy:
    """A selection copy that is never added to the image."""

    def __init__(self):
        self.deleted = False

    def delete(self):
        self.deleted = True
        _LIVE["state"]["ops"].append("delete_copy")


class FakeSelectionMask:
    def copy(self):
        st = _LIVE["state"]
        st["ops"].append("copy")
        if not st.get("copy_ok", True):
            return None
        st["copy"] = FakeSelectionCopy()
        return st["copy"]

    def combine_masks(self, other, op, x, y):
        st = _LIVE["state"]
        st["ops"].append("restore")
        st["restored_from"] = (other, op, x, y)
        if st.get("restore_raises"):
            raise RuntimeError("combine_masks failed")
        return True


class FakeImage3:
    """GIMP 3 shaped: no undo/redo, no display accessors."""

    def __init__(self, ident=1):
        self._id = ident
        self._layer = FakeLayer()
        self._channels = []
        self.deleted = False
        self.undo_groups = 0

    def get_id(self):
        return self._id

    def get_layers(self):
        return [self._layer]

    def get_selected_layers(self):
        return [self._layer]

    def get_channels(self):
        return self._channels

    def get_selection(self):
        return FakeSelectionMask()

    def undo_group_start(self):
        self.undo_groups += 1

    def undo_group_end(self):
        self.undo_groups -= 1

    def is_valid(self):
        # Instance method; the real API also has a static
        # Gimp.Image.id_is_valid(image_id) which cannot be called on an
        # instance ("takes exactly 1 argument (0 given)").
        return not self.deleted

    def delete(self):
        # GIMP: refuses while a display still shows the image, and a delete
        # for an ID that no longer exists raises gimp-image-delete.
        if self.deleted:
            raise RuntimeError("gimp-image-delete: invalid image id")
        if _LIVE["displays"].get(self._id):
            return
        self.deleted = True


def install_gi_stub(images, state):
    """Importable gi shaped like GIMP 3.

    images: list returned by Gimp.get_images().
    state: dict with "bounds" (the Gimp.Selection.bounds() return value),
           "calls" (fill op names performed on a drawable), and "ops"
           (Selection.all / none, fill ops, and the selection copy/restore,
           in the order they ran); "fill_ok" (default True) is what
           edit_fill returns. "saved_channel" / "sel_restored_from" are only
           set if a handler saves a selection into an image channel or
           restores one with select_item, which fill_layer must not do.
    """
    _LIVE["displays"] = {}
    _LIVE["state"] = state
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

        class Display:
            @staticmethod
            def new(image):
                d = FakeDisplay3(image)
                _LIVE["displays"].setdefault(image.get_id(), []).append(d)
                return d

        class Image:
            # GIMP 3 only: restore a selection from a saved channel.
            @staticmethod
            def select_item(image, op, item):
                state["sel_restored_from"] = item.get_id()
                return True

        class Selection:
            @staticmethod
            def bounds(image):
                return state["bounds"]

            @staticmethod
            def is_empty(image):
                return not state["bounds"][1]

            @staticmethod
            def save(image):
                ch = FakeChannel()
                image._channels.append(ch)
                state["saved_channel"] = ch.get_id()
                return ch

            @staticmethod
            def all(image):
                state["ops"].append("all")

            @staticmethod
            def none(image):
                state["ops"].append("none")

        class Drawable:
            @staticmethod
            def edit_clear(d):
                state["calls"].append("edit_clear")
                state["ops"].append("edit_clear")
                return True

            @staticmethod
            def edit_fill(d, fill_type):
                state["calls"].append("edit_fill")
                state["ops"].append("edit_fill")
                return state.get("fill_ok", True)

        @staticmethod
        def get_images():
            return images

        @staticmethod
        def get_pdb():
            pdb = types.SimpleNamespace(get_last_error=lambda: "")
            pdb.lookup_procedure = lambda name: None
            return pdb

        @staticmethod
        def displays_flush():
            pass

        @staticmethod
        def context_push():
            state["context_depth"] = state.get("context_depth", 0) + 1

        @staticmethod
        def context_pop():
            state["context_depth"] = state.get("context_depth", 0) - 1

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

    class Gio:
        class File:
            @staticmethod
            def new_for_path(path):
                return ("gio-file", path)

    repository.Gimp, repository.GLib, repository.GObject, repository.Gegl, \
        repository.Gio = Gimp, GLib, GObject, Gegl, Gio
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

    state = {"bounds": (True, False, 0, 0, 0, 0), "calls": [], "ops": []}
    images = [FakeImage3()]
    img = images[0]
    install_gi_stub(images, state)
    p = load_plugin().MCPPlugin()

    # --- fill_selection guard ---
    # With no selection GIMP would clear/fill the entire layer, so the call
    # must be refused before touching the layer.
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

    # --- fill_layer contract ---
    # fill_layer fills the whole layer and puts an active selection back:
    # copy -> select none -> fill -> restore the copy (combine_masks REPLACE)
    # -> delete the copy. Select all would stop at the canvas edge. The copy
    # must never be added to the image (no Selection.save, no select_item, no
    # new channel). The steps are recorded in one ordered list, so a fill
    # that ran before the selection was cleared (and thus hit only the
    # user's selection) fails the check.
    state["calls"] = []
    state["ops"] = []
    r = p._fill_layer({"image_index": 0, "color": "#ff0000"})
    check("fill_layer_keeps_selection",
          r.get("status") == "success"
          and state["ops"] == ["copy", "none", "edit_fill", "restore", "delete_copy"]
          and state["restored_from"][0] is state["copy"]
          and state["restored_from"][1:] == (0, 0, 0)  # REPLACE at (0, 0)
          and "saved_channel" not in state and "sel_restored_from" not in state
          and img.get_channels() == [] and img.undo_groups == 0
          and state.get("context_depth", 0) == 0, r)

    # A failed fill still puts the selection back.
    state["ops"] = []
    state["fill_ok"] = False
    r = p._fill_layer({"image_index": 0, "color": "#ff0000"})
    check("fill_layer_failed_fill_restores_selection",
          r.get("status") == "error"
          and state["ops"] == ["copy", "none", "edit_fill", "restore", "delete_copy"]
          and img.undo_groups == 0
          and state.get("context_depth", 0) == 0, r)
    state["fill_ok"] = True

    # A copy GIMP refuses: an error before anything changes, nothing left open.
    state["ops"] = []
    state["copy_ok"] = False
    r = p._fill_layer({"image_index": 0, "color": "#ff0000"})
    check("fill_layer_failed_copy_fills_nothing",
          r.get("status") == "error" and "Copying the selection" in r.get("error", "")
          and state["ops"] == ["copy"] and img.undo_groups == 0
          and state.get("context_depth", 0) == 0, r)
    state["copy_ok"] = True

    # A restore that raises still deletes the copy and closes the undo group.
    state["ops"] = []
    state["restore_raises"] = True
    r = p._fill_layer({"image_index": 0, "color": "#ff0000"})
    check("fill_layer_failed_restore_closes_everything",
          r.get("status") == "error"
          and state["ops"] == ["copy", "none", "edit_fill", "restore", "delete_copy"]
          and img.undo_groups == 0
          and state.get("context_depth", 0) == 0, r)
    state["restore_raises"] = False

    # With no selection there is nothing to keep: edit_fill fills the whole
    # layer as it is.
    state["ops"] = []
    state["calls"] = []
    state["bounds"] = (True, False, 0, 0, 0, 0)
    r = p._fill_layer({"image_index": 0, "color": "#ffffff"})
    check("fill_layer_no_selection",
          r.get("status") == "success" and state["ops"] == ["edit_fill"]
          and state.get("context_depth", 0) == 0, r)

    # --- undo/redo ---
    # Not exposed by GIMP 3's Python API: the clear "not supported" error,
    # not an AttributeError traceback.
    r = p._undo({"image_index": 0, "steps": 1})
    check("undo_gimp3_clear_error",
          r.get("status") == "error" and "scriptable undo" in r.get("error", "")
          and "traceback" not in r, r)
    r = p._redo({"image_index": 0, "steps": 1})
    check("redo_gimp3_clear_error",
          r.get("status") == "error" and "scriptable undo" in r.get("error", "")
          and "traceback" not in r, r)

    # --- set_active_image ---
    check("create_display_tracked",
          p._create_display(img) is True
          and p._displays.get(img.get_id()) is not None, p._displays)
    tracked = p._displays[img.get_id()]
    r = p._set_active_image({"image_index": 0})
    check("set_active_image_tracked_window_raised",
          r.get("status") == "success" and tracked.presented == 1, r)

    # An image whose window the plugin did not open cannot be raised:
    # GIMP 3 does not expose other display windows to Python.
    img_b = FakeImage3(ident=2)
    images.append(img_b)
    p._create_display(img_b)
    del p._displays[img_b.get_id()]  # simulate a user-opened window
    r = p._set_active_image({"image_index": 1})
    check("set_active_image_untracked_window_errors",
          r.get("status") == "error" and "Windows menu" in r.get("error", "")
          and "traceback" not in r, r)

    # --- close_image ---
    # close_image deletes the tracked window; GIMP then deletes the image
    # itself (last window closed), so the handler must not delete the dead
    # ID again — deleting it would raise gimp-image-delete.
    img_c = FakeImage3(ident=3)
    images.append(img_c)
    p._create_display(img_c)
    r = p._close_image({"image_index": 2, "save_first": False})
    check("close_image_tracked_display_auto_deleted",
          r.get("status") == "success" and not img_c.is_valid()
          and _LIVE["displays"].get(img_c.get_id(), []) == [], r)

    # A display the plugin did not create (a user-opened window) blocks the
    # delete; close_image must say so instead of reporting success.
    r = p._close_image({"image_index": 1, "save_first": False})
    check("close_image_untracked_display_errors",
          r.get("status") == "error" and "not tracked" in r.get("error", "")
          and img_b.is_valid(), r)

    # A window the user closed in the meantime is a stale tracking entry:
    # set_active_image must drop it.
    img_d = FakeImage3(ident=4)
    images.append(img_d)
    p._create_display(img_d)
    _LIVE["displays"].setdefault(img_d.get_id(), []).append(FakeDisplay3(img_d))
    p._displays[img_d.get_id()].delete()  # the user closes the plugin's window
    r = p._set_active_image({"image_index": 3})
    check("set_active_image_stale_entry_dropped",
          r.get("status") == "error" and img_d.get_id() not in p._displays, r)

    # close_image must drop stale entries too. It needs its own fixture:
    # the set_active_image call above already dropped img_d's entry, so a
    # close on img_d would only re-run the untracked-display case. This
    # image has two windows — the plugin's (closed by the user, so its
    # tracking entry is stale) and the user's (still open, keeping the
    # image alive).
    img_e = FakeImage3(ident=5)
    images.append(img_e)
    p._create_display(img_e)
    _LIVE["displays"].setdefault(img_e.get_id(), []).append(FakeDisplay3(img_e))
    p._displays[img_e.get_id()].delete()  # the user closes the plugin's window
    r = p._close_image({"image_index": 4, "save_first": False})
    check("close_image_stale_entry_errors_on_user_window",
          r.get("status") == "error" and "not tracked" in r.get("error", "")
          and img_e.get_id() not in p._displays  # the stale entry was dropped
          and len(_LIVE["displays"][img_e.get_id()]) == 1  # the user's window stayed open
          and img_e.is_valid(), r)

    if failures:
        print(f"FAIL: test_gimp3_compat — {len(failures)} failure(s): {failures}")
        return 1
    print("PASS: test_gimp3_compat — fill guard, fill_layer contract, "
          "undo/redo errors, and GIMP 3 window management")
    return 0


if __name__ == "__main__":
    sys.exit(main())
