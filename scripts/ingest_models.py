#!/usr/bin/env python3
"""
scripts/ingest_models.py — M@TE model ingestion pipeline
=========================================================
Reads _registry.yml, fetches each model's root-level
ro-crate-metadata.json from GitHub, normalises the
data into a common schema, then generates:

  models/{slug}.qmd          — detailed model page (tabbed layout)
  models/index.qmd           — model listing page (real cards)
  tags/index.qmd             — tag cloud browse page
  tags/{tag-slug}.qmd        — one page per unique tag
  creators/index.qmd         — A–Z creator listing
  creators/{creator-slug}.qmd — one page per unique creator

Run this script from the repository root before `quarto render`:
  python scripts/ingest_models.py

No GitHub token is required — all source repos must be public.
"""

import json
from datetime import datetime, timezone
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Set, Tuple

import requests
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from scripts import model_renderer

# ---------------------------------------------------------------------------
# Paths (relative to repo root)
# ---------------------------------------------------------------------------
REGISTRY_PATH = os.path.join(REPO_ROOT, "_registry.yml")
GENERATED_DIR = os.path.join(REPO_ROOT, "generated")
MODELS_DIR = os.path.join(REPO_ROOT, "models")
TAGS_DIR = os.path.join(REPO_ROOT, "tags")
CREATORS_DIR = os.path.join(REPO_ROOT, "creators")
INGEST_LOG_PATH = os.path.join(REPO_ROOT, ".ingest.log")

# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------


