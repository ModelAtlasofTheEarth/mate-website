# M@TE-website — Quarto powered

A **Quarto-based static website** that serves [M@TE (Model Atlas of the Earth)](https://mate.science/). Driven by a **model ingestion pipeline** that pulls content directly from M@TE model submission repositories on GitHub.

[![Netlify Status](https://api.netlify.com/api/v1/badges/d4f730fc-398c-4226-9c25-7091734ab1e0/deploy-status)](https://app.netlify.com/projects/mate-science/deploys)

M@TE website ![source](https://github.com/ModelAtlasofTheEarth/mate-website)

---

## Quick Start, to run locally.

```bash
git clone https://github.com/ModelAtlasofTheEarth/mate-website.git
cd mate-website
pixi run heymate
```

`pixi` installs all dependencies (Python, Quarto, poppler),
fetches model metadata from GitHub, generates all pages, renders the site,
and opens a local preview in your browser.

> **Prerequisite:** [pixi](https://pixi.sh) — a cross-platform package manager.
> Install it on any platform:
> - **Linux / macOS:** `curl -fsSL https://pixi.sh/install.sh | sh`
> - **macOS (Homebrew):** `brew install pixi`
> - **Windows:** `winget install pixi` or `scoop install pixi`
> - **Any (via conda):** `conda install -c conda-forge pixi`

---

## 🗂️ Repository Structure

```
mate-website/
├── pixi.toml                    # ← Single dependency manifest (Python, Quarto, poppler)
├── _quarto.yml                  # ← M@TE site config; registers pandoc filter + global JS
├── _registry.yml                # ← MODEL REGISTRY: add a model slug+repo here
├── scripts/
│   ├── __init__.py              # ← Makes scripts/ a package (enables filter imports)
│   ├── ingest_models.py         # ← Ingest pipeline (fetches metadata + graphics)
│   ├── model_renderer.py        # ← Shared HTML generation (model pages + cards)
│   └── model-tabs.js            # ← switchTab() JS, included globally via _quarto.yml
├── _extensions/
│   └── mate/
│       └── model-page.py        # ← Pandoc filter: reads YAML frontmatter → renders HTML
├── index.qmd                    # ← Hero landing page
├── about.qmd                    # ← About M@TE page
├── contact.qmd                  # ← Contact / model submission info
├── styles/
│   └── mate.css                 # ← M@TE visual design (colours, badges, tabs)
├── images/                      # ← M@TE website images
├── models/                      # ← All ingest-generated content (gitignored)
│   ├── _graphics/               # ← Auto-generated PNGs (converted from PDFs)
│   ├── index.qmd                # ← Model listing page
│   ├── featured.json            # ← Home page carousel data
│   └── {slug}.qmd               # ← Per-model YAML frontmatter only
├── tags/                        # ← Generated tag pages (one per tag)
│   └── {tag-slug}.qmd
├── creators/                    # ← Generated creator pages (one per creator)
│   └── {creator-slug}.qmd
├── news/
│   └── index.qmd                # ← News listing page
└── .github/
    └── workflows/
        └── publish.yml          # ← CI/CD: pixi run build → gh-pages + Netlify
```

---

## 🔄 Development Workflow

### Adding a new model

1. Add an entry to `_registry.yml`:

```yaml
models:
  - repo: ModelAtlasofTheEarth/my-new-model
```

2. Run `pixi run heymate` to fetch all models' metadata and regenerate all pages locally,
   or just commit and push — CI handles everything automatically.


**All pixi tasks:**

| Command | What it does |
|---------|-------------|
| `pixi run heymate` | Ingest + preview — run a local version of M@TE website |
| `pixi run clean` | Remove all generated files to force a fresh rebuild |
| `pixi run ingest` | Fetch model metadata from GitHub, discover graphics, generate `.qmd` files |
| `pixi run render` | Render the site with Quarto to `_site/` |
| `pixi run preview` | 'render' + start a local Quarto server |
| `pixi run build` | Ingest + render (no preview) — used in CI only |

The ingest must run **before** every render to ensure model pages reflect
the latest metadata. `pixi run build` and `pixi run heymate` handle this ordering
automatically.

All generated files (`.qmd` pages, `_graphics/` PNGs, `_site/`, `_freeze/`,
`.quarto/`) are gitignored — they live only on disk during rendering and are
never committed. Run `pixi run clean` to wipe them all, then `pixi run build`
for a pristine rebuild.

---

## 🎨 Design

The M@TE design is replicated from the original Gatsby/Netlify site:

| Element | Value |
|---------|-------|
| Primary colour | `#D64000` (rust/orange-red) |
| Link colour | `#2c8ec7` (blue) |
| Navbar / hero background | `#DAE1E3` (light grey-blue) |
| Font | Open Sans Bold, sans-serif |
| Tag badges | Blue (`#2c8ec7`), link to `/tags/{slug}.html` |
| Creator badges | Grey (`#6c757d`), link to `/creators/{slug}.html` |
| DOI badges | Two-part: grey `#555` + blue `#007ec6` |

---

## 📄 Pages

| Page | File | Source | Description |
|------|------|--------|-------------|
| Home | `index.qmd` | hand-authored | Hero section + model card grid + highlights |
| Models | `models/index.qmd` | generated | Searchable/filterable model listing |
| Model detail | `models/{slug}.qmd` | generated | YAML frontmatter only — HTML rendered at build time by pandoc filter |
| Tags | `tags/index.qmd` | generated | (Hidden) Tag cloud browse page |
| Tag detail | `tags/{tag}.qmd` | generated | All models sharing a tag |
| Creators | `creators/index.qmd` | generated | (Hidden) A–Z creator listing |
| Creator detail | `creators/{creator}.qmd` | generated | All models by a creator |
| News | `news/index.qmd` | hand-authored | News listing placeholder |
| About | `about.qmd` | hand-authored | What M@TE is and how it works |
| Contact | `contact.qmd` | hand-authored | Model submission info |

### How model detail pages are rendered

The per-model QMDs (`models/{slug}.qmd`) contain **no HTML body** — only a YAML
frontmatter block with all model data nested under a `model:` key:

```yaml
---
title: "My Model Title"
model:
  slug: my-model
  abstract: "..."
  description: "..."
  creators: [...]
  publication: {...}
  # ... all other fields
---
```

At render time, the pandoc filter `_extensions/mate/model-page.py` intercepts
each document, reads the `model:` metadata, calls
`scripts/model_renderer.render_model_page()`, and replaces the empty document
body with the full four-tab HTML layout.  The tab-switching JS
(`scripts/model-tabs.js`) is injected once globally via `_quarto.yml` rather
than being duplicated in each page.

This separation means:
- Model data is **readable and diffable** in the `.qmd` files (plain YAML, ~130 lines each)
- All HTML generation logic lives in **one place** (`scripts/model_renderer.py`)
- Adding or changing the page layout requires editing only `model_renderer.py`, not every generated file

---

## 🚀 Deployment

Deployment is fully automatic via GitHub Actions (GHA). The `publish.yml` workflow builds the
site once with pixi and then deploys the output to Netlify. Netlify *never* triggers a rebuild.

See **[.github/workflows/publish.yml](.github/workflows/publish.yml)** for the full workflow.

### Algorithm

1. Push to the `main` branch
2. GitHub Actions runs `prefix-dev/setup-pixi` (installs pixi and all
   dependencies from `pixi.toml` — Python, Quarto, poppler — with caching)
3. `pixi run build` fetches model metadata from GitHub, generates all pages,
   PDF thumbnails, and renders the full site to `_site/`
4. `nwtgck/actions-netlify` pushes `_site/` to the `Netlify'
5. `Netlify` serves it at **https://mate.science**

#### Required secrets

Add these in **GitHub → repo Settings → Secrets and variables → Actions**:

| Secret | Where to get it |
|--------|----------------|
| `NETLIFY_AUTH_TOKEN` | Netlify UI → User settings → Personal access tokens → New token |
| `NETLIFY_SITE_ID` | Netlify UI → Site → Site configuration → Site ID (a UUID) |

