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

### Fixed
- A tool call that GIMP refuses no longer also shows an error message in GIMP. The tool
  already returns GIMP's reason; the message duplicated it, and calls that are expected
  to fail popped up messages too. In `call_api` code, a failed GIMP call returns False
  or None without a message: check the result, and `Gimp.get_pdb().get_last_error()`
  for the reason. To get GIMP's messages back until GIMP restarts, run
  `Gimp.get_plug_in().set_pdb_error_handler(Gimp.PDBErrorHandler.INTERNAL)` through
  `call_api`.
- `new_canvas` rejects a `resolution` outside 1-1048576, `draw_line` a `width` outside
  1-10000, and `draw_rectangle`/`draw_ellipse` a `line_width` outside 0-2000 (0 excluded).
  GIMP refused these values, but the tools went on: the canvas got the default resolution
  while the requested one was reported, and the stroke used whatever width was set before.
- `edit_text` and `set_layer_properties` skip opening an undo group when no edits
  are requested. Invalid opacity and font size are rejected before opening an
  undo group, including font sizes above GIMP's 8192 limit (#148).
- All 148 CSS color names paint the right color. Names other than the 16 basic HTML ones,
  such as `orange`, `gold` or `cornflowerblue`, used to paint cyan (#44). Based on #54 by
  @floraschouw.
- A color the tools can't read returns an error naming it and leaves the image unchanged.
  Unknown names, hex without `#` and other strings GIMP couldn't read used to paint cyan
  and report success, and values after an `rgba()` color's fourth were ignored (#145).
  A rejected color no longer clears GIMP's redo history.
- An empty color (`""`) means "no color", as in `draw_line`, `draw_rectangle` and
  `draw_ellipse`: `fill_selection` fills with the current foreground color, and
  `edit_text` and `set_colors` leave the color unchanged. They used to paint or set cyan.
- `fill_selection` with no `color` fills with the current foreground color. Through the
  MCP server it used to fail; a direct plug-in request without `color` filled white.
- `fill="none"` is transparent, like `fill="transparent"`, in `new_canvas`, `create_layer`
  and `resize_canvas`. `resize_canvas` used to flatten the image for it.
- `apply_drop_shadow` gives a shadow that reaches its layer's edge a soft edge there
  too, also for a layer with a mask. The blur used to be cut off at the layer's edge, e.g.
  for a layer that fills the canvas. Based on #62 by @gr8monk3ys.
- `apply_drop_shadow` moves the shadow of a layer whose position is locked to its offset.
  The shadow used to stay under the layer.
- `apply_drop_shadow` rejects a `blur_radius` outside 0-1500 and an `opacity` outside
  0-100. A failed drop shadow no longer leaves an extra layer in the image, and the
  foreground color is no longer changed to the shadow color.
- `export_web_optimized` names its files after the source image (`photo.jpg`, `photo.png`),
  or `image_<index>` for an image that was never saved. All exports used to go to
  `image.jpg` / `image.png`. An export that would overwrite the source file, also through a
  symlinked folder, gets a `_web` suffix. Based on #62 by @gr8monk3ys.
- `rotate_image` with an angle that isn't a multiple of 90 works. It used to fail, asking
  for a GIMP procedure that 3.2 doesn't have. It turns every layer, channel and path and
  the selection clockwise about the image centre and resizes the canvas to fit; the layers
  are kept, not flattened, and the uncovered corners are transparent. A layer, channel or
  path with locked contents or position is an error, and the image is left unchanged.
  Any multiple of 90, such as -180 or 450, uses the lossless rotation. Based on #63 by
  @yonggandewo12.
- `convert_color_mode` converts to `INDEXED`. It used to fail with an error about
  `NO_DITHER`. `num_colors` must be 1-256, and an image with layer groups gets an error
  asking to merge them first, as GIMP won't index those from a script. So does an image
  that isn't 8-bit non-linear, without marking it as changed. A conversion GIMP refuses
  is reported as an error. Converting an image to the mode it already has
  doesn't mark it as changed; `RGBA` and `GRAYA` then only add alpha. Based on #63 by
  @yonggandewo12.
- `get_image_metadata` reports each layer's `is_group` and `layer_type` (`RGB`, `RGBA`,
  `GRAY`, ...), the image's `resolution_x`/`resolution_y`, and a channel's `color` as hex.
  `is_group` used to be true for every layer, `layer_type` and channel colors were Python
  object descriptions, and the resolution was null. Based on #63 by @yonggandewo12.