def to_slug(text: str) -> str:
    """Convert arbitrary text to a URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def creator_slug(full_name: str) -> str:
    return to_slug(full_name)


def tag_slug(tag: str) -> str:
    return to_slug(tag)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------


def parse_registry(path: str) -> List[dict]:
    """
    Parse _registry.yml using the standard PyYAML library.

    Expected schema:

      models:
        - repo: ModelAtlasofTheEarth/slug-name

    Returns a list of dicts, each with keys ``repo`` and ``slug`` (derived from
    the repo name).
    """
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    entries: List[dict] = []
    for item in data.get("models", []):
        repo = item["repo"]
        slug = repo.split("/")[1] if "/" in repo else repo
        branch = item.get("branch", "main")
        entries.append({"repo": repo, "slug": slug, "branch": branch})
    return entries


def fetch_raw(
    url: str,
    timeout: int = 30,
    max_retries: int = 3,
    log_failures: bool = True,
    as_bytes: bool = False,
) -> Optional[str | bytes]:
    """Fetch URL content with exponential backoff retry.

    Args:
        url: URL to fetch.
        timeout: Request timeout in seconds.
        max_retries: Maximum number of retry attempts when *log_failures* is
            True.  When *log_failures* is False only one attempt is made.
        log_failures: If True (default), print warnings for non-200 status
            codes and exceptions, and retry on failure.  Set to False for
            optional probes where a 404 is expected — in this mode only
            one attempt is made and no warnings are printed.
        as_bytes: If False (default), returns the response body as decoded
            text (``r.text``).  Set to True to return raw bytes
            (``r.content``), e.g. for downloading binary files such as PDFs
            for conversion to PNG.

    Returns:
        Response body as ``str`` or ``bytes``, or ``None`` if all attempts
        failed.
    """
    attempts = max_retries if log_failures else 1
    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.content if as_bytes else r.text
            if log_failures:
                print(
                    f"  WARNING: fetch attempt {attempt}/{attempts} for {url}: "
                    f"HTTP {r.status_code}",
                    file=sys.stderr,
                )
        except Exception as exc:
            if log_failures:
                print(
                    f"  WARNING: fetch attempt {attempt}/{attempts} failed for {url}: {exc}",
                    file=sys.stderr,
                )
        if attempt < attempts:
            delay = 2**attempt
            print(f"  Retrying in {delay}s...", file=sys.stderr)
            time.sleep(delay)
    return None


def fetch_ro_crate(repo: str, branch: str = "main") -> dict:
    """
    Fetch RO-Crate metadata from repository root for a model repository.

    Args:
        repo: GitHub repository in ``Owner/name`` form.
        branch: Branch to fetch from (default ``"main"``).  If the fetch
            fails the error is raised immediately — no fallback to ``main``.
    """
    ro_file = "ro-crate-metadata.json"
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{ro_file}"
    text = fetch_raw(url)
    if not text:
        raise RuntimeError(f"Could not fetch {ro_file} for {repo} on branch '{branch}'")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON in {ro_file} for {repo} on branch '{branch}': {exc}"
        ) from exc


def validate_doi(doi: str, timeout: int = 10) -> bool:
    """Check whether a DOI resolves via the DOI resolver.

    Sends a HEAD request to ``https://doi.org/{doi}`` and returns ``True``
    for HTTP 200 or 302 responses.  Returns ``False`` for 404, other
    error codes, or any network/timeout exception.

    Args:
        doi: Bare DOI identifier (e.g. ``10.25914/whbg-hd74``).  If empty,
            returns ``False`` immediately.
        timeout: Request timeout in seconds.

    Returns:
        ``True`` if the DOI resolves, ``False`` otherwise.
    """
    if not doi:
        return False
    url = f"https://doi.org/{doi}"
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True)
        return r.status_code in (200, 302)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _clean(val, default=""):
    if val is None:
        return default
    return str(val).strip()


def always_list(val):
    if val is None or val == "":
        return []
    if isinstance(val, list):
        return val
    return [val]


def resolve(index, ref):
    if isinstance(ref, dict) and ref.get("@id"):
        return index.get(ref["@id"], {})
    return {}


def get_name(node):
    if not isinstance(node, dict):
        return ""
    given = node.get("givenName", "")
    if isinstance(given, list):
        given = given[0] if given else ""
    family = node.get("familyName", "")
    if isinstance(family, list):
        family = family[0] if family else ""
    full = f"{_clean(given)} {_clean(family)}".strip()
    if full:
        return full
    name_val = node.get("name")
    if isinstance(name_val, list):
        return _clean(name_val[0]) if name_val else ""
    return _clean(name_val)


def _strip_doi_prefix(value: str) -> str:
    val = _clean(value)
    prefixes = (
        "https://doi.org/",
        "http://doi.org/",
        "http://dx.doi.org/",
        "doi:",
    )
    for p in prefixes:
        if val.lower().startswith(p):
            return val[len(p) :]
    return val


def _is_doi_like(value: str) -> bool:
    low = _clean(value).lower()
    return (
        low.startswith("10.")
        or low.startswith("doi:")
        or low.startswith(
            (
                "https://doi.org/",
                "http://doi.org/",
                "http://dx.doi.org/",
            )
        )
    )


def _first_url(node: dict) -> str:
    for u in always_list(node.get("url")):
        if isinstance(u, dict):
            v = _clean(u.get("@id"))
        else:
            v = _clean(u)
        if v:
            return v
    return ""


def _first_identifier(node: dict, exclude: Optional[Set[str]] = None) -> str:
    exclude = exclude or set()
    for ident in always_list(node.get("identifier")):
        if isinstance(ident, dict):
            val = _clean(ident.get("@id"))
        else:
            val = _clean(ident)
        if not val:
            continue
        norm = _strip_doi_prefix(val).lower()
        if norm in exclude:
            continue
        return _strip_doi_prefix(val) if _is_doi_like(val) else val
    return ""


# Standard 8-field result keys for graphic discovery
GRAPHIC_FIELDS = [
    "graphic_abstract_url",
    "graphic_abstract_caption",
    "landing_image_url",
    "landing_image_caption",
    "model_setup_image_url",
    "model_setup_image_caption",
    "animation_url",
    "animation_caption",
]


def _infer_animation_kind(*values: str) -> str:
    """Infer animation media type during ingest so rendering stays offline.

    ``gif`` and image formats are returned when the source clearly points to
    a raster image; video formats fall back to ``video`` so the renderer can
    use ``<video>`` without network probing.
    """
    text = " ".join(_clean(v).lower() for v in values if _clean(v))

    if text.endswith(("png", "jpg", "jpeg", "webp", "svg", "gif")):
        return "image"
    if text.endswith(("mp4", "webm", "mov", "m4v")):
        return "video"
    return "video"


def _parse_index_sheet(repo: str, branch: str = "main") -> dict:
    """Backward-compatibility: try to parse an image mapping from legacy
    .website_material/index.md (YAML frontmatter) or .website_material/index.json.

    *Future* repositories will NOT have these index files — they will rely on
    the RO-Crate graphic entries parsed by discover_graphics() instead.
    This function exists solely to support repositories created before the
    June 2026 naming convention without requiring any changes to those
    repositories.

    Args:
        repo: GitHub repository in ``Owner/name`` form.
        branch: Branch to fetch from (default ``"main"``).

    Returns a standard 8-field dict; any field not found is left empty.
    """
    raw_base = f"https://raw.githubusercontent.com/{repo}/{branch}/.website_material"

    result = {
        "graphic_abstract_url": "",
        "graphic_abstract_caption": "",
        "landing_image_url": "",
        "landing_image_caption": "",
        "model_setup_image_url": "",
        "model_setup_image_caption": "",
        "animation_url": "",
        "animation_caption": "",
    }

    # Mapping: index field name → (url_result_key, caption_result_key)
    # Handles the naming difference between index.md (model_setup) and
    # index.json (model_setup_figure).
    INDEX_KEYS = {
        "graphic_abstract": ("graphic_abstract_url", "graphic_abstract_caption"),
        "landing_image": ("landing_image_url", "landing_image_caption"),
        "model_setup": ("model_setup_image_url", "model_setup_image_caption"),
        "model_setup_figure": ("model_setup_image_url", "model_setup_image_caption"),
        "animation": ("animation_url", "animation_caption"),
    }

    def _assign(entry: dict) -> str:
        """Extract a resolvable URL from an image entry.
        Prefers direct 'url' (GitHub attachment CDN), otherwise
        builds from 'filename' or 'src' relative to raw_base."""
        url = (entry.get("url") or "").strip()
        if url:
            return url
        fn = (entry.get("filename") or entry.get("src") or "").strip()
        if fn:
            fn = fn[2:] if fn.startswith("./") else fn
            return f"{raw_base}/{fn}"
        return ""

    # --- Try index.md (YAML frontmatter) ---
    try:
        r = fetch_raw(f"{raw_base}/index.md", timeout=30, log_failures=False)
        if r and r.startswith("---"):
            import yaml

            end = r.find("---", 3)
            if end > 0:
                fm = yaml.safe_load(r[3:end])
                if isinstance(fm, dict):
                    # Check under images: block
                    images = fm.get("images", {}) or {}
                    for img_key, entry in images.items():
                        if img_key in INDEX_KEYS and isinstance(entry, dict):
                            url_key, cap_key = INDEX_KEYS[img_key]
                            url = _assign(entry)
                            if url:
                                result[url_key] = url
                                result[cap_key] = (entry.get("caption") or "").strip()
                    # Check top-level keys (animation is top-level in index.md)
                    for img_key in INDEX_KEYS:
                        if result[INDEX_KEYS[img_key][0]]:
                            continue  # already set from images block
                        entry = fm.get(img_key)
                        if isinstance(entry, dict):
                            url_key, cap_key = INDEX_KEYS[img_key]
                            url = _assign(entry)
                            if url:
                                result[url_key] = url
                                result[cap_key] = (entry.get("caption") or "").strip()
    except Exception:
        pass

    # --- Try index.json ---
    try:
        data = fetch_raw(f"{raw_base}/index.json", timeout=30, log_failures=False)
        if data:
            import json

            parsed = json.loads(data)
            if isinstance(parsed, dict):
                for img_key, (url_key, cap_key) in INDEX_KEYS.items():
                    if result[url_key]:
                        continue  # already set by index.md
                    entry = parsed.get(img_key)
                    if isinstance(entry, dict):
                        url = _assign(entry)
                        if url:
                            result[url_key] = url
                            result[cap_key] = (entry.get("caption") or "").strip()
    except Exception:
        pass

    return result


def _parse_ro_crate_graphics(repo: str, branch: str = "main") -> dict | None:
    """Parse RO-Crate metadata for graphic entries (June 2026+ convention).

    Scans the RO-Crate @graph for entries whose @id ends with one of the
    four known graphic role identifiers (graphic_abstract, landing_image,
    model_setup_figure, animation).  Each such entry provides:

      path        — full download URL
      description — caption text

    Args:
        repo: GitHub repository in ``Owner/name`` form.
        branch: Branch to fetch from (default ``"main"``).

    Returns the standard 8-field dict only if all required graphic entries are
    present. If any required graphic role is missing, returns ``None`` so the
    caller can fall back to the legacy index sheet.
    """
    role_suffixes = {
        "graphic_abstract": ("graphic_abstract_url", "graphic_abstract_caption"),
        "landing_image": ("landing_image_url", "landing_image_caption"),
        "model_setup_figure": ("model_setup_image_url", "model_setup_image_caption"),
        "animation": ("animation_url", "animation_caption"),
    }

    result = {k: "" for k in GRAPHIC_FIELDS}
    animation_kind = "video"
    found = False

    try:
        crate_url = (
            f"https://raw.githubusercontent.com/{repo}/{branch}/ro-crate-metadata.json"
        )
        text = fetch_raw(crate_url, timeout=30)
        if text is None:
            return None

        graph = json.loads(text).get("@graph", [])
        for node in graph:
            if not isinstance(node, dict):
                continue
            nid = node.get("@id", "")
            for suffix, (url_key, cap_key) in role_suffixes.items():
                if nid.endswith(suffix):
                    url = (node.get("path") or node.get("url") or "").strip()
                    result[url_key] = url
                    result[cap_key] = (node.get("description") or "").strip()
                    if suffix == "animation":
                        animation_kind = _infer_animation_kind(
                            url,
                            node.get("encodingFormat", ""),
                        )
                    found = True
                    break
    except Exception:
        return None

    if not found:
        return None

    # Strict fallback: treat a partial RO-Crate as absent so legacy index
    # sheets can supply missing graphics such as animation.
    if any(not result[url_key] for url_key, _ in role_suffixes.values()):
        return None

    result["animation_kind"] = animation_kind
    return result


def discover_graphics(repo: str, branch: str = "main") -> dict:
    """
    Discover model graphics for a repository.

    Strategy 1 — RO-Crate graphic entries (primary, June 2026+):
      Parse ro-crate-metadata.json for
      @graph entries whose @id ends with graphic_abstract, landing_image,
      model_setup_figure, or animation.  Uses the ``path`` field as the
      download URL and ``description`` as the caption.

    Strategy 2 — Legacy index sheet (backward compatibility fallback):
      Only reached when the RO-Crate has no graphic entries (pre-June 2026
      repositories).  Parses .website_material/index.md or .website_material/
      index.json for explicit image→role mappings with captions.

    Args:
        repo: GitHub repository in ``Owner/name`` form.
        branch: Branch to fetch from (default ``"main"``).
    """
    # Strategy 1: RO-Crate graphic entries (primary)
    result = _parse_ro_crate_graphics(repo, branch)
    if result is not None:
        return result

    # Strategy 2: Legacy index sheet (fallback)

    result = _parse_index_sheet(repo, branch)

    # Warn about any graphic fields that remain empty
    url_keys = [k for k in result if k.endswith("_url")]
    missing = [
        k.replace("_url", "").replace("_", " ") for k in url_keys if not result[k]
    ]
    if missing:
        print(
            f"  [WARN] discover_graphics({repo.split('/')[1]}): "
            f"no graphic found for: {', '.join(missing)}",
            file=sys.stderr,
        )

    result["animation_kind"] = _infer_animation_kind(result.get("animation_url", ""))

    return result


def _process_graphic(url: str, slug: str, label: str) -> str:
    """Convert a PDF graphic URL to a local PNG.

    Downloads the PDF from *url*, converts the first page to PNG using
    pdftoppm, and saves to models/_graphics/{slug}_{label}.png.

    If the URL does not end with ``.pdf``, or if anything fails
    (download error, missing pdftoppm, conversion error), the original
    URL is returned unchanged so the caller's ``onerror`` placeholder
    fallback still works in the HTML.
    """
    if not url.lower().endswith(".pdf"):
        return url

    out_dir = os.path.join(MODELS_DIR, "_graphics")
    out_path = os.path.join(out_dir, f"{slug}_{label}.png")
    rel_path = f"_graphics/{slug}_{label}.png"
    if os.path.exists(out_path):
        return rel_path

    os.makedirs(out_dir, exist_ok=True)

    pdf_tmp = None
    try:
        pdf_bytes = fetch_raw(url, timeout=60, as_bytes=True)
        if pdf_bytes is None:
            return url
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            pdf_tmp = f.name

        prefix = os.path.splitext(out_path)[0]
        subprocess.run(
            ["pdftoppm", "-png", "-r", "150", pdf_tmp, prefix],
            check=True,
            capture_output=True,
            timeout=60,
        )
        # pdftoppm creates {prefix}-1.png for the first page
        page1 = f"{prefix}-1.png"
        if os.path.exists(page1) and not os.path.exists(out_path):
            os.rename(page1, out_path)
    except Exception:
        return url
    finally:
        if pdf_tmp and os.path.exists(pdf_tmp):
            os.unlink(pdf_tmp)

    return rel_path if os.path.exists(out_path) else url


# Candidate @ids for the RO-Crate root Data Entity, tried in order.
#
# The RO-Crate 1.1 spec (https://researchobject.github.io/ro-crate/) requires
# the root Data Entity to be addressed as "./".  All legacy M@TE model crates
# comply, but the Lu-2026 crate generator emits the root as
# "http://example.org/base/" instead.  If a future model crate ever returns
# empty root-level fields (title, abstract, description, creators, DOI, ...),
# its root @id has changed again — add the new value here rather than
# hard-coding it inside normalise_ro_crate().
ROOT_ID_CANDIDATES = [
    "./",  # RO-Crate 1.1 spec (all legacy M@TE model crates)
    "http://example.org/base/",  # Lu-2026 generator convention
]


def _find_root_node(index: dict) -> dict:
    """Resolve the RO-Crate root Data Entity from the @graph ``index``.

    Tries the known root @ids from ``ROOT_ID_CANDIDATES`` first, then falls
    back to a heuristic scan: the root is the Dataset-typed node that carries
    model-level metadata (a ``name`` plus an ``abstract`` or ``description``),
    which child datasets (``.metadata_trail``, ``model_outputs``, ...) never
    have.  Returns ``{}`` if nothing matches, matching the old
    ``index.get("./", {})`` behaviour.
    """
    for candidate in ROOT_ID_CANDIDATES:
        if candidate in index:
            return index[candidate]
    for node in index.values():
        if not isinstance(node, dict):
            continue
        if "Dataset" not in always_list(node.get("@type")):
            continue
        if node.get("name") and (node.get("abstract") or node.get("description")):
            return node
    return {}


def normalise_ro_crate(crate: dict, slug: str, repo: str, branch: str = "main") -> dict:
    """
    Parse a RO-Crate 1.1 @graph into the common normalised schema dict.
    crate: parsed JSON (has "@graph" key)
    slug: derived from repo name (or alternateName override)
    repo: "Owner/repo-name"
    Returns the same schema dict as the old normalisers.
    """
    graph = always_list(crate.get("@graph"))
    index = {n.get("@id"): n for n in graph if isinstance(n, dict) and n.get("@id")}
    root = _find_root_node(index)

    title = _clean(root.get("name"))
    out_slug = _clean(root.get("alternateName")) or slug
    abstract = _clean(root.get("abstract"))
    description = _clean(root.get("description"))

    identifiers = []
    for ident in always_list(root.get("identifier")):
        if isinstance(ident, dict):
            val = _clean(ident.get("@id"))
        else:
            val = _clean(ident)
        if val:
            identifiers.append(val)
    doi = _strip_doi_prefix(identifiers[0]) if identifiers else ""
    dataset_doi_norm = doi.lower()

    creators = []
    for cref in always_list(root.get("creator")):
        cnode = resolve(index, cref)
        if not cnode and isinstance(cref, dict):
            cnode = cref
        full_name = get_name(cnode)
        if not full_name:
            continue
        orcid = ""
        if isinstance(cref, dict):
            orcid = _clean(cref.get("@id"))
        orcid = orcid.replace("https://orcid.org/", "").replace("http://orcid.org/", "")
        creators.append({"full_name": full_name, "orcid": orcid})

    tags = []
    for kw in always_list(root.get("keywords")):
        if isinstance(kw, dict):
            val = _clean(kw.get("name")) or _clean(kw.get("@id"))
        else:
            val = _clean(kw)
        if val:
            tags.append(val)
    if not tags:
        lower_text = f"{title} {abstract} {description}".lower()
        if "crustal root" in lower_text:
            tags.append("crustal roots")
        if "stability" in lower_text or "stable" in lower_text:
            tags.append("stability")
        if "retrogression" in lower_text:
            tags.append("retrogression")

    citation_ref = always_list(root.get("citation"))
    citation_node = resolve(index, citation_ref[0]) if citation_ref else {}
    pub_authors = []
    for aref in always_list(citation_node.get("author")):
        author_node = resolve(index, aref)
        if not author_node and isinstance(aref, dict):
            author_node = aref
        name = get_name(author_node)
        if name:
            pub_authors.append({"full_name": name})

    publication = {
        "title": _clean(citation_node.get("name")),
        "doi": _strip_doi_prefix(_clean(citation_node.get("@id"))),
        "journal": "",
        "date": _clean(citation_node.get("datePublished")),
        "authors": pub_authors,
    }

    license_ref = root.get("license")
    if isinstance(license_ref, dict):
        licence_url = _clean(license_ref.get("@id")) or _clean(license_ref.get("url"))
        licence_node = resolve(index, license_ref)
    else:
        licence_url = _clean(license_ref)
        licence_node = index.get(licence_url, {})
    licence_name = _clean(licence_node.get("name"))

    creation_ref = (
        root.get("#datasetCreation")
        or root.get("datasetCreation")
        or {"@id": "#datasetCreation"}
    )
    creation_node = resolve(index, creation_ref) or index.get("#datasetCreation", {})
    instrument_ref = creation_node.get("instrument")
    instrument_node = resolve(index, instrument_ref)
    if not instrument_node and isinstance(instrument_ref, str):
        instrument_node = index.get(instrument_ref, {})

    sw_name = _clean(instrument_node.get("name"))
    sw_url = _clean(instrument_node.get("url"))
    sw_id = _clean(instrument_node.get("@id"))
    sw_doi = ""
    if _is_doi_like(sw_id):
        sw_doi = _strip_doi_prefix(sw_id)
    else:
        match = re.search(r"zenodo\.org/records?/(\d+)", sw_id)
        if match:
            sw_doi = f"10.5281/zenodo.{match.group(1)}"
    if not sw_doi and sw_url:
        match = re.search(r"zenodo\.org/records?/(\d+)", sw_url)
        if match:
            sw_doi = f"10.5281/zenodo.{match.group(1)}"

    def find_data_node(candidates: List[str]) -> dict:
        for cand in candidates:
            if cand in index:
                return index[cand]
        for node in index.values():
            node_name = _clean(node.get("name"))
            if node_name in candidates:
                return node
        return {}

    model_files_node = find_data_node(["model_code_inputs", "model_inputs"])
    dataset_node = find_data_node(["model_output_data", "model_outputs"])

    model_files_nci_url = _first_url(model_files_node)
    dataset_nci_url = _first_url(dataset_node)

    model_files_existing_id = _first_identifier(
        model_files_node, exclude={dataset_doi_norm}
    )
    dataset_existing_id = _first_identifier(dataset_node)

    credit_text_vals = always_list(root.get("creditText"))
    credit_text = _clean(credit_text_vals[0]) if credit_text_vals else ""

    funders = []
    for fref in always_list(root.get("funder")):
        fnode = resolve(index, fref)
        if not fnode and isinstance(fref, dict):
            fnode = fref
        fname = _clean(fnode.get("name"))
        if fname:
            funders.append({"name": fname})

    graphics = discover_graphics(repo, branch)

    return {
        "slug": out_slug,
        "title": title,
        "abstract": abstract,
        "description": description,
        "doi": doi,
        "creators": creators,
        "tags": tags,
        "research_tags": tags,
        "compute_tags": [],
        "publication": publication,
        "software": {"name": sw_name, "doi": sw_doi, "url": sw_url},
        "graphic_abstract_url": graphics["graphic_abstract_url"],
        "graphic_abstract_caption": graphics["graphic_abstract_caption"],
        "landing_image_url": graphics["landing_image_url"],
        "landing_image_caption": graphics["landing_image_caption"],
        "model_setup_image_url": graphics["model_setup_image_url"],
        "model_setup_image_caption": graphics["model_setup_image_caption"],
        "animation_url": graphics["animation_url"],
        "animation_caption": graphics["animation_caption"],
        "animation_kind": graphics.get("animation_kind", "video"),
        "licence_url": licence_url,
        "licence_name": licence_name,
        "dataset_nci_url": dataset_nci_url,
        "dataset_existing_id": dataset_existing_id,
        "dataset_notes": _clean(dataset_node.get("description")),
        "model_files_nci_url": model_files_nci_url,
        "model_files_existing_id": model_files_existing_id,
        "model_files_notes": _clean(model_files_node.get("description")),
        "credit_text": credit_text,
        "funders": funders,
        "source_repo": repo,
    }


# ---------------------------------------------------------------------------
# HTML generation helpers (used by tag / creator pages)
# ---------------------------------------------------------------------------


def yaml_esc(val: str) -> str:
    """Escape a string for use in a YAML double-quoted scalar."""
    return val.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Model .qmd page generator
# ---------------------------------------------------------------------------


def write_model_qmd(m: dict, path: str) -> None:
    """Write a model QMD with all data in YAML frontmatter (no HTML body).
    The pandoc filter _extensions/mate/model-page.py renders the HTML
    at quarto render time from the ``model:`` metadata."""
    fm = {"title": m["title"], "model": {k: v for k, v in m.items() if k != "title"}}
    yaml_str = yaml.safe_dump(
        fm, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{yaml_str}---\n")


def write_models_index(models: List[dict]) -> None:
    """Write the main models page for MATE website.
    Using model_renderer for a HTML version of each model and
    injects javascipt to perform the search feature and linking to tags/creators"""

    cards_html = ""
    for m in models:
        cards_html += model_renderer.model_card_html(m)

    content = f"""---
