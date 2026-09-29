#!/usr/bin/env python3
"""
scripts/model_renderer.py — Shared HTML generation for M@TE model pages
======================================================================
Used by:
  - ingest_models.py (generates models/index.qmd, models/tags/*, models/creators/*)
  - _extensions/mate/model-page.py (pandoc filter for per-model pages)
"""

import re

# Fallback images used when a model has no graphic or setup image.
PLACEHOLDER_IMG = "https://placehold.co/1200x500/D64000/white?text=M%40TE+Model"
PLACEHOLDER_SETUP_IMG = "https://placehold.co/1200x500/2c8ec7/white?text=Model+Setup"


def doi_badge(doi: str, href: str = "") -> str:
    """Return a two-part DOI badge anchor element.

    Produces the styled ``DOI | <identifier>`` pill used across the site.
    If ``doi`` is empty, returns an empty string.  If ``href`` is not
    supplied it is derived from ``doi`` automatically.

    Args:
        doi: Raw DOI string, either a bare identifier (``10.xxx/yyy``) or a
            full ``https://doi.org/`` URL.
        href: Optional explicit link target.  Defaults to
            ``https://doi.org/{doi}``.

    Returns:
        HTML string for the badge anchor, or ``""`` if ``doi`` is falsy.
    """
    if not doi:
        return ""
    if not href:
        href = f"https://doi.org/{doi}" if not doi.startswith("http") else doi
        doi_display = doi.replace("https://doi.org/", "")
    else:
        doi_display = doi.replace("https://doi.org/", "")
    return (
        f'<a class="badge-doi" href="{href}" onclick="event.stopPropagation()" target="_blank" rel="noopener">'
        f'<span class="badge-doi-left">DOI</span>'
        f'<span class="badge-doi-right">{doi_display}</span>'
        f"</a>"
    )


def creator_badges_html(creators: list, linked: bool = True, indent: int = 4) -> str:
    """Return HTML badge elements for a list of model creators.

    Each creator is rendered as either a linked ``<a>`` (pointing to their
    ``/creators/{slug}.html`` page) or an unlinked ``<span>``, styled with
    the ``badge-creator`` CSS class.

    Args:
        creators: List of creator dicts, each containing at least
            ``{"full_name": str}``.
        linked: If ``True`` (default), badges link to the creator page.
            Set to ``False`` for contexts where linking is not appropriate.
        indent: Number of spaces prepended to each badge element, used to
            align the HTML output with its surrounding template.

    Returns:
        Newline-joined HTML string of badge elements.  Empty string if
        ``creators`` is empty or all names are blank.
    """
    pad = " " * indent
    parts = []
    for c in creators:
        name = c["full_name"]
        if not name:
            continue
        slug = _creator_slug(name)
        if linked:
            parts.append(
                f'{pad}<a class="badge-creator" href="/creators/{slug}.html">{name}</a>'
            )
        else:
            parts.append(f'{pad}<span class="badge-creator">{name}</span>')
    return "\n".join(parts)


def tag_badges_html(tags: list, linked: bool = True, indent: int = 4) -> str:
    """Return HTML badge elements for a list of research or compute tags.

    Each tag is rendered as either a linked ``<a>`` (pointing to
    ``/tags/{slug}.html``) or an unlinked ``<span>``, styled with the
    ``badge-tag`` CSS class.

    Args:
        tags: List of tag strings.
        linked: If ``True`` (default), badges link to the tag page.
        indent: Number of spaces prepended to each badge element.

    Returns:
        Newline-joined HTML string of badge elements.  Empty string if
        ``tags`` is empty or all entries are blank.
    """
    pad = " " * indent
    parts = []
    for t in tags:
        if not t:
            continue
        slug = _tag_slug(t)
        if linked:
            parts.append(f'{pad}<a class="badge-tag" href="/tags/{slug}.html">{t}</a>')
        else:
            parts.append(f'{pad}<span class="badge-tag">{t}</span>')
    return "\n".join(parts)


def safe_doi(doi: str) -> str:
    """Normalise a DOI value to a full ``https://doi.org/`` URL.

    Args:
        doi: Bare DOI identifier or already-full URL.

    Returns:
        Full URL string, or ``""`` if ``doi`` is falsy.
    """
    if not doi:
        return ""
    if doi.startswith("http"):
        return doi
    return f"https://doi.org/{doi}"


def _to_slug(text: str) -> str:
    """Convert arbitrary text to a URL-safe lowercase slug.

    Strips leading/trailing whitespace, lowercases, removes characters that
    are not word characters, spaces, or hyphens, collapses runs of
    whitespace/underscores to a single hyphen, and collapses repeated
    hyphens.

    Args:
        text: Input string.

    Returns:
        Slugified string, e.g. ``"Ben Mather"`` → ``"ben-mather"``.
    """
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def _creator_slug(full_name: str) -> str:
    """Return the URL slug for a creator's full name."""
    return _to_slug(full_name)


