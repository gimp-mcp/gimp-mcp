#!/usr/bin/env python3
"""Tests create_layer dimension validation without a running GIMP (#135)."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_pdb_status import install_gi_stub, load_plugin


class CreateLayerDimensionsTests(unittest.TestCase):
    def setUp(self):
        install_gi_stub()
        self.mod = load_plugin()
        self.plugin = self.mod.MCPPlugin()
        self.image = Mock()
        self.image.get_width.return_value = 640
        self.image.get_height.return_value = 480
        self.image.get_base_type.return_value = 0
        self.image.get_item_position.return_value = 0
        self.layer = Mock()
        self.layer.get_name.return_value = "test"
        self.layer.get_id.return_value = 7
        self.layer.get_parent.return_value = None
        self.new_layer = Mock(return_value=self.layer)
        self.plugin._get_image = Mock(return_value=self.image)
        self.plugin._blend_mode_from_string = Mock(return_value=28)
        self.patcher = patch.multiple(
            self.mod.Gimp,
            MAX_IMAGE_SIZE=524288,
            ImageBaseType=SimpleNamespace(RGB=0, GRAY=1, INDEXED=2),
            ImageType=SimpleNamespace(RGBA_IMAGE=0, GRAYA_IMAGE=1, INDEXEDA_IMAGE=2),
            FillType=SimpleNamespace(TRANSPARENT=0),
            Layer=SimpleNamespace(new=self.new_layer),
            displays_flush=Mock(),
            create=True,
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_create_layer_rejects_out_of_range_dimensions(self):
        for dimension in ("width", "height"):
            for value in (-5, 0, 524289):
                with self.subTest(dimension=dimension, value=value):
                    result = self.plugin._create_layer({dimension: value})
                    self.assertEqual(result["status"], "error", result)
                    self.assertIn(dimension, result["error"])
                    self.assertIn(str(value), result["error"])
                    self.assertIn("1", result["error"])
                    self.assertIn("524288", result["error"])
                    self.new_layer.assert_not_called()
                    self.image.insert_layer.assert_not_called()
                    self.mod.Gimp.displays_flush.assert_not_called()

    def test_create_layer_defaults_only_missing_or_none_dimensions(self):
        for params, expected in (
            ({}, (640, 480)),
            ({"width": None, "height": None}, (640, 480)),
            ({"width": 32}, (32, 480)),
            ({"height": 24}, (640, 24)),
            ({"width": None, "height": 24}, (640, 24)),
            ({"width": 32, "height": None}, (32, 480)),
        ):
            with self.subTest(params=params):
                self.check_dimensions(params, expected)

    def test_create_layer_accepts_inclusive_dimension_limits(self):
        for width, height in ((1, 1), (524288, 1), (1, 524288)):
            with self.subTest(width=width, height=height):
                self.check_dimensions({"width": width, "height": height}, (width, height))

    def check_dimensions(self, params, expected):
        result = self.plugin._create_layer(params)
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.new_layer.call_args.args[2:4], expected)
        self.assertEqual((result["results"]["width"], result["results"]["height"]), expected)
        self.assertEqual(self.image.insert_layer.call_args.args, (self.layer, None, -1))


if __name__ == "__main__":
    unittest.main()