title: "Models"
---

```{{=html}}
<style>
  h1.title, .title-block {{display: none !important; }}
</style>

<div class="people-section">

<h1>Search the Models</h1>

<div style="padding: 1rem 0;">
  <input
    class="model-search-box"
    id="model-search"
    type="text"
    placeholder="&#x1F50D; Search models by title, creator, or tag&hellip;"
    oninput="filterModels(this.value)"
  />
</div>

<div class="models-grid" id="models-grid">

{cards_html}

</div>

<p id="no-results" style="display:none; color:#888; font-size:16px;">No models matched your search.</p>

<script>
function filterModels(query) {{
  var q = query.toLowerCase().trim();
  var cards = document.querySelectorAll('#models-grid > [data-title]');
  var visible = 0;
  cards.forEach(function(card) {{
    var text = (
      (card.dataset.title || '') + ' ' +
      (card.dataset.tags  || '') + ' ' +
      (card.dataset.creators || '')
    ).toLowerCase();
    var show = !q || text.includes(q);
    card.style.display = show ? '' : 'none';
    if (show) visible++;
  }});
  document.getElementById('no-results').style.display = (visible === 0) ? '' : 'none';
}}
// Support ?q= URL parameter for deep-linking from tag/creator pages
(function() {{
  var params = new URLSearchParams(window.location.search);
  var q = params.get('q');
  if (q) {{
    var box = document.getElementById('model-search');
    if (box) {{ box.value = q; filterModels(q); }}
  }}
}})();
</script>
```
"""
    path = os.path.join(MODELS_DIR, "index.qmd")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  Wrote {path}")


# ---------------------------------------------------------------------------
# tags/ generators
# ---------------------------------------------------------------------------


def write_tags_index(
    all_tags: Dict[str, List[dict]],
    research_tag_set: Set[str],
    compute_tag_set: Set[str],
) -> None:
    """all_tags: {tag_display_string: [model_dict, ...]}"""
    # Sort case-insensitively
    sorted_tags = sorted(all_tags.keys(), key=lambda t: t.lower())

    research = [t for t in sorted_tags if tag_slug(t) in research_tag_set]
    compute = [
        t
        for t in sorted_tags
        if tag_slug(t) in compute_tag_set and tag_slug(t) not in research_tag_set
    ]
    other = [
        t
        for t in sorted_tags
        if tag_slug(t) not in research_tag_set and tag_slug(t) not in compute_tag_set
    ]

    def cloud_items(tags):
        html = ""
        for tag in tags:
            tslug = tag_slug(tag)
            count = len(all_tags[tag])
            html += f'  <a class="badge-tag" href="/tags/{tslug}.html">{tag} <small>({count})</small></a>\n'
        return html

    sections = ""
    if research:
        sections += (
            '<h2>Research Tags</h2>\n<div class="tag-cloud">\n'
            + cloud_items(research)
            + "</div>\n\n"
        )
    if compute:
        sections += (
            '<h2>Compute Tags</h2>\n<div class="tag-cloud">\n'
            + cloud_items(compute)
            + "</div>\n\n"
        )
    if other:
        sections += (
            '<h2>Other Tags</h2>\n<div class="tag-cloud">\n'
            + cloud_items(other)
            + "</div>\n\n"
        )

    content = f"""---