def _tag_slug(tag: str) -> str:
    """Return the URL slug for a tag string."""
    return _to_slug(tag)


def render_model_page(m: dict) -> str:
    """Render the full HTML body for a single model detail page.

    Called by the pandoc filter (``_extensions/mate/model-page.py``) at
    Quarto render time.  The filter reads model data from the QMD YAML
    frontmatter, passes it here as a plain Python dict, and injects the
    returned HTML as a ``RawBlock`` replacing the document body.

    The output is a five-tab layout:

    - **Snapshot** — plain-language description + landing image or animation
    - **Science Overview** — research tags, associated publication, abstract,
      and graphic abstract image
    - **Software & Setup** — compute tags, software name/DOI, model setup image
    - **Code & Data** — dataset and model file links (NCI catalogue + DOIs)
    - **Metadata** — citation text, licence, and funders

    Args:
        m: Model data dict.  Expected keys mirror the ``model:`` YAML block
           written by ``ingest_models.py``; required keys include ``slug``,
           ``title``, ``doi``, ``abstract``, ``description``, ``creators``,
           ``tags``, ``research_tags``, ``compute_tags``, ``publication``,
           ``software``, ``graphic_abstract_url``, ``landing_image_url``,
           ``animation_url``, ``model_setup_image_url``, ``dataset_nci_url``,
           ``dataset_existing_id``, ``dataset_notes``, ``model_files_nci_url``,
           ``model_files_existing_id``, ``model_files_notes``, ``licence_url``,
            ``licence_name``, ``credit_text``, ``funders``, ``source_repo``.

    Returns:
        HTML string starting with ``<div class="model-page">`` ready to be
        embedded directly into a Quarto-rendered page.
    """
    slug = m["slug"]

    doi_raw = m["doi"]
    doi_href = safe_doi(doi_raw)
    doi_display = doi_raw.replace("https://doi.org/", "") if doi_raw else ""

    pub = m["publication"]
    pub_doi_href = safe_doi(pub["doi"])

    sw = m["software"]
    sw_doi_href = safe_doi(sw["doi"])

    creator_hdg_badges = creator_badges_html(m["creators"], linked=True, indent=4)
    rtags_html = tag_badges_html(m["research_tags"], linked=True, indent=12)
    ctags_html = tag_badges_html(m["compute_tags"], linked=True, indent=12)

    ms_url = m["model_setup_image_url"] or PLACEHOLDER_SETUP_IMG
    ms_cap = m["model_setup_image_caption"] or ""

    if m.get("animation_url"):
        anim_url = m["animation_url"]
        anim_cap = m.get("animation_caption", "")
        anim_kind = (m.get("animation_kind") or "video").lower()
        # animation_kind is decided during ingest so render time does not need
        # to probe the network or guess a MIME type.
        if anim_kind == "image":
            media_html = f"""      <div class="animation-container">
        <img src="{anim_url}"
             alt="{anim_cap or "Model animation"}"
             style="max-width:100%; border-radius:6px;"
             onerror="this.style.display='none'" />
      </div>"""
        else:
            media_html = f"""      <div class="animation-container">
        <video controls autoplay loop muted playsinline style="max-width:100%; border-radius:6px;">
          <source src="{anim_url}" />
          Your browser does not support video playback.
        </video>
      </div>"""
        if anim_cap:
            media_html += f'\n      <p style="font-size:13px;color:#777;text-align:center;margin-top:0.25rem;">{anim_cap}</p>'
    else:
        li_url = m["landing_image_url"] or PLACEHOLDER_IMG
        li_cap = m["landing_image_caption"] or ""
        media_html = f"""      <div class="full-width-image">
        <img src="{li_url}"
             alt="{li_cap}"
             style="width:100%; border-radius:6px;"
             onerror="this.src='{PLACEHOLDER_IMG}';" />
      </div>"""
        if li_cap:
            media_html += f'\n      <p style="font-size:13px;color:#777;text-align:center;margin-top:0.25rem;">{li_cap}</p>'

    if pub["authors"]:
        pub_authors_str = ", ".join(
            a["full_name"] for a in pub["authors"] if a["full_name"]
        )
    else:
        pub_authors_str = ""

    ds_nci = m["dataset_nci_url"]
    ds_id = m["dataset_existing_id"]
    ds_notes = m["dataset_notes"]
    mf_nci = m["model_files_nci_url"]
    mf_id = m["model_files_existing_id"]
    mf_notes = m["model_files_notes"]

    lic_url = m["licence_url"]
    lic_name = m["licence_name"] or lic_url

    funder_items = "".join(
        f"    <li>{f['name']}</li>\n" for f in m["funders"] if f["name"]
    )
    funders_block = (
        "<ul>\n" + funder_items + "  </ul>" if funder_items else "<p>Not specified.</p>"
    )

    credit = m["credit_text"]
    source_repo_url = f"https://github.com/{m['source_repo']}"

    data_tab_parts = []
    meta_tab_parts = []
    if ds_nci:
        data_tab_parts.append(
            f"        <p><strong>Model input and output files (NCI catalogue):</strong><br/>"
            f'<a href="{ds_nci}" target="_blank" rel="noopener">{ds_nci}</a></p>'
        )
        # move this secction to the metadata tab
    if ds_id:
        ds_id_url = safe_doi(ds_id) if not ds_id.startswith("http") else ds_id
        meta_tab_parts.append(
            f"        <p><strong>Dataset existing identifier:</strong><br/>"
            f'<a href="{ds_id_url}" target="_blank" rel="noopener">{ds_id}</a></p>'
        )
    if ds_notes:
        data_tab_parts.append(
            f"        <p><strong>Model output data notes:</strong> {ds_notes}</p>"
        )
    if mf_id:
        mf_id_url = safe_doi(mf_id) if not mf_id.startswith("http") else mf_id
        data_tab_parts.append(
            f"        <p><strong>Model code / inputs DOI:</strong><br/>"
            f'<a href="{mf_id_url}" target="_blank" rel="noopener">{mf_id}</a></p>'
        )
    data_tab_parts.append(
        f"        <p><strong>M@TE artifact:</strong><br/>"
        f'<a href="{source_repo_url}" target="_blank" rel="noopener">{source_repo_url}</a></p>'
    )
    data_tab_html = (
        "\n".join(data_tab_parts)
        if data_tab_parts
        else "        <p>Data information not available.</p>"
    )
    meta_tab_html = (
        "\n".join(meta_tab_parts)
        if meta_tab_parts
        else "        <p>meta information not available.</p>"
    )
    if pub["title"]:
        pub_section = f"""        <p>
          <strong>{pub["title"]}</strong><br/>
          {pub_authors_str}<br/>
          <em>{pub["journal"]}</em>{(" — " + pub["date"]) if pub["date"] else ""}<br/>
          {doi_badge(pub["doi"], pub_doi_href) if pub["doi"] else ""}
        </p>"""
    else:
        pub_section = "        <p>Publication information not available.</p>"

    sw_block_parts = []
    if sw["name"]:
        sw_block_parts.append(f"        <p><strong>{sw['name']}</strong></p>")
    if sw["doi"] or sw["url"]:
        links = []
        if sw["doi"]:
            links.append(
                f'<a href="{sw_doi_href}" target="_blank" rel="noopener">{sw_doi_href}</a>'
            )
        if sw["url"] and sw["url"] != sw_doi_href:
            links.append(
                f'<a href="{sw["url"]}" target="_blank" rel="noopener">{sw["url"]}</a>'
            )
        sw_block_parts.append("        <p>" + " · ".join(links) + "</p>")
    sw_block = (
        "\n".join(sw_block_parts)
        if sw_block_parts
        else "        <p>Software information not available.</p>"
    )

    if doi_display:
        doi_badge_html = (
            f'    <a class="badge-doi" href="{doi_href}" target="_blank" rel="noopener">'
            f'<span class="badge-doi-left">DOI</span>'
            f'<span class="badge-doi-right">{doi_display}</span></a>'
        )
    else:
        doi_badge_html = ""

    abstract = m["abstract"]
    ga_url = m["graphic_abstract_url"]

    return f"""<div class="model-page">

  <div class="model-meta-block">
    <strong>DOI:</strong>
    {doi_badge_html if doi_badge_html else "<em>DOI not verified.</em>"}
    <br/><br/>
    <strong>Creators:</strong><br/>
{creator_hdg_badges}
  </div>

  <div class="tab-container">
    <div class="tab-nav">
      <button class="tab-btn active" onclick="switchTab(this,'snapshot')">Snapshot</button>
      <button class="tab-btn" onclick="switchTab(this,'overview')">Science Overview</button>
      <button class="tab-btn" onclick="switchTab(this,'setup')">Software &amp; Setup</button>
      <button class="tab-btn" onclick="switchTab(this,'data')">Code &amp; Data</button>
      <button class="tab-btn" onclick="switchTab(this,'meta')">Metadata</button>
    </div>

    <div id="tab-snapshot" class="tab-panel active">
      <p>{m["description"]}</p>
{media_html}
    </div>

    <div id="tab-overview" class="tab-panel">
      <h2>Research Tags</h2>
      <div>
{rtags_html if rtags_html else "        <p><em>None specified.</em></p>"}
      </div>

      <h2>Associated Publication</h2>
{pub_section}

      <h2>Abstract</h2>
      <p>{abstract}</p>
      {f'<img src="{ga_url}" alt="Graphic abstract" style="width:100%; border-radius:6px; margin-top:1rem;" onerror="this.style.display=\'none\'" />' if ga_url else ""}
    </div>

    <div id="tab-setup" class="tab-panel">
      <h2>Compute Tags</h2>
      <div>
{ctags_html if ctags_html else "        <p><em>None specified.</em></p>"}
      </div>

      <h2>Software</h2>
{sw_block}

      <h2>Model Setup</h2>
      <div class="full-width-image">
        <img src="{ms_url}"
             alt="{ms_cap}"
             style="width:100%; border-radius:6px;"
             onerror="this.src='{PLACEHOLDER_SETUP_IMG}';" />
      </div>
      {f'<p style="font-size:13px; color:#777; text-align:center; margin-top:0.25rem;">{ms_cap}</p>' if ms_cap else ""}
    </div>

    <div id="tab-data" class="tab-panel">
{data_tab_html}
    </div>

    <div id="tab-meta" class="tab-panel">
            {meta_tab_html}
      <h2>Citation</h2>
      {f"<blockquote>{credit}</blockquote>" if credit else "<p><em>See source repository for citation.</em></p>"}

      <h2>Licence</h2>
      <p>
        {f'<a href="{lic_url}" target="_blank" rel="noopener">{lic_name}</a>' if lic_url else (lic_name or "<em>Not specified.</em>")}
      </p>

      <h2>Funders</h2>
      {funders_block}
    </div>
  </div>

</div>"""


