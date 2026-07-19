# Brand assets

Drop the DASHR logo here as a vector file (SVG preferred; PDF/EPS acceptable if SVG isn't available).

Suggested filename: `dashr-logo.svg`

`dashr-logo.jpg` is a raster placeholder uploaded 2026-07-19 — it's a CMYK JPEG (445x445), which some browsers/PDF renderers display with shifted colors, and it won't scale cleanly for nav/favicon/print use. Swap in a real SVG when available and this file can go away.

This is the single source of the logo, shared by:
- `apps/web` — website frontend (nav, footer, etc.), copied/imported from here rather than a second original in `apps/web/public`
- `apps/api` — invoice PDF header (`services/invoicing.py`, once that spec lands)

Keeping one canonical file here avoids the web and PDF-rendering paths drifting to different logo versions.