title: "Tags"
---

```{{=html}}
<p>Browse all tags used across M@TE models. Click a tag to see related models.</p>

{sections}
```
"""
    path = os.path.join(TAGS_DIR, "index.qmd")
    os.makedirs(TAGS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  Wrote {path}")


def write_tag_page(tag: str, models: List[dict]) -> None:
    tslug = tag_slug(tag)
    cards_html = "".join(model_renderer.model_card_html(m) for m in models)
    content = f"""---
title: "Tag: {yaml_esc(tag)}"
---

```{{=html}}
<p>Models tagged with <span class="badge-tag">{tag}</span>:</p>

<div class="models-grid">
{cards_html}
</div>
```
"""
    path = os.path.join(TAGS_DIR, f"{tslug}.qmd")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  Wrote {path}")


# ---------------------------------------------------------------------------
# creators/ generators
# ---------------------------------------------------------------------------


def write_creators_index(all_creators: Dict[str, List[dict]]) -> None:
    """all_creators: {full_name: [model_slug, ...]}"""
    sorted_names = sorted(all_creators.keys(), key=lambda n: n.split()[-1].lower())

    items_html = ""
    for name in sorted_names:
        cslug = creator_slug(name)
        count = len(all_creators[name])
        items_html += f'  <li><a href="/creators/{cslug}.html">{name}</a> <small>({count} model{"s" if count != 1 else ""})</small></li>\n'

    content = f"""---