- `list_layers` and `get_image_metadata` report `blend_mode` by name (`NORMAL`,
  `HARD_LIGHT`, ...), as `create_layer` and `set_layer_properties` take it, instead of
  GIMP's number (`"28"`). A mode those tools don't take is named after GIMP
  (`ADDITION`, `NORMAL_LEGACY`). `list_layers` reports `offsets` as `[x, y]`; it used to
  be `[true, x, y]`. Based on #63 by @yonggandewo12.
- `edit_text` on a layer that isn't a text layer is an error naming it, and leaves the
  image unchanged. It used to fail with "could not convert ... GimpTextLayer" and mark the
  image changed. `edit_text` returns the layer's `layer_name` after the edit: new text
  renames a text layer that still has GIMP's automatic name (its text). Based on #63 by
  @yonggandewo12.
- `get_pixel_color` outside the layer is an error giving the layer's size and its offsets
  in the image (`x` and `y` are relative to the layer). It used to fail with "'NoneType'
  object is not subscriptable". A fractional or boolean `x` or `y` is an error; 1.9 used to
  read pixel 1. Based on #63 by @yonggandewo12.
- `modify_selection` returns `has_selection` and, while something is selected, its bounds
  (`x`, `y`, `width`, `height`): a shrink or border can leave nothing selected, which it
  used to report as a plain success. A negative `amount`, which GIMP ignored while the
  tool reported success, is an error, as is a fractional one for `grow`, `shrink` and
  `border` (2.7 used to grow by 2) and a non-finite one; the selection is left as it was.
  `amount` is optional (default 0), so `sharpen`, which ignores it, no longer needs one.
  Based on #63 by @yonggandewo12.
- `batch_export` is an error when no image could be exported, giving each image's reason;
  it used to report success with an empty `exported` list. When some images export, it
  still succeeds and lists the others in `errors`. A `name_pattern` with fields other than
  `{name}` and `{index}` is an error before anything is written; it used to fail for each
  image with only the field's name (`'bogus'`). Based on #63 by @yonggandewo12.
- `fill_layer` also fills the parts of a layer outside the canvas, as its description
  promises; it used to fill only the part inside the canvas.
- `fill_layer` leaves the selection as it was; it used to clear it. It still fills the
  whole layer. `get_histogram`'s description no longer lists `min` and `max`, which it
  never returned, and says that only the selected pixels count when there is a selection.
  Based on #63 by @yonggandewo12.
- `get_histogram` gives `mean`, `median` and `std_dev` on a 0-255 scale at every
  precision. For 16-bit, 32-bit and floating-point images they used to be on a 0-1 scale
  (GIMP's own), so a white layer's mean was 1 instead of 255. Based on #63 by
  @yonggandewo12.
- `get_histogram` with a `channel` the layer doesn't have (`alpha` without an alpha
  channel, `red`, `green`, `blue` or `luminance` on a grayscale layer) is an error saying
  so. It used to fail with just "gimp-drawable-histogram did not succeed".
- `save_xcf`, `close_image` with `save_first`, and `get_image_bitmap`'s last-resort export
  run GIMP's save procedures non-interactively. Those procedures default to interactive, so
  the bitmap fallback (used only when the PNG export fails) could open GIMP's export
  dialog and wait for it. Based on #63 by @yonggandewo12.
- `get_histogram` takes `channel="luminance"` (perceived brightness), which GIMP 3.2 has;
  `value` is the brightest of red, green and blue. Based on #63 by @yonggandewo12.
