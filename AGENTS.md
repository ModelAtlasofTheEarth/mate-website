# AGENTS.md — M@TE Quarto site

## Branch & CI

- Default branch: `main`. CI triggers on push here.
- CI: `.github/workflows/publish.yml` — `prefix-dev/setup-pixi@v0.9.6` → `pixi run build` → deploy to `gh-pages` **and** Netlify (via `nwtgck/actions-netlify`).
- Netlify deploys require two GitHub Actions secrets: `NETLIFY_AUTH_TOKEN` and `NETLIFY_SITE_ID`.

## Commands (pixi — never use pip/npm directly)

| Command | What it does |
|---------|-------------|
| `pixi run ingest` | Fetch model metadata from GitHub, generate `.qmd` pages |
| `pixi run render` | `quarto render` — produce HTML in `_site/` |
| `pixi run build` | **ingest → render** (order matters; use this in CI) |
| `pixi run heymate` | ingest → preview (full local dev) |
| `pixi run clean` | Wipe all generated files (`_site/`, `_freeze/`, `.quarto/`, `models/`, `tags/`, `creators/`) |

**Build order**: `ingest` must run before `render`. Prefer `pixi run build` or `pixi run heymate` to get the order right.

## Key source files

| File | Role |
|------|------|
| `scripts/ingest_models.py` | Ingest pipeline — fetches RO-Crate metadata, converts PDFs, writes model QMDs |
| `scripts/model_renderer.py` | **Single source of HTML generation** — `render_model_page()` and `model_card_html()` |
| `scripts/model-tabs.js` | `switchTab()` JS included once globally via `_quarto.yml` `include-after-body` |
| `scripts/__init__.py` | Makes `scripts/` a Python package so the pandoc filter can import from it |
| `_extensions/mate/model-page.py` | Pandoc filter — reads `model:` YAML frontmatter, calls `render_model_page()`, injects HTML |
| `_quarto.yml` | Registers `_extensions/mate/model-page.py` as a filter and `scripts/model-tabs.js` as a global include |
| `_registry.yml` | Model registry — the only file to edit when adding a model |
| `styles/mate.css` | M@TE visual design (colours, badges, tab layout) |

## How model pages work

Per-model QMDs (`models/{slug}.qmd`) contain **YAML frontmatter only** — no HTML body.
All model data lives under a `model:` key:

```yaml
---
title: "Model Title"
model:
  slug: my-model
  abstract: "..."
  description: "..."
  creators: [...]
  publication: {...}
  # ... all other fields
---
```

At render time the pandoc filter `_extensions/mate/model-page.py`:
1. Reads `doc.meta["model"]` from the parsed frontmatter
2. Converts pandoc MetaValues to plain Python with `_meta_to_python` / `_stringify`
3. Calls `scripts/model_renderer.render_model_page()` to produce the four-tab HTML
4. Replaces the empty document body with a `RawBlock` containing that HTML

To change the model page layout edit **only** `scripts/model_renderer.py`.
Never put HTML into the generated QMDs.

### `_stringify` gotchas

The filter's `_stringify` function converts pandoc inline AST elements to plain text.
Two known edge cases fixed in this codebase:
- `Space`/`SoftBreak`/`LineBreak` have `c: null` — the space check must come **before** the `if c is None: continue` guard.
- Quoted inline elements (e.g. `'word'`) have a nested inlines **list** as part of their `c` value — `isinstance(item, list)` must be handled explicitly or `str()` is called on the raw list.

## Adding a model

1. Add entry to `_registry.yml`:
   ```yaml
   models:
     - repo: ModelAtlasofTheEarth/my-model
   ```
2. Run `pixi run build` (or push to `main` — CI handles it).
3. Never commit generated files — they are gitignored.

## Generated files (all gitignored, never commit)

- `models/*.qmd`, `models/_graphics/`
- `tags/`, `creators/`
- `_site/`, `_freeze/`, `.quarto/`

## Data source

- Model metadata fetched from `raw.githubusercontent.com/{repo}/main/ro-crate-metadata.json`.
- No GitHub token needed — all source repos **must be public**.
- Graphics discovered from RO-Crate `@graph` entries (primary, June 2026+). Only the landing image is required by the submission form, so any role the crate lacks is filled from legacy `.website_material/index.{md,json}` (per-role fallback). Legacy models are never migrated, so that fallback stays.
- A missing image or animation is omitted from the model page — no placeholder is shown (placeholders remain only on model cards and the home carousel).
- Model input notes come from the crate: `model_code_inputs.description` (empty when the submitter gave none). Only legacy crates, where that description is still the template placeholder "Code and inputs for computational model", fall back to `.metadata_trail/issue_dict.json` (`model_code_inputs.notes`) via `_legacy_input_notes()`. New models must not rely on `.metadata_trail`.
- Model links (Model & Software tab) are the extra `model_code_inputs.url` entries (new crates) plus, for legacy crates, the non-M@TE-DOI `identifier` on that node (GitHub repo or Zenodo DOI), merged into one `model_links` list at ingest. The NCI catalogue link (labelled "Storage catalogue") and source repo ("M@TE artifact") are shown under "M@TE Submission" in the Metadata tab, followed by the citation. Licence and funders are still ingested into the frontmatter but not rendered.
- Software frameworks are every `#datasetCreation` instrument that is not a `Service`/`Organization` (DOI-resolved software is typed `SoftwareSourceCode`, older entries `SoftwareApplication`).
- PDF graphics auto-converted to PNG via `pdftoppm` (poppler dependency in pixi) **during ingest**; resolved URLs are stored in the YAML frontmatter so the pandoc filter never touches the filesystem.

### RO-Crate root `@id` gotcha

The RO-Crate 1.1 spec requires the root Data Entity to be addressed as `./`, and
all legacy M@TE model crates comply. The **Lu-2026 generator** instead prefixes
every `@id` (and reference) with `http://example.org/base/`. `normalise_ro_crate()`
strips that prefix first (`_strip_example_base()`), so the root becomes `./` and
ids like `#datasetCreation` resolve. Ids with a trailing slash
(`model_code_inputs/`) are also indexed without it (`_index_graph()`).

The root is then resolved via `ROOT_ID_CANDIDATES` + a heuristic fallback
(`_find_root_node()`), because every root-level field (title, abstract,
description, creators, DOI, publication, … ) is read from that single node — a
wrong root means a model page renders with all of those blank even though
graphics still appear.

**Future work:** if a new model crate comes back with empty root-level fields,
its root `@id` has changed again. Add the new value to `ROOT_ID_CANDIDATES`
(or extend `_strip_example_base()` if it is another generator prefix) in
`scripts/ingest_models.py` — never hard-code the root lookup elsewhere.

## Design tokens (M@TE theme in `styles/mate.css`)

| Token | Value |
|-------|-------|
| Primary | `#D64000` |
| Link | `#2c8ec7` |
| Navbar/hero bg | `#DAE1E3` |
| Font | Open Sans |
| Tag badges | Blue `#2c8ec7` → `/tags/{slug}.html` |
| Creator badges | Grey `#6c757d` → `/creators/{slug}.html` |
| DOI badges | Grey `#555` + blue `#007ec6` |

`styles/mate.css` is the primary theme file. `styles.css` provides additional site-wide styling on top of the cosmo Quarto theme.

## No tests

This repo has no test infrastructure.
