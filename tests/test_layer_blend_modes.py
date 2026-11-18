import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_pdb_status import install_gi_stub, load_plugin


class LayerBlendModeTests(unittest.TestCase):
    def setUp(self):
        install_gi_stub()
        module = load_plugin()
        self.plugin = module.MCPPlugin()
        self.image = Mock()
        self.layer = Mock()
        self.layer_new = Mock(return_value=self.layer)
        self.image.get_base_type.return_value = 0
        self.multiply = object()
        self.hard_light = object()
        modes = Mock(NORMAL=object(), MULTIPLY=self.multiply, HARDLIGHT=self.hard_light)
        self.plugin._get_image = Mock(return_value=self.image)
        self.plugin._resolve_layer = Mock(return_value=self.layer)
        gimp = patch.multiple(
            module.Gimp,
            LayerMode=modes,
            Layer=Mock(new=self.layer_new),
            ImageBaseType=SimpleNamespace(RGB=0, GRAY=1, INDEXED=2),
            ImageType=SimpleNamespace(RGBA_IMAGE=0, GRAYA_IMAGE=1, INDEXEDA_IMAGE=2),
            FillType=SimpleNamespace(TRANSPARENT=0),
            MAX_IMAGE_SIZE=524288,
            create=True,
        )
        gimp.start()
        self.addCleanup(gimp.stop)

    def test_create_layer_rejects_unknown_blend_mode_before_allocating_layer(self):
        result = self.plugin._create_layer(
            {"width": 32, "height": 32, "blend_mode": "MULITPLY"}
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("Unknown blend_mode 'MULITPLY'", result["error"])
        self.assertIn("MULTIPLY", result["error"])
        self.layer_new.assert_not_called()
        self.image.insert_layer.assert_not_called()

    def test_set_layer_properties_rejects_unknown_blend_mode_before_edits(self):
        result = self.plugin._set_layer_properties(
            {"blend_mode": "MULITPLY", "opacity": 20, "visible": False}
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("Unknown blend_mode 'MULITPLY'", result["error"])
        self.image.undo_group_start.assert_not_called()
        self.assertEqual(self.layer.mock_calls, [])

    def test_set_layer_properties_accepts_blend_mode_aliases(self):
        for name, mode in (
            ("multiply", self.multiply),
            ("HARD_LIGHT", self.hard_light),
            (" Hard Light ", self.hard_light),
            ("hard-light", self.hard_light),
        ):
            with self.subTest(name=name):
                self.layer.reset_mock()
                result = self.plugin._set_layer_properties({"blend_mode": name})
                self.assertEqual(result["status"], "success")
                self.layer.set_mode.assert_called_once_with(mode)


if __name__ == "__main__":
    unittest.main()
