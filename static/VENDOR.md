# Vendored JavaScript — CaddyPanel

CaddyPanel serves **classic `<script src=…>` files only** (no bundler, no ES
module graph, CSP `script-src 'self'`). The reusable *holaf-lib* bricks are
therefore shipped as **pinned, flat copies** under `static/`, with their ESM
export removed so they parse as classic scripts.

> This file documents the **holaf-lib bricks**. The separate CDN libraries
> (`d3`, `chart.js`, `topojson-client`, `world-atlas`) are documented in
> [`static/vendor/VERSIONS.md`](vendor/VERSIONS.md).

---

## 1. Pinned versions (verified in the files, not guessed)

| Vendored file | Brick | Upstream version | Upstream source (holaf-lib) |
|---------------|-------|------------------|-----------------------------|
| `static/holaf-icons.js` | icons | **0.1.1** | `js/holaf-icons.js` |
| `static/holaf-fetch.js` | fetch | **0.2.0** | `js/holaf-fetch.js` |
| `static/holaf-toast.js` | toast | **0.6.0** | `js/holaf-toast.js` |
| `static/holaf-modal.js` | modal | **0.5.0** | `js/holaf-modal.js` |

The version is stated **twice** in each vendored file and must match the table:
the header line (`Holaf UI — Brique Holaf… · version X.Y.Z`) and the runtime
constant (`const VERSION = "X.Y.Z"`). The source of truth upstream is
`holaf-lib/manifest.json` (`bricks.<name>.version`).

Loaded in the pages as:

| Page | Bricks loaded (in order) |
|------|--------------------------|
| `templates/index.html` | `holaf-icons.js`, `holaf-fetch.js`, `holaf-toast.js`, `holaf-modal.js` + `script.js` |
| `templates/stats.html` | `holaf-icons.js`, `holaf-fetch.js`, `holaf-toast.js` + `stats.js` |

`holaf-modal.js` is **not** loaded on `stats.html` (that page does not use
`HolafModal`). Bricks must always be loaded **before** the page script that
uses them. This invariant is enforced by `tests/test_frontend.js`.

---

## 2. Why manual copies instead of the `scripts/holaf` CLI?

holaf-lib ships a helper CLI (`holaf-lib/scripts/holaf`, sub-commands
`install / check / upgrade / adopt`). Its convention is:

- copy into **`DEST/vendor/holaf/holaf-<brick>.js`** (a `vendor/holaf/`
  sub-directory),
- record the version in `DEST/vendor/holaf/holaf-manifest.json`,
- copy the file **verbatim** — it does **not** remove the ESM `export { … }`
  line nor add a vendoring header.

That convention targets projects that consume the bricks as ES modules
(`<script type="module">` / `import`). **CaddyPanel does not**, so the CLI is
**not applicable here as-is**:

- CaddyPanel keeps the copies **flat** in `static/` (there is no
  `static/vendor/holaf/` tree and no `holaf-manifest.json`).
- CaddyPanel loads them as **classic scripts**, which requires stripping the
  top-level ESM `export` (otherwise the file throws a `SyntaxError` and *all*
  page JS breaks).

The applicable convention is the one already used for these files: a manual,
reviewed copy with an added vendor note and the ESM export removed (§3). Do
**not** run `scripts/holaf install` against CaddyPanel — it would place the
file in the wrong directory and leave an ESM `export` in a classic script.

---

## 3. Procedure to re-vendor a brick (step by step)

Run from `CaddyPanel/`. Example for `icons`; replace `<brick>`/path accordingly.

1. **Read the intended version** in `holaf-lib/manifest.json`
   (`bricks.<brick>.version`) and confirm it is the version you want to pin.
2. **Copy the upstream source** over the vendored file:
   ```bash
   cp "../holaf-lib/js/holaf-<brick>.js" "static/holaf-<brick>.js"
   ```
3. **Add the vendoring header note.** In the copied file, replace the upstream
   paragraph
   ```
    * Fichier DUAL : module ES (export) + global window.Holaf… — se charge
    * via <script type="module"> ou `import { Holaf… }`.
   ```
   with a note stating it is a vendored CaddyPanel copy loaded as a classic
   script (see the existing `static/holaf-*.js` headers for the exact wording).
4. **Remove the top-level ESM export.** Delete the `export { … }` line(s) at
   the end of the file (for `fetch`, also the top-level
   `const HolafFetchError = …` alias that only existed for the export) and
   leave a short `// (Copie vendorée : pas d'export ESM…)` marker. The global
   `window.HolafXxx` assignment must remain.
5. **Check it parses as a classic script:**
   ```bash
   node --check static/holaf-<brick>.js
   ```
6. **Run the front-end test** (also asserts no top-level ESM in every vendored
   copy):
   ```bash
   node tests/test_frontend.js
   ```
7. **Update the version table** in §1 (and the header/`const VERSION` must
   already match the copied source).

The diff between the vendored file and its upstream source must then contain
**only** (a) the header note and (b) the removed `export`. Any other change
means the copy is stale or was hand-edited — investigate before committing.

---

## 4. Golden rules

- **Never edit a `static/holaf-*.js` by hand** to fix behaviour. Any real fix
  belongs upstream in holaf-lib (e.g. via `scripts/holaf adopt`), then gets
  re-vendored here with a version bump. Hand edits are silently lost on the
  next re-vendor and break the "vendored ↔ source" diff guarantee.
- **Never leave a top-level `import`/`export`** in a vendored file — it is a
  classic script (see the CSP `script-src 'self'`, no module graph).
- Keep each vendored file **flat in `static/`** and keep the **load order** in
  the templates (bricks first). `tests/test_frontend.js` guards both.
- Only the files listed in §1 are vendored holaf bricks. `auth-theme.js` is
  project-local, not vendored.