- `crop_to_selection` with no selection (and `autocrop` off) returns an error and leaves
  the image unchanged. It used to report success, change nothing and mark the image as
  changed. It now returns the `x`, `y`, `width` and `height` it cropped to, as its docs
  said; with `autocrop`, the image's new `width` and `height`. Based on #63 by
  @yonggandewo12.
- `select_by_color` with a `threshold` outside 0-255 (such as 300 or -5) is an error and
  the selection is left as it was. GIMP ignored such a value and silently used its own
  current threshold. Clients writing to the plugin's socket directly also get an error
  for a fraction such as 1.9, which was cut to 1.
- `select_by_color` always compares whole colors. A sample criterion that `call_api` code
  had left set (such as `Gimp.SelectCriterion.RGB_RED`) made it compare one channel only,
  so selecting red also selected white.
- `sharpen` with a `threshold` outside 0-255 is an error before anything changes. It
  failed only after the image was marked as changed, quoting the threshold on GEGL's 0-1
  scale (300 as 1.176...). Clients writing to the plugin's socket directly also get an
  error for a fraction such as 1.9, or `true`, which became 1.
- `select_by_color` returns an error when no pixel of the layer matches the color, and
  leaves the selection as it was. It used to report success, and with `replace` the
  selection was silently gone. It now returns `has_selection` and the selection's `x`, `y`,
  `width` and `height`. Based on #63 by @yonggandewo12.
- `warp_region` pushes pixels the way GIMP's Warp Transform tool does. It used to turn the
  whole layer black and report success. `x`/`y` are image coordinates, also on a layer
  with an offset, and an active selection limits the change. A vector with no push or a
  push longer than the layer's diagonal plus the radius, an `amount` outside 0-1 (0
  excluded), a `radius` outside 0.5-5000, a vector that doesn't reach the layer, a layer
  group or a layer with locked pixels (its own or its group's) is an error, and nothing
  is warped. Based on #63 by @yonggandewo12.
- `batch_resize` without `width`, `height` or `scale_factor` returns an error. It used to
  "resize" every open image to its own size, mark it as changed and report success.
  A size of 0 or less, a fractional `width`/`height`, no open images, or a result above
  GIMP's 524288-pixel limit for any image is an error too, and no image is resized; such a
  limit used to stop the batch halfway. An image already at its target size is left
  untouched, and each result says whether the image was `scaled`. Based on #63 by
  @yonggandewo12.
- `scale_image` and `scale_to_fit` use their `interpolation`. They used to ignore it and
  scale with GIMP's current setting.
- The plug-in's socket server no longer hangs on a bad request. A request that isn't JSON
  or UTF-8 gets an error reply and ends the connection; it used to get no reply, and its
  connection held a thread for good. Two requests that arrive together each get a reply
  (neither used to); to send several on one connection reliably, send
  `disable_auto_disconnect` first. A client that sends nothing, or half a request, is
  disconnected after a timeout (300 s idle, 30 s to finish a request). A reply that can't
  be encoded as JSON comes back as an error instead of no reply. Replies end with a
  newline. `disable_auto_disconnect` keeps only its own connection open, and the
  connection now serves further requests; before, it served one request whatever the
  setting. Based on #63 by @yonggandewo12.
- `call_api` output contains only what the command printed. Log lines from other
  connections running at the same time ("Connected to client: ...") used to appear in it,
  and after a command that raised, the plug-in's own log output was lost until the
  plug-in restarted. Based on #63 by @yonggandewo12.
- `adjust_curves` checks custom `points` before changing anything: 2-1024 `[input, output]`
  pairs (or a flat list of them), each value 0-255, no two points with the same input.
  A `NaN` value used to crash GIMP. Values outside 0-255 were clipped, so a curve could
  silently do nothing; an odd count or a single point failed with GIMP's bare "execution
  error" and left the image marked as changed; `[input, output, extra]` triples were read
  as other pairs; of two points with the same input, the last one silently won. A
  `channel` the layer doesn't have (`alpha` without an alpha channel, `red`, `green` or
  `blue` on a grayscale layer) is an error too; it failed the same bare way. On an indexed
  layer `adjust_curves` is an error; it reported success and changed nothing. Based on
  #63 by @yonggandewo12.
