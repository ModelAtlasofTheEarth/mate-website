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


def resolve_many(index, refs):
    """Resolve a single RO-Crate reference or a list of references."""
    nodes = []
    for ref in always_list(refs):
        node = resolve(index, ref)
        if not node and isinstance(ref, str):
            node = index.get(ref, {})
        if node:
            nodes.append(node)
    return nodes


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


_GRAPHIC_ROLES = {
    "graphic_abstract": ("graphic_abstract_url", "graphic_abstract_caption"),
    "landing_image": ("landing_image_url", "landing_image_caption"),
    "model_setup_figure": ("model_setup_image_url", "model_setup_image_caption"),
    "animation": ("animation_url", "animation_caption"),
}


def _parse_ro_crate_graphics(crate: dict) -> dict:
    """Parse RO-Crate metadata for graphic entries (June 2026+ convention).

    Scans the RO-Crate @graph for entries whose @id ends with one of the
    four known graphic role identifiers (graphic_abstract, landing_image,
    model_setup_figure, animation).  Each such entry provides:

      path        — full download URL
      description — caption text

    Only the landing image is required by the submission form, so a crate may
    legitimately carry any subset of the four.  Roles without an entry are
    returned as empty strings.

    Args:
        crate: Parsed ``ro-crate-metadata.json``.

    Returns the standard 8-field dict plus ``animation_kind``.
    """
    result = {k: "" for k in GRAPHIC_FIELDS}
    animation_kind = ""

    for node in always_list(crate.get("@graph")):
        if not isinstance(node, dict):
            continue
        nid = _clean(node.get("@id"))
        for suffix, (url_key, cap_key) in _GRAPHIC_ROLES.items():
            if nid.endswith(suffix):
                url = _clean(node.get("path"))
                result[url_key] = url
                result[cap_key] = _clean(node.get("description"))
                if suffix == "animation" and url:
                    animation_kind = _infer_animation_kind(
                        url, _clean(node.get("encodingFormat"))
                    )
                break

    result["animation_kind"] = animation_kind
    return result


def discover_graphics(repo: str, branch: str = "main", crate: Optional[dict] = None) -> dict:
    """
    Discover model graphics for a repository.

    Strategy 1 — RO-Crate graphic entries (primary, June 2026+):
      Parse ro-crate-metadata.json for
      @graph entries whose @id ends with graphic_abstract, landing_image,
      model_setup_figure, or animation.  Uses the ``path`` field as the
      download URL and ``description`` as the caption.

    Strategy 2 — Legacy index sheet (per-role fallback):
      Any role the crate does not provide is filled from
      .website_material/index.md or .website_material/index.json.  This is
      required by every pre-June 2026 repository, and also by newer ones
      whose crate omits an optional graphic that the index sheet still has.

    Args:
        repo: GitHub repository in ``Owner/name`` form.
        branch: Branch to fetch from (default ``"main"``).
        crate: Already-fetched RO-Crate; fetched here when omitted.
    """
    if crate is None:
        crate = fetch_ro_crate(repo, branch)
    result = _parse_ro_crate_graphics(crate)

    if any(not result[url_key] for url_key, _ in _GRAPHIC_ROLES.values()):
        legacy = _parse_index_sheet(repo, branch)
        for url_key, cap_key in _GRAPHIC_ROLES.values():
            if not result[url_key] and legacy[url_key]:
                result[url_key] = legacy[url_key]
                result[cap_key] = legacy[cap_key]
                if url_key == "animation_url":
                    result["animation_kind"] = _infer_animation_kind(legacy[url_key])

    # Only the landing image is mandatory; the other roles are optional.
    if not result["landing_image_url"]:
        print(
            f"  [WARN] discover_graphics({repo.split('/')[1]}): no landing image found",
            file=sys.stderr,
        )

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


# The Lu-2026 crate generator prefixes every @id (and every reference to one)
# with this base, e.g. "http://example.org/base/" for the root and
# "http://example.org/base/#datasetCreation" for the creation activity.
EXAMPLE_BASE = "http://example.org/base/"


