# Changelog

All notable changes to `gimp-mcp-plugin` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

The plugin and the MCP server must run exactly the same version. To upgrade, restart your
MCP client so it starts the new server, then run `uvx gimp-mcp-plugin@latest install-plugin`
and restart GIMP. If you pin the server to a version, install that version's plugin with
`uvx gimp-mcp-plugin@X.Y.Z install-plugin`; from a checkout, run
`uv run gimp_mcp_server.py install-plugin`. `check_server` and `get_gimp_info` print the
right command when the versions differ.

## [Unreleased]

### Added
- `open_image_as_layer` imports an image file as a new layer in an open image, without
  opening it in its own window. A multi-layer file is merged into one layer with its
  transparency kept. The layer can be scaled to fit the canvas or stretched to its size,
  and is centered unless offsets are given. Based on #61 by @kabachuha.

### Changed
- `create_layer`'s `fill` covers the whole layer. With a selection active, it used to fill only
  the selection (#130).

### Fixed
- `get_image_bitmap` and `get_state_snapshot` region snapshots show the visible image. They
  copied only the top layer, even when it was hidden, so they could show the wrong pixels. Both
  full and region snapshots now render the visible composite (blend modes, layer groups and
  layer filters included), so a region matches the same rectangle of a full snapshot. As in
  GIMP's canvas, a visible channel (including Quick Mask) tints the snapshot. Taking a
  snapshot no longer replaces your selection or clipboard. Snapshots are now always RGBA PNGs, also
  for grayscale and indexed images, and images above 8 bits per channel come back as 16-bit
  PNGs, with colours converted to sRGB (#17).
- `get_image_bitmap` and `get_state_snapshot` honour `image_index` instead of always capturing
  the first image, and an index past the last open image is an error. The `get_image_bitmap`
  MCP tool now accepts `image_index`.
- `get_image_bitmap` and `get_state_snapshot` return an error when nothing is visible (every
  layer hidden or at 0% opacity), when a region's width or height is 0, or when a scaling bound
  is below 1 (`get_image_bitmap`'s `max_width` / `max_height`, or a negative `max_size` for
  `get_state_snapshot`, where 0 still means no scaling). Before, these returned a blank or wrong
  image, a misleading error or a division by zero.
- `get_image_bitmap` scales by `max_width` or `max_height` alone. Before, scaling needed both,
  and a single bound returned the image at full size. A region with one bound of its own was
  left unscaled, or scaled by the top-level bounds when both of those were given.
- `get_image_bitmap` returns an error for a `region` that doesn't give `origin_x`, `origin_y`,
  `width` and `height`. A region with none of them, such as `{}` or one with only `max_width` /
  `max_height`, used to return the full image instead. The error names the missing keys.
- `create_layer` rejects width or height outside 1–524288 with an error naming the
  dimension and value. Zero no longer silently uses the image size (#135).
- `apply_drop_shadow` no longer fails with `PDB procedure 'plug-in-gauss' not found` on
  GIMP 3.2 builds that lack the Gauss blur plug-in. It now falls back to the
  `gegl:gaussian-blur` operation, the same fallback `blur` already uses.
- `fill_selection` no longer clears or fills the entire layer when no selection is active. GIMP
  applies an edit-clear or edit-fill with no selection to the whole layer, so a leftover empty
  selection silently destroyed image content while the tool reported success. An empty selection
  now returns an error pointing to `fill_layer` for whole-layer fills.
- The `create_layer` and `reorder_layer` docs said position 0 is the bottom of the layer stack.
  In GIMP it is the top, so asking for the bottom put the layer on top. The docs now match
  GIMP: positions count from the top, and `create_layer`'s default -1 means directly above
  the selected layer (#129).
- `create_layer` reports where the layer landed, not the position it was given: with the default
  -1, or with a floating selection in the image, the two differ. A new `parent` field names the
  layer group it went into, if any (#130).
- `create_layer` works on indexed images. It used to fail with an error about a gray layer (#130).
- `create_layer` and `reorder_layer` no longer report success when GIMP refuses to insert or
  move the layer. They return GIMP's error and leave the image unchanged (#130).
- `create_layer` rejects positions below -1. GIMP read them as "above the selected layer, but
  never inside a group" (#130).
- `add_text` no longer replaces an unknown font silently. Its default font is now `Sans-serif`, and
  its result reports `font` (the applied font), `requested_font` when it differs from `font`, and a
  `warning` naming both fonts when a default font was substituted (#124).
- `export_sprite_sheet` works on GIMP 3. It failed every time with
  `ImageBaseType has no attribute 'RGBA'`, and the sheet now keeps its transparent background
  instead of being flattened (#64).
- `edit_text` can change a text layer's font. Passing `font` always failed on GIMP 3.2. When a font
  name is given, the result now includes `font` (the applied font), `requested_font` when it differs
  from `font`, and a `warning` when the requested font wasn't found and a default was substituted
  (#64, #124).
- `export_image`, `batch_export`, `export_web_optimized` and `export_icon_sizes` now write real
  JPEG, WebP, TIFF and BMP files. They used GIMP 2 procedure names that GIMP 3 doesn't have, so
  every export silently fell back to PNG data under a `.jpg`, `.webp`, `.tiff` or `.bmp` name,
  and the `quality` setting was ignored. An unsupported format (e.g. `gif`), a quality outside
  0–100 or a missing export procedure now returns an error. `tif` is accepted as an alias for
  `tiff`.
- `flatten_image`, `merge_visible_layers`, `batch_resize`, `new_canvas` and the export tools
  (`export_icon_sizes`, `export_web_optimized`, `export_social_media_kit`,
  `export_sprite_sheet`, and flattened exports) no longer report success when GIMP rejects a
  step. The export tools also delete their temporary copies when a step fails, and
  `new_canvas` deletes the half-made image.
- Fill, line, crop, scale, rotate, flip and canvas-resize tools, and saving an XCF when GIMP's
  save procedure is unavailable, no longer report success when GIMP rejects the edit (for
  example on a locked layer). They now return an error with GIMP's reason. Fill tools also
  clear their temporary selection when the fill fails.
- `auto_levels` returns an error when GIMP lacks `gimp-drawable-levels-stretch`, instead of
  falling back to a levels call that could never change the image and reporting success.
- Tools that call GIMP procedures no longer report success when GIMP rejects the call
  (for example on a locked layer) or doesn't have the procedure. They now return an
  error, with GIMP's reason when it gives one. This affects most adjustment, filter,
  selection, drawing, text, transform and save tools, so a call that used to "succeed"
  without changing anything may now return an error. A tool that fails partway through
  can leave earlier steps applied, so check the image before retrying (#90).

## [0.3.0] - 2026-11-12

### Added
- `GIMP_MCP_TIMEOUT` sets how many seconds the MCP server waits for GIMP to finish a
  command (default 300). `check_server` and `get_gimp_info` report it as
  `timeout_seconds`, and a timed-out command now says so and tells you how to raise it (#108).

### Changed
- Tools run on a single worker thread off the server's event loop, in the order they were
  sent. The server keeps answering pings and cancellations while GIMP runs a long
  command. A cancelled command that is still waiting never runs, but one GIMP has already
  started finishes in the background, so check the image before retrying it, or the edit
  may be applied twice (#106).

## [0.2.1] - 2026-11-11

### Fixed
- `denoise`, `apply_gaussian_blur`, `apply_pixelate`, `apply_emboss`, `apply_vignette` and
  `apply_noise` reported success on GIMP 3.2 without changing the image. They now apply
  the filter and raise an error for unknown or out-of-range parameters (#103).
- `get_histogram` returned wrong values (#48, #102).
- `new_canvas` no longer lets the current paint opacity or mode affect its background
  fill (#102).
- The fill and draw tools, and `create_layer`, now reset opacity and paint mode before
  painting, so a value left by an earlier `call_api` call no longer changes their result.
  Your own context settings are restored afterwards (#104).

## [0.2.0] - 2026-11-11

### Added
- `install-plugin` subcommand: `uvx gimp-mcp-plugin@latest install-plugin` installs the
  bundled GIMP plugin into the newest GIMP 3.x config dir (Linux standard/Snap/Flatpak,
  macOS, Windows including the Microsoft Store version). Supports `--dry-run`,
  `--gimp-dir DIR` and the `GIMP3_DIRECTORY` override (#98, #100).

### Changed
- The README documents `uvx gimp-mcp-plugin@latest` as the main way to install and
  run the server (#99).

## [0.1.0] - 2026-11-11

First release on PyPI as `gimp-mcp-plugin` (#96).

### Added
- Plugin/server version check: `check_server` and `get_gimp_info` report a mismatch
  and the command that fixes it (#95).
- **Tools > MCP > MCP Server Settings...** dialog with autostart and a configurable
  port (#91).

### Fixed
- **Tools > MCP > Check** and **Restart** reported the wrong server state (#93).
- `adjust_brightness_contrast` now matches GIMP 3's perceptual result (#58).
- `auto_levels` on GIMP 3.2 (#60).
- `edit_text` pixel units on GIMP 3.2 (#59).
- `open_image` no longer fails in headless mode when no display can be created (#67).
- `get_pixel_color` returns sRGB instead of linear values (#55).
- The plugin no longer crashes after a client-side timeout (#82).

[Unreleased]: https://github.com/gimp-mcp/gimp-mcp/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/gimp-mcp/gimp-mcp/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/gimp-mcp/gimp-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/gimp-mcp/gimp-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/gimp-mcp/gimp-mcp/releases/tag/v0.1.0
