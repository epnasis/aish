# Vendored frontend dependencies

No build step and no CDN at runtime: every third-party file the page loads is
checked in here and served same-origin, which is what the CSP
(`script-src 'self'`, fonts under `default-src 'self'`) permits.

## highlight.js 11.9.0

`highlight.min.js` — see the header comment inside the file (custom "common"
bundle built with esbuild). BSD-3-Clause.

## xterm.js

`xterm.js`, `xterm-addon-fit.js`, `xterm.css` — lazy-loaded when the console
opens (`ensureXterm`). MIT.

## KaTeX 0.18.7 (#391)

Renders the LaTeX a model writes for mathematics in the web transcript.

- Source: npm `katex@0.18.7`, tarball
  `https://registry.npmjs.org/katex/-/katex-0.18.7.tgz`
  (sha256 `9a80a3fba2367e99bf67b52bfff52e9534c8c7f198e4eaa12699e4f1df9a0bda`).
- Files, copied byte-for-byte from the tarball's `package/dist/`:
  - `katex.min.js` (sha256 `10a91b479cd927446ceb60409fb0d72b5d0d05eaf446c9e52fafd64058c84540`)
  - `katex.min.css` (sha256 `50d9c78e03da144a021001b7de679133355179bcb06fd11e9e309223056a03dd`)
  - `fonts/KaTeX_*.woff2` — the 20 woff2 faces only. The stylesheet lists
    woff2, woff and ttf for every face; a browser takes the FIRST format it
    supports and never requests the others, and every browser aish serves
    (iOS Safari, Chrome, Firefox) supports woff2. The woff/ttf copies would
    have tripled the footprint (1.1 MB) for a fallback no client reaches. The
    CSS is untouched, so the dangling entries are exactly the release's own.
- Fonts load on demand: the stylesheet's `@font-face` rules fetch a face the
  first time a glyph from it is drawn, so a page with no maths fetches none.
- License: MIT, (c) 2013-2020 Khan Academy and other contributors.
  https://github.com/KaTeX/KaTeX

Upgrading: replace the three groups above from the new tarball, update the
version, URL and checksums here, and re-run `tests/js/test_math_render.js`
(it loads the vendored file, so a renamed export or changed DOM shape fails
there before it reaches a browser).
