---
name: imagegen
description: Create and edit raster-image assets (illustrations, mockups, textures, sprites, transparent cutouts). Use when the result should be a bitmap image.
---

# imagegen

The `imagegen` skill creates and edits raster-image assets. Use it for new illustrations, mockups, textures, sprites, photo-style visuals, and transparent-background cutouts.

It is appropriate when the requested result is a bitmap image. For an existing SVG, a logo system, or a visual better expressed in HTML, CSS, or LaTeX, edit the source asset or code instead.

## Workflow

1. Confirm the user wants a **raster** image (PNG/JPEG/WebP), not SVG or pure LaTeX drawing.
2. Write outputs under the project workspace (prefer `prism-uploads/` or `figures/`).
3. Use relative paths only in any LaTeX `\includegraphics{...}`.
4. If no images API key is configured, report that image generation is unavailable rather than inventing binary data.