def model_card_html(m: dict) -> str:
    """Render an HTML summary card for use in model listing pages.

    Used by ``ingest_models.py`` when generating ``models/index.qmd``,
    tag pages (``models/tags/*.qmd``), and creator pages (``models/creators/*.qmd``).
    Each card is a clickable ``<a>`` element containing a thumbnail image,
    model title, up to three creator badges, up to five tag badges, and
    a DOI badge.

    The card element carries ``data-title``, ``data-tags``, and
    ``data-creators`` attributes (all lowercased) used by the client-side
    search/filter JS on the models index page.

    Args:
        m: Model data dict.  Required keys: ``slug``, ``title``, ``doi``,
           ``landing_image_url``, ``creators`` (list of dicts with
           ``full_name``), ``tags`` (list of strings).

    Returns:
        HTML string for the card ``<a>`` element.
    """
    slug = m["slug"]
    title = m["title"]
    title_lc = title.lower()
    tags_lc = " ".join(_tag_slug(t) for t in m["tags"])
    creators_lc = " ".join(c["full_name"].lower() for c in m["creators"])

    img_url = m["landing_image_url"] or PLACEHOLDER_IMG
    doi_raw = m["doi"]
    doi_href = safe_doi(doi_raw)
    doi_display = doi_raw.replace("https://doi.org/", "") if doi_raw else ""

    creator_badges = ""
    for c in m["creators"][:3]:
        name = c["full_name"]
        if name:
            cslug = _creator_slug(name)
            creator_badges += f'\n      <a class="badge-creator" href="/creators/{cslug}.html">{name}</a>'

    tag_badges = ""
    for t in m["tags"][:5]:
        if t:
            tslug = _tag_slug(t)
            tag_badges += (
                f'\n      <a class="badge-tag" href="/tags/{tslug}.html">{t}</a>'
            )

    doi_block = ""
    if doi_display:
        doi_block = f"""
      <br/>
      <a class="badge-doi" href="{doi_href}" onclick="event.stopPropagation()" target="_blank" rel="noopener">
        <span class="badge-doi-left">DOI</span>
        <span class="badge-doi-right">{doi_display}</span>
      </a>"""
    else:
        doi_block = (
            '\n      <br/><em style="font-size:12px;color:#888;">DOI not verified.</em>'
        )

    return f"""
  <div class="mc-card-container"
       data-title="{title_lc}"
       data-tags="{tags_lc}"
       data-creators="{creators_lc}">
    <a href="/models/{slug}.html" class="mc-card-link">
      <img src="{img_url}"
           alt="{title}"
           onerror="this.src='https://placehold.co/600x300/D64000/white?text=M%40TE+Model';" />
      <h3>{title}</h3>
    </a>
    <div class="mc-card-meta">{creator_badges}
      <br/>{tag_badges}{doi_block}
    </div>
  </div>"""
