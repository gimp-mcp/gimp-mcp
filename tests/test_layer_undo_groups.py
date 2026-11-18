import unittest
from unittest.mock import Mock, call, patch

from test_pdb_status import install_gi_stub, load_plugin


class LayerUndoGroupsTests(unittest.TestCase):
    def setUp(self):
        install_gi_stub()
        module = load_plugin()
        self.module = module
        self.plugin = module.MCPPlugin()
        self.calls = Mock()
        self.plugin._get_image = Mock(return_value=self.calls.image)
        self.plugin._resolve_layer = Mock(return_value=self.calls.layer)
        flush = patch.object(module.Gimp, "displays_flush", self.calls.flush, create=True)
        flush.start()
        self.addCleanup(flush.stop)

    def test_set_layer_properties_skips_undo_for_omitted_properties(self):
        result = self.plugin._set_layer_properties(
            {"opacity": None, "blend_mode": None, "visible": None}
        )
        self.assertEqual(result, {"status": "success", "results": {"status": "success"}})
        self.assertEqual(self.calls.mock_calls, [])

    def test_set_layer_properties_skips_undo_for_empty_blend_mode(self):
        result = self.plugin._set_layer_properties({"blend_mode": ""})
        self.assertEqual(result, {"status": "success", "results": {"status": "success"}})
        self.assertEqual(self.calls.mock_calls, [])

    def test_set_layer_properties_rejects_invalid_opacity_before_undo(self):
        result = self.plugin._set_layer_properties({"opacity": "abc", "visible": False})
        self.assertEqual(result["status"], "error")
        self.assertIn("could not convert string to float", result["error"])
        self.assertEqual(self.calls.mock_calls, [])

    def test_set_layer_properties_rejects_non_finite_and_out_of_range_opacity(self):
        for opacity in ("nan", float("inf"), float("-inf"), -1, 101):
            with self.subTest(opacity=opacity):
                self.calls.reset_mock()
                result = self.plugin._set_layer_properties({"opacity": opacity})
                self.assertEqual(result["status"], "error")
                self.assertIn("opacity must be between 0 and 100", result["error"])
                self.assertEqual(self.calls.mock_calls, [])

    def test_set_layer_properties_keeps_zero_opacity_and_false_visibility(self):
        result = self.plugin._set_layer_properties({"opacity": 0, "visible": False})
        self.assertEqual(result["status"], "success")
        self.assertEqual(
            self.calls.mock_calls,
            [
                call.image.undo_group_start(),
                call.layer.set_opacity(0.0),
                call.layer.set_visible(False),
                call.image.undo_group_end(),
                call.flush(),
            ],
        )

    def test_edit_text_skips_undo_for_omitted_or_blank_options(self):
        for options in ({}, {"text": None, "font": " ", "size": None, "color": ""}):
            with self.subTest(options=options):
                self.calls.reset_mock()
                result = self.plugin._edit_text(options)
                self.assertEqual(result, {"status": "success", "results": {"status": "success"}})
                self.assertEqual(self.calls.mock_calls, [])

    def test_edit_text_rejects_invalid_size_before_any_edit(self):
        self.plugin._lookup_pdb = Mock(return_value=self.calls.procedure)
        self.plugin._run_pdb = self.calls.run_pdb
        for size in ("abc", 0, -1, "nan", float("inf"), float("-inf"), 8193, "8193"):
            with self.subTest(size=size):
                self.calls.reset_mock()
                self.plugin._get_image.reset_mock()
                self.plugin._resolve_layer.reset_mock()
                self.plugin._lookup_pdb.reset_mock()

                with patch.object(self.module.Gimp, "Unit", Mock(), create=True):
                    result = self.plugin._edit_text({"text": "X", "size": size})

                self.assertEqual(result["status"], "error")
                if size == "abc":
                    self.assertIn("could not convert string to float", result["error"])
                else:
                    self.assertIn("size must be greater than 0 and at most 8192", result["error"])
                self.plugin._get_image.assert_not_called()
                self.plugin._resolve_layer.assert_not_called()
                self.plugin._lookup_pdb.assert_not_called()
                self.assertEqual(self.calls.mock_calls, [])

    def test_edit_text_keeps_sizes_within_gimp_range(self):
        self.plugin._lookup_pdb = Mock(return_value=self.calls.procedure)
        self.plugin._run_pdb = self.calls.run_pdb
        config = self.calls.procedure.create_config.return_value
        unit = Mock()
        with patch.object(self.module.Gimp, "Unit", unit, create=True):
            for size in (0.5, 24, "36", 8192, "8192"):
                with self.subTest(size=size):
                    self.calls.reset_mock()
                    self.plugin._lookup_pdb.reset_mock()

                    result = self.plugin._edit_text({"size": size})

                    self.assertEqual(result["status"], "success")
                    self.plugin._lookup_pdb.assert_called_once_with("gimp-text-layer-set-font-size")
                    self.assertEqual(
                        self.calls.mock_calls,
                        [
                            call.image.undo_group_start(),
                            call.procedure.create_config(),
                            call.procedure.create_config().set_property("layer", self.calls.layer),
                            call.procedure.create_config().set_property("font-size", float(size)),
                            call.procedure.create_config().set_property("unit", unit.pixel()),
                            call.run_pdb(self.calls.procedure, config),
                            call.image.undo_group_end(),
                            call.flush(),
                        ],
                    )

    def test_edit_text_keeps_empty_text_as_an_edit(self):
        self.plugin._lookup_pdb = Mock(return_value=self.calls.procedure)
        self.plugin._run_pdb = self.calls.run_pdb
        config = self.calls.procedure.create_config.return_value

        result = self.plugin._edit_text({"text": ""})

        self.assertEqual(result["status"], "success")
        self.plugin._lookup_pdb.assert_called_once_with("gimp-text-layer-set-text")
        self.assertEqual(
            self.calls.mock_calls,
            [
                call.image.undo_group_start(),
                call.procedure.create_config(),
                call.procedure.create_config().set_property("layer", self.calls.layer),
                call.procedure.create_config().set_property("text", ""),
                call.run_pdb(self.calls.procedure, config),
                call.image.undo_group_end(),
                call.flush(),
            ],
        )


if __name__ == "__main__":
    unittest.main()