- `call_api` with `"pyGObject-eval"`, as documented, returns the values of the
  expressions. It used to run them as statements and return empty strings. Expressions,
  also with `"python-fu-eval"`, see the variables earlier commands set. `args` that aren't
  `[mode, [code, ...]]` are an error and nothing runs; a string in place of the list
  used to run each of its characters. A request with an unknown `type` and no `args` gets
  an error naming the type; it used to fail with just `'params'` or `'args'`. Only clients
  writing to the socket themselves can send one: the MCP server sends only types the
  plug-in knows. Based on
  #63 by @yonggandewo12.
- The tool docs say which layer a tool uses when no layer is given: the selected one,
  else the top one. They said "the active layer", which GIMP 3 doesn't have. `new_canvas`
  says `name` names the background layer, not the image. The README's GIMP 3.2
  compatibility table no longer recommends a procedure 3.2 doesn't have
  (`gimp-text-fontname`) or GEGL operations that can't be applied to a layer
  (`gegl:linear-gradient`), and the protocol docs no longer recommend
  `image.get_active_layer()`, which doesn't exist. Based on #63 by @yonggandewo12.
- An `image_index` or `layer_index` that isn't a whole number 0 or more is an error, and
  nothing changes. A negative one used to count from the end: `image_index=-1` acted on
  another open image, and `layer_index=-1` on the bottom layer, so `rename_layer`,
  `delete_layer`, `set_layer_properties` or `reorder_layer` changed the wrong layer and
  reported success. `-0.5`, `1.9` and `true` picked a nearby index the same way, and a
  bad `layer_index` was ignored when a layer name was also given. Based on #63 by
  @yonggandewo12.
- Tools that change a layer's pixels (the adjustment, filter, fill and drawing tools,
  `gradient_fill` and `warp_region`) refuse a layer group, a layer with locked pixels and a
  layer in a group with locked pixels, with an error saying which, and change nothing.
  They used to fail with GIMP's message, or none, and leave the image marked as changed.
  `sharpen`, `blur`, `denoise`, `apply_gaussian_blur`, `apply_pixelate`, `apply_emboss`,
  `apply_vignette` and `apply_noise` were worse: they changed locked pixels and reported
  success, and on a layer group reported success without changing anything.
  `apply_drop_shadow` refuses a layer group, which it failed on the same way; it still
  shadows a locked layer, which it only reads. `convert_color_mode` RGBA or GRAYA refuses
  a locked layer that needs alpha before converting; it used to convert the image, then
  fail with "Adding alpha to layer 'X' failed: success".

### Changed
- Some results have a new format, so clients that read them may need updating (details
  under Fixed): `list_layers` reports `offsets` as `[x, y]` (was `[true, x, y]`);
  `list_layers` and `get_image_metadata` report `blend_mode` by name (was GIMP's number);
  `get_histogram`'s `mean`, `median` and `std_dev` are 0-255 at every precision (were 0-1
  above 8 bit); `call_api` with `"pyGObject-eval"` returns the expressions' values (was
  empty strings).