def _strip_example_base(value):
    """Recursively remove the accidental ``example.org/base`` prefix.

    The root (``http://example.org/base/``) becomes ``./`` as required by the
    RO-Crate 1.1 spec; every other id loses the prefix.  Mirrors
    ``normalize_rocrate_dict()`` in the model repositories' ``parse_utils.py``.
    """
    if isinstance(value, str):
        if value in (EXAMPLE_BASE, EXAMPLE_BASE.rstrip("/")):
            return "./"
        if value.startswith(EXAMPLE_BASE):
            return value[len(EXAMPLE_BASE) :]
        return value
    if isinstance(value, list):
        return [_strip_example_base(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_example_base(v) for k, v in value.items()}
    return value


def _index_graph(graph: list) -> dict:
    """Index @graph nodes by ``@id``.

    Nodes whose id carries a trailing slash (``model_code_inputs/``) are also
    registered without it, so lookups work for both crate generations.
    """
    index = {n["@id"]: n for n in graph if isinstance(n, dict) and n.get("@id")}
    for nid, node in list(index.items()):
        if nid != "./" and nid.endswith("/"):
            index.setdefault(nid.rstrip("/"), node)
    return index

# Candidate @ids for the RO-Crate root Data Entity, tried in order.
#
# The RO-Crate 1.1 spec (https://researchobject.github.io/ro-crate/) requires
# the root Data Entity to be addressed as "./".  Known generator quirks (the
# example.org prefix) are removed by _strip_example_base() before the root is
# looked up.  If a future model crate ever returns empty root-level fields
# (title, abstract, description, creators, DOI, ...), its root @id has changed
# again — add the new value here rather than hard-coding it inside
# normalise_ro_crate().
ROOT_ID_CANDIDATES = ["./"]

# Description the M@TE RO-Crate template puts on ``model_code_inputs`` when the
# submitter gave no input notes; it is not real content.
INPUT_NOTES_PLACEHOLDER = "Code and inputs for computational model"


def _legacy_input_notes(repo: str, branch: str = "main") -> str:
    """Read the model input notes from a legacy model repo's issue record.

    Crates created before the notes were mapped into the RO-Crate keep them
    only in ``.metadata_trail/issue_dict.json`` (``model_code_inputs.notes``).
    New models must not rely on that directory — their notes come from the
    crate — so this is used only for crates still carrying the placeholder
    description.  Any failure simply means "no notes".
    """
    url = (
        f"https://raw.githubusercontent.com/{repo}/{branch}"
        "/.metadata_trail/issue_dict.json"
    )
    text = fetch_raw(url, timeout=30, log_failures=False)
    if not text:
        return ""
    try:
        record = json.loads(text)
        inputs = record.get("model_code_inputs") or {}
        return _clean(inputs.get("notes"))
    except (ValueError, AttributeError):
        return ""


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
    crate = _strip_example_base(crate)
    graph = always_list(crate.get("@graph"))
    index = _index_graph(graph)
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
        if full_name:
            creators.append({"full_name": full_name})

    tags = []
    for kw in always_list(root.get("keywords")):
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
        "date": _clean(citation_node.get("datePublished")),
        "authors": pub_authors,
    }

    licence_url = _clean(root.get("license"))
    licence_name = _clean(index.get(licence_url, {}).get("name"))

    creation_node = index.get("#datasetCreation", {})
    instrument_nodes = resolve_many(index, creation_node.get("instrument"))

    # Instruments are the software frameworks plus, optionally, the compute
    # resource (a Service or Organization).  DOI-resolved software arrives
    # typed SoftwareSourceCode and older entries SoftwareApplication, so select
    # by exclusion rather than by software type.
    non_software_types = {"Service", "Organization"}
    software_nodes = [
        node
        for node in instrument_nodes
        if not non_software_types.intersection(always_list(node.get("@type")))
    ]

    software_records = []
    for instrument_node in software_nodes:
        sw_url = _first_url(instrument_node)
        sw_repo = ""
        for repo_ref in always_list(instrument_node.get("codeRepository")):
            sw_repo = _clean(repo_ref.get("@id") if isinstance(repo_ref, dict) else repo_ref)
            if sw_repo:
                break
        if not sw_url:
            sw_url = sw_repo
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
        software_records.append(
            {
                "name": _clean(instrument_node.get("name")),
                "doi": sw_doi,
                "url": sw_url,
                "repository": sw_repo,
                "version": _clean(instrument_node.get("softwareVersion")),
            }
        )

    # Preserve the old normalized shape when no software entity is present;
    # the renderer handles both a dict and a list for existing generated pages.
    if not software_records:
        software_records = [
            {"name": "", "doi": "", "url": "", "repository": "", "version": ""}
        ]

    def find_data_node(candidates: List[str]) -> dict:
        for cand in candidates:
            if cand in index:
                return index[cand]
        return {}

    model_files_node = find_data_node(["model_code_inputs", "model_inputs"])
    dataset_node = find_data_node(["model_output_data", "model_outputs"])

    dataset_nci_url = _first_url(dataset_node)

    # "Model links" supplied through the submission form are stored as extra
    # URLs on the inputs node, after the generated NCI/THREDDS catalogue URL
    # (already shown as dataset_nci_url), so that one is skipped.
    model_links: List[str] = []
    for u in always_list(model_files_node.get("url")):
        link = _clean(u.get("@id") if isinstance(u, dict) else u)
        if link and link != dataset_nci_url and "thredds.nci.org.au" not in link:
            if link not in model_links:
                model_links.append(link)

    # New crates carry the "model input usage" answer as the inputs node's
    # description (empty when the submitter gave none) and are used as-is.
    # Older crates hold the template's placeholder text instead; only those
    # fall back to the legacy .metadata_trail record.
    model_input_notes = _clean(model_files_node.get("description"))
    if model_input_notes.rstrip(".").lower() == INPUT_NOTES_PLACEHOLDER.lower():
        model_input_notes = _legacy_input_notes(repo, branch)

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

    graphics = discover_graphics(repo, branch, crate)

    return {
        "slug": out_slug,
        "title": title,
        "abstract": abstract,
        "description": description,
        "doi": doi,
        "creators": creators,
        "tags": tags,
        "publication": publication,
        "software": software_records,
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
        "model_files_existing_id": model_files_existing_id,
        "model_links": model_links,
        "model_input_notes": model_input_notes,
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


def write_tags_index(all_tags: Dict[str, List[dict]]) -> None:
    """all_tags: {tag_display_string: [model_dict, ...]}"""
    # Sort case-insensitively
    sorted_tags = sorted(all_tags.keys(), key=lambda t: t.lower())

    items = ""
    for tag in sorted_tags:
        tslug = tag_slug(tag)
        count = len(all_tags[tag])
        items += f'  <a class="badge-tag" href="/tags/{tslug}.html">{tag} <small>({count})</small></a>\n'

    sections = ""
    if items:
        sections = '<h2>Research Tags</h2>\n<div class="tag-cloud">\n' + items + "</div>\n\n"

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
                "pub_doi_href": model_renderer.safe_doi(m["publication"]["doi"]),
                "pub_doi_display": m["publication"]["doi"].replace(
                    "https://doi.org/", ""
                ),
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

    # Write tags/
    os.makedirs(TAGS_DIR, exist_ok=True)
    write_tags_index(all_tags)
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