title: "Creators"
---

```{{=html}}
<p>All model creators in the M@TE catalogue, listed alphabetically by family name.</p>

<ul class="creator-list">
{items_html}
</ul>
```
"""
    path = os.path.join(CREATORS_DIR, "index.qmd")
    os.makedirs(CREATORS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  Wrote {path}")


def write_creator_page(name: str, models: List[dict]) -> None:
    cslug = creator_slug(name)
    cards_html = "".join(model_renderer.model_card_html(m) for m in models)
    content = f"""---
title: "{yaml_esc(name)}"
---

```{{=html}}
<p>Models by <span class="badge-creator">{name}</span>:</p>

<div class="models-grid">
{cards_html}
</div>
```
"""
    path = os.path.join(CREATORS_DIR, f"{cslug}.qmd")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  Wrote {path}")


# ---------------------------------------------------------------------------
# Featured JSON generator (home page carousel)
# ---------------------------------------------------------------------------


def write_featured_json(models: List[dict]) -> None:
    """Write ``models/_featured.json`` — a lightweight JSON snapshot of every
    model used by the home-page carousel (``scripts/featured-carousel.js``).

    Each entry contains only the fields needed to render a model card in the
    carousel, keeping the file small and avoiding the full model schema.

    When ``landing_image_url`` is a local relative path (starts with
    ``_graphics/``), it is rewritten to ``models/_graphics/…`` so that the
    URL resolves correctly from the home page at the site root (``/``).

    Args:
        models: List of normalised model dicts (the same list passed to
            ``write_models_index()``).
    """
    featured = []
    for m in models:
        img_url = m["landing_image_url"] or model_renderer.PLACEHOLDER_IMG
        # Model pages at /models/{slug}.html resolve a relative _graphics/…
        # path correctly, but the home page at / needs an explicit
        # /models/ prefix so the browser finds the file.
        if img_url.startswith("_graphics/"):
            img_url = "models/" + img_url
        featured.append(
            {
                "slug": m["slug"],
                "title": m["title"],
                "img_url": img_url,
                "creators": [
                    {
                        "full_name": c["full_name"],
                        "slug": model_renderer._creator_slug(c["full_name"]),
                    }
                    for c in m["creators"]
                    if c["full_name"]
                ],
                "tags": [
                    {"name": t, "slug": model_renderer._tag_slug(t)}
                    for t in m["tags"]
                    if t
                ],
                "doi": m["doi"],
                "doi_href": model_renderer.safe_doi(m["doi"]),
                "doi_display": m["doi"].replace("https://doi.org/", "")
                if m["doi"]
                else "",
            }
        )
    path = os.path.join(MODELS_DIR, "featured.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(featured, f, indent=2)
    print(f"  Wrote {path}")


def write_ingest_log(elapsed_seconds: float) -> None:
    """Write the most recent successful ingest timing to the repo root log."""
    now = datetime.now(timezone.utc)
    line = (
        f"timestamp={int(now.timestamp())} "
        f"datetime={now.isoformat()} "
        f"elapsed={elapsed_seconds:.2f}s status=0\n"
    )
    tmp_path = f"{INGEST_LOG_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(line)
    os.replace(tmp_path, INGEST_LOG_PATH)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    ingest_started = time.time()

    # Read registry
    entries = parse_registry(REGISTRY_PATH)
    print(f"Found {len(entries)} model(s) in registry.")

    models = []
    for entry in entries:
        repo = entry["repo"]
        slug = entry["slug"]
        branch = entry["branch"]
        print(f"\nIngesting: {slug} from {repo} (branch: {branch})")
        crate = fetch_ro_crate(repo, branch)
        m = normalise_ro_crate(crate, slug, repo, branch)
        if not validate_doi(m["doi"]):
            if m["doi"]:
                print(
                    f"  WARNING: DOI does not resolve for {slug}: "
                    f"https://doi.org/{m['doi']} — model included but DOI will be marked as unverified",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  WARNING: No DOI found for {slug} — model included but DOI will be marked as unverified",
                    file=sys.stderr,
                )
            m["doi"] = ""
        models.append(m)
        print(f"  Format: ro-crate  |  title: {m['title']}")

    # Write individual model pages (YAML frontmatter, no HTML body)
    os.makedirs(MODELS_DIR, exist_ok=True)
    for m in models:
        slug = m["slug"]
        # Resolve PDF graphic URLs before writing to YAML frontmatter
        m["landing_image_url"] = _process_graphic(
            m["landing_image_url"], slug, "landing"
        )
        m["model_setup_image_url"] = _process_graphic(
            m["model_setup_image_url"], slug, "setup"
        )
        m["graphic_abstract_url"] = _process_graphic(
            m["graphic_abstract_url"], slug, "abstract"
        )

        path = os.path.join(MODELS_DIR, f"{slug}.qmd")
        write_model_qmd(m, path)
        print(f"  Wrote {path}")

    # Write models/index.qmd
    write_models_index(models)

    # Build tag index — deduplicate by slug so "Python" and "python" merge
    _tag_slug_map: Dict[str, Tuple[str, List[dict]]] = {}  # slug → (display, models)
    for m in models:
        for tag in m["tags"]:
            if tag:
                tslug = tag_slug(tag)
                if tslug not in _tag_slug_map:
                    _tag_slug_map[tslug] = (tag, [])
                _tag_slug_map[tslug][1].append(m)
    all_tags: Dict[str, List[dict]] = {
        display: ms for _slug, (display, ms) in _tag_slug_map.items()
    }

    # Track which slugs are research vs compute tags
    research_slug_set: Set[str] = set()
    compute_slug_set: Set[str] = set()
    for m in models:
        for t in m["research_tags"]:
            if t:
                research_slug_set.add(tag_slug(t))
        for t in m["compute_tags"]:
            if t:
                compute_slug_set.add(tag_slug(t))

    # Write tags/
    os.makedirs(TAGS_DIR, exist_ok=True)
    write_tags_index(all_tags, research_slug_set, compute_slug_set)
    for tag, tag_models in all_tags.items():
        write_tag_page(tag, tag_models)

    # Build creator index
    all_creators: Dict[str, List[dict]] = {}
    for m in models:
        for c in m["creators"]:
            name = c["full_name"]
            if name:
                all_creators.setdefault(name, []).append(m)

    # Write creators/
    os.makedirs(CREATORS_DIR, exist_ok=True)
    write_creators_index(all_creators)
    for name, creator_models in all_creators.items():
        write_creator_page(name, creator_models)

    # Write models/_featured.json (carousel data for the home page)
    write_featured_json(models)

    write_ingest_log(time.time() - ingest_started)
    print("\nDone. Commit the generated .qmd files and run `quarto render`.")


if __name__ == "__main__":
    main()