- `create_layer` and `set_layer_properties` reject unknown blend modes instead of
  using Normal, and accept names like `hard light` and `hard-light` (#149).
  An empty `blend_mode` leaves the mode unchanged in `set_layer_properties`.
- An unknown option name is an error that names the valid ones, and nothing changes.
  Tools used to replace it with a default and go on: `operation="bogus"` in
  `select_rectangle`, `select_ellipse` and `select_by_color` replaced the selection, and
  `interpolation`, `anchor` (`resize_canvas`), `direction` (`flip_image`), `mode`
  (`desaturate`), `color_range`, `range` (`adjust_color_balance`), `channel` and `preset`
  (`adjust_curves`, `get_histogram`), `gradient_type`, `color_mode` (`new_canvas`, which
  reported the unknown mode as if it were used), `tool` (`draw_line`), `platform`
  (`export_icon_sizes`) and `source` (`export_sprite_sheet`) fell back the same way.
  `export_social_media_kit` skipped unknown `platforms`. Names ignore case and surrounding
  spaces. Based on #63 by @yonggandewo12.
- Some color strings that used to paint now return an error. Use hex or
  `rgb(r, g, b)` instead:
  - `cmyk()` and `cmyka()` colors. GIMP read some of them, but they were never documented,
    and Gegl reads others, such as `cmyk(0%,100%,100%,0%)`, as cyan.
  - `rgb()` values separated partly by commas and partly by spaces, such as
    `rgb(255, 128 0)`.
  - Text after the closing paren, as in `rgb(1,2,3) junk`, or another word in place of
    `rgb`, as in `rgbx(1,2,3)`.
  - An empty value from a doubled, leading or trailing comma, as in `rgb(255,0,0,)`.

### Added
- Percentages in `rgb()` / `rgba()` colors, such as `rgb(50%, 50%, 50%)`, and the
  space-separated form `rgb(255 128 0 / 0.5)`.

## [0.5.0] - 2026-11-18

### Changed
- An `rgb()` / `rgba()` color whose values can't be read, such as `rgb(50%,50%,50%)`
  (percentages) or `rgb(1,2)` (too few values), returns an error and leaves the image
  unchanged. It used to paint cyan and report success (#41). Unknown color names still
  paint cyan (#44), and values after the fourth are ignored (#145).

### Fixed
- `get_state_snapshot` validates region coordinates and required dimensions before
  connecting to GIMP. Missing sizes no longer use `max_size`, and invalid values
  return errors naming the caller's `x`, `y`, `width` or `height` parameter (#141).
- Colors given as `rgb()` paint the color asked for. 0-255 values such as
  `rgb(255,87,51)` used to paint white, and 0-1 values looked washed out, because GIMP
  read them as linear values (#50). Based on #41 by @robisonsantos.
- `gradient_fill` paints the gradient, limited to the selection if there is one. It used
  to paint nothing (#41).
- `close_image` and `set_active_image` work on GIMP 3.2 instead of failing with an
  `AttributeError` (#51). `close_image` never reports success while the image is still
  open: for an image whose window was opened in GIMP directly, it returns an error saying
  to close the window there. `set_active_image` raises windows the plugin opened and
  returns an error for other images.
- `undo` and `redo` return an error explaining that GIMP 3 has no scriptable undo,
  instead of failing with an `AttributeError` (#42).
- **Tools > MCP > Start MCP Server** shows a message once the server has started.

## [0.4.0] - 2026-11-17

### Added
- `open_image_as_layer` imports an image file as a new layer in an open image, without
  opening it in its own window. A multi-layer file is merged into one layer with its
  transparency kept. The layer can be scaled to fit the canvas or stretched to its size,
  and is centered unless offsets are given. Based on #61 by @kabachuha.

### Changed
- `create_layer`'s `fill` covers the whole layer. With a selection active, it used to fill only
  the selection (#130).
- `get_image_bitmap` and `get_state_snapshot` no longer enlarge images. Their bounds
  (`max_width` / `max_height`, `max_size`) only shrink, so an image or region smaller than
  them comes back at its own size: a 240×300 region with `get_state_snapshot`'s default
  `max_size=512` used to be upscaled to 409×512. A single bound can't blow up a thin image
  either (a 1×1000 image with only `max_width=100` would have become 100×100000).

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
- `get_image_bitmap` and `get_state_snapshot` compute scaled sizes exactly. They could come out
  a pixel short (175×75 fitted into 70×70 gave 70×29, not 70×30), and an image already at the
  size of its bounds could be resampled 1 pixel smaller, distorting its aspect ratio.
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

[Unreleased]: https://github.com/gimp-mcp/gimp-mcp/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/gimp-mcp/gimp-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/gimp-mcp/gimp-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/gimp-mcp/gimp-mcp/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/gimp-mcp/gimp-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/gimp-mcp/gimp-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/gimp-mcp/gimp-mcp/releases/tag/v0.1.0
