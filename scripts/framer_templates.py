#!/usr/bin/env python3
"""
Monitors Framer marketplace for new templates and notifies Discord.
State is persisted in a Notion database.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone
import error_log
from shared import (
    load_dotenv,
    _should_retry,
    _parse_retry_after,
    _retry,
    _RETRY_AFTER_MAX_SECONDS,
    http_get,
    http_post,
    notion_headers,
    is_valid_iso8601_date as _is_valid_iso8601_date,
    truncate_for_notion as _truncate_for_notion,
    side_effects_enabled,
    warn_discord,
    write_summary as _write_summary,
    should_suppress_alert as _should_suppress_alert,
    record_alert_sent as _record_alert_sent,
    load_alert_state as _load_alert_state,
    save_alert_state as _save_alert_state,
)

_ALERT_STATE_PATH = 'state/alert_state-framer_templates.json'
_ALERT_SUPPRESS_MINUTES = 60


# ---------------------------------------------------------------------------
# Marketplace URLs
# ---------------------------------------------------------------------------
# The June 2026 marketplace upgrade moved the community marketplace under a
# ``/community/`` path prefix.  The old ``/marketplace/...`` URLs are no longer
# canonical: template and listing pages 301-redirect to their ``/community/``
# equivalents, but author *profile* pages now return a hard 404
# (``/marketplace/profiles/<slug>/`` → 404 while
# ``/community/marketplace/profiles/<slug>/`` → 200).  We build every link we
# post to Discord/Notion against the ``/community/`` paths directly so author
# profile links resolve and the rest don't depend on redirects that may be
# removed.  Centralised here so a future path change is a one-line edit.
_MARKETPLACE_BASE = 'https://www.framer.com/community/marketplace'
_MARKETPLACE_TEMPLATES_URL = f'{_MARKETPLACE_BASE}/templates/'
_MARKETPLACE_LISTING_URL = f'{_MARKETPLACE_TEMPLATES_URL}?sort=newest'
_MARKETPLACE_PROFILE_URL = f'{_MARKETPLACE_BASE}/profiles/'


# ---------------------------------------------------------------------------
# Fetching & parsing
# ---------------------------------------------------------------------------

def fetch_framer_templates() -> list[dict]:
    return fetch_from_rsc()


_RSC_HEADERS = {
    'Accept': 'text/x-component',
    'Rsc': '1',
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}

# Primary RSC search key followed by fallback alternatives.
# Framer's Next.js RSC stream embeds template objects under a JSON key that may
# change between Next.js or Framer releases.  If the primary key yields < 5
# results we try each fallback in order and use whichever produces the most
# templates.  The winning key is logged so a human can update the primary if
# needed.
#
# As of June 2026, Framer changed its RSC payload structure.  Each item in the
# marketplace list is now emitted as {"resource":{...}} where the resource object
# carries the template data.  Fields also changed: ``price`` and ``previewUrl``
# moved into an ``attributes`` sub-object, ``publishedUrl`` was renamed to
# ``attributes.previewUrl``, ``creator`` was renamed to ``author``, the ``$D``
# date prefix was dropped (ISO 8601 is emitted directly), and ``thumbnail`` moved
# to ``media[0].url``.  The old ``"item":`` key is kept as a fallback so we can
# automatically recover if Framer rolls the format back.
_RSC_PRIMARY_KEY = '"resource":{'
_RSC_FALLBACK_KEYS = ('"item":', '"templateItem":', '"marketplaceItem":')


def _find_candidate_rsc_keys(body: str, max_results: int = 5) -> list[str]:
    """Scan an RSC body for JSON key names that precede objects containing both
    ``"id":`` and ``"slug":`` fields.

    Used as a last-resort diagnostic when all known RSC keys produce zero results
    and zero parse errors — meaning the response format changed to use an entirely
    new key name.  Returns up to *max_results* unique candidate key strings
    (e.g. ``['"templateData":', '"listingItem":']``) that can be added as new
    fallback keys, or an empty list when no candidates are found.
    """
    candidates: list[str] = []
    seen_keys: set[str] = set()
    # Find every JSON object start in the body and look back for a quoted key
    pos = 0
    while pos < len(body):
        brace = body.find('{', pos)
        if brace == -1:
            break
        # Quick pre-check: skip objects that obviously don't have both id and slug
        chunk = body[brace:brace + 300]
        if '"id":' not in chunk or '"slug":' not in chunk:
            pos = brace + 1
            continue
        # Try to parse the object to confirm it has both fields
        try:
            obj = _extract_json_object(body, brace)
        except ValueError:
            pos = brace + 1
            continue
        if 'id' not in obj or not obj.get('slug'):
            pos = brace + 1
            continue
        # Walk backwards from brace to find the quoted key preceding it
        # Pattern: "someKey": { or "someKey":{
        look_back = body[max(0, brace - 60):brace]
        # Find the last JSON string followed by an optional colon + optional whitespace
        key_match = re.search(r'"([^"\\]{1,40})"\s*:\s*$', look_back)
        if key_match:
            key_str = f'"{key_match.group(1)}":'
            if key_str not in seen_keys:
                seen_keys.add(key_str)
                candidates.append(key_str)
                if len(candidates) >= max_results:
                    break
        pos = brace + 1
    return candidates


def _rsc_payload_type(payload: str) -> str:
    """Return a normalised type label for an RSC flight-format payload string.

    RSC payloads fall into a small number of structural types.  Using the raw
    first 4 characters as a type key is imprecise because ``I[339756,...]`` and
    ``I[837457,...]`` are both module-chunk references but would produce different
    4-char labels (``I[33`` vs ``I[83``), causing ``_sample_rsc_line_prefixes``
    to exhaust its quota on what is really a single line type.

    Recognised types and their labels:
    - ``I[...``  → ``"I["``   (module/chunk reference)
    - ``"$S...`` → ``'"$S"``  (React server component reference)
    - ``"$...``  → ``'"$"``   (other React special string)
    - ``"...``   → ``'"str"`` (plain string literal)
    - ``{...``   → ``"{"``    (inline JSON object)
    - ``[...``   → ``"["``    (JSON array)
    - anything else → first 4 characters (fallback)
    """
    if payload.startswith('I['):
        return 'I['
    if payload.startswith('"$S'):
        return '"$S"'
    if payload.startswith('"$'):
        return '"$"'
    if payload.startswith('"'):
        return '"str"'
    if payload.startswith('{'):
        return '{'
    if payload.startswith('['):
        return '['
    return payload[:4]


def _sample_rsc_line_prefixes(body: str, max_lines: int = 10) -> list[str]:
    """Return up to *max_lines* distinct RSC line-type prefixes from *body*.

    Each line in the RSC flight format starts with a numeric row index followed
    by a colon and then the line payload, e.g. ``1:"$Sreact.fragment"`` or
    ``5:I[339756,[...],...]``.  This function extracts a normalised type label
    for each unique payload type (via ``_rsc_payload_type``) so a human reader
    can quickly identify the encoding in use without reading hundreds of lines of
    raw RSC output.

    Chunk references (``I[chunk_id,...]``) are all normalised to ``I[`` so that
    the many distinct chunk IDs in a real RSC page do not exhaust the quota with
    what is really a single line type.

    Returns a list of up to *max_lines* unique strings of the form
    ``"<row>:<type>"`` — for example ``['1:"$S"', '3:I[', '5:{']``.
    An empty list is returned when *body* contains no RSC-style numbered lines.
    """
    seen: set[str] = set()
    samples: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        # RSC flight lines start with a number followed by a colon
        colon = line.find(':')
        if colon <= 0 or not line[:colon].isdigit():
            continue
        row = line[:colon]
        payload = line[colon + 1:]
        # Normalise the payload to a canonical type label so that e.g. all chunk
        # references (I[339756,...], I[837457,...]) are treated as one type.
        payload_type = _rsc_payload_type(payload)
        if payload_type not in seen:
            seen.add(payload_type)
            samples.append(f'{row}:{payload_type}')
            if len(samples) >= max_lines:
                break
    return samples


def fetch_from_rsc() -> list[dict]:
    # Framer uses Next.js RSC (React Server Components). Fetching the marketplace
    # URL with Rsc: 1 header returns a structured component stream that includes
    # all templates (including the newest) directly from the server — no JavaScript
    # execution needed. The defuddle approach missed the 1-2 newest templates because
    # they are hydrated after the initial render that defuddle captured.
    #
    # Pages are cumulative: page=2 returns items 1-24, etc.
    # We fetch up to 2 pages (24 templates) and stop early when a page adds fewer than
    # 12 new items, which means we've reached the last page.  (As of June 2026,
    # Framer's RSC payload returns 12 templates per page; the old format returned 20.)
    #
    # If page 1 succeeds but page 2 fails (network error, HTTP 5xx after retries,
    # read timeout, etc.) we keep the page-1 data and continue — there is no reason
    # to throw away 12 valid templates because of a transient failure on the second
    # page.  Only a page-1 failure is treated as fatal, since without page 1 we have
    # no data at all to compare against the seen-slugs set.  This mirrors the
    # general principle used elsewhere in the codebase (e.g. ``reddit_leads.py``
    # tolerates a subset of subreddit-feed failures and only escalates when the
    # majority fail) and means a single bad fetch cannot cost us a discovery
    # window for the newest 12 templates — exactly the ones a 15-min cron is
    # most likely to find first.
    seen: set[str] = set()
    templates: list[dict] = []
    bodies: list[str] = []
    total_parse_errors = 0

    for page in range(1, 3):
        print(f'Fetching Framer marketplace via RSC (page {page})...')
        url = _MARKETPLACE_LISTING_URL
        if page > 1:
            url += f'&page={page}'
        try:
            body = http_get(url, headers=_RSC_HEADERS)
        except Exception as exc:
            if page == 1:
                # Page 1 failure is fatal — re-raise so main() can alert.
                raise
            # Page 2 failure with valid page-1 data: log a warning and continue
            # with what we have.  Capture the HTTP response body (if available)
            # for diagnostic continuity with the rest of the codebase.
            response_body = ''
            status: int | None = None
            if isinstance(exc, urllib.error.HTTPError):
                status = exc.code
                try:
                    response_body = exc.read().decode('utf-8', errors='replace')[:500]
                except Exception:
                    pass
            ctx: dict = {
                'page': page,
                'error': str(exc),
                'page1_templates': len(templates),
            }
            if status is not None:
                ctx['status'] = status
            if response_body:
                ctx['response_body'] = response_body
            print(f'Page {page} fetch failed; continuing with {len(templates)} '
                  f'template(s) from earlier page(s): {exc}')
            error_log.log_error(
                'framer_templates', 'warning',
                f'RSC page {page} fetch failed — using earlier pages only',
                ctx,
            )
            break
        bodies.append(body)

        count_before = len(templates)
        # Primary: the embedded "data":[...] array carries the real newest-templates
        # grid (~120 items, newest-first) — the list a visitor sees under "Newest".
        # Then also scan the 12 curated "resource":{...} featured blocks; these are
        # deduplicated by slug, so this only adds any featured template not already
        # in the newest grid.  Both run every page so the legacy fallback machinery
        # below still recovers templates if Framer changes the data-array encoding.
        page_errors = _parse_rsc_data_array(body, seen, templates)
        page_errors += _parse_rsc_body(body, seen, templates, _RSC_PRIMARY_KEY)
        total_parse_errors += page_errors
        new_this_page = len(templates) - count_before

        if new_this_page < 12:
            break

    # If the primary key produced fewer than 5 results, try fallback keys across
    # all fetched pages to recover from an RSC format change automatically.
    if len(templates) < 5:
        best_key = _RSC_PRIMARY_KEY
        best_templates: list[dict] = templates
        best_parse_errors = total_parse_errors
        for fallback_key in _RSC_FALLBACK_KEYS:
            candidate_seen: set[str] = set()
            candidate_templates: list[dict] = []
            candidate_errors = 0
            for body in bodies:
                candidate_errors += _parse_rsc_body(body, candidate_seen, candidate_templates, fallback_key)
            if len(candidate_templates) > len(best_templates):
                best_templates = candidate_templates
                best_key = fallback_key
                best_parse_errors = candidate_errors
        if best_key != _RSC_PRIMARY_KEY:
            print(f'Primary RSC key yielded {len(templates)} template(s); '
                  f'fallback key {best_key!r} yielded {len(best_templates)} — using fallback.')
            error_log.log_error(
                'framer_templates', 'warning',
                f'RSC primary key {_RSC_PRIMARY_KEY!r} yielded only {len(templates)} template(s); '
                f'fallback key {best_key!r} yielded {len(best_templates)}',
                {'primary_count': len(templates), 'fallback_key': best_key,
                 'fallback_count': len(best_templates)},
            )
            templates = best_templates
            total_parse_errors = best_parse_errors

    print(f'Parsed {len(templates)} templates from RSC.')
    if len(templates) < 5:
        print(f'WARNING: only {len(templates)} templates parsed — RSC output may be incomplete.')
        last_body = bodies[-1] if bodies else ''
        ctx: dict = {
            'count': len(templates),
            'parse_errors': total_parse_errors,
            'body_preview': last_body[:500],
        }
        # When no known key matched AND no parse errors occurred, the RSC format
        # has likely changed to use a completely new key.  Scan all fetched pages
        # for JSON objects that contain both "id": and "slug": fields and log the
        # key prefixes immediately before them — this reveals the new key name so a
        # fallback entry can be added without manually inspecting raw RSC output.
        # All pages are scanned (not just the last) so that candidates are not
        # missed when page 2 happens to contain fewer template objects than page 1.
        if len(templates) == 0 and total_parse_errors == 0:
            candidate_keys: list[str] = []
            seen_candidate_keys: set[str] = set()
            for body in bodies:
                for key in _find_candidate_rsc_keys(body):
                    if key not in seen_candidate_keys:
                        seen_candidate_keys.add(key)
                        candidate_keys.append(key)
            if candidate_keys:
                ctx['candidate_keys'] = candidate_keys
            else:
                # No known or candidate JSON keys found at all — the RSC encoding
                # may have changed at a higher level (e.g. to a pure flight-format
                # line protocol with no inline JSON objects).  Log a sample of the
                # distinct RSC line-type prefixes from all fetched pages so a human
                # can immediately see the new structure without digging into raw logs.
                rsc_line_types: list[str] = _sample_rsc_line_prefixes(
                    '\n'.join(bodies), max_lines=10
                )
                if rsc_line_types:
                    ctx['rsc_line_types'] = rsc_line_types
        error_log.log_error(
            'framer_templates', 'warning',
            f'Only {len(templates)} templates parsed from RSC — format may have changed',
            ctx,
        )
    return templates


def _format_price(raw) -> str:
    """Normalise a raw RSC price into a display string with a currency symbol.

    The newest-feed data array encodes price as a number (e.g. ``79``) or ``null``
    for a free template; the featured ``"resource"`` blocks use a string (a bare
    ``"99"``, an RSC ``"$$99"`` literal-dollar form, or ``""``).  Normalise all of
    these to ``"$<n>"`` for paid templates and ``"Free"`` for free ones, so Discord,
    X, and Notion show a price with a ``$`` instead of a bare number — and ``"Free"``
    instead of a blank (the bug visible in the grouped Discord summary).
    """
    if raw is None or isinstance(raw, bool):
        return 'Free'
    if isinstance(raw, (int, float)):
        n = int(raw) if float(raw).is_integer() else raw
        return f'${n}' if n else 'Free'
    s = str(raw).strip()
    if not s:
        return 'Free'
    if s.startswith('$$'):
        # RSC encodes a literal "$" as "$$" — strip one
        s = s[1:]
    if s.startswith('$'):
        return s
    # Plain numeric string like "99" → "$99" (or "Free" when zero)
    if s.replace('.', '', 1).isdigit():
        return f'${s}' if float(s) != 0 else 'Free'
    return s


def _new_format_template(item: dict) -> dict:
    """Map a June-2026+ RSC template object to our internal template dict.

    Shared by both the embedded ``"data":[...]`` newest-feed parser
    (``_parse_rsc_data_array``) and the ``"resource":{...}`` featured-block parser
    (``_parse_rsc_body``), since both encode each template with the same field
    layout: ``introduction`` (meta title), ``author`` object, ``media[0].url``
    (thumbnail), plain-ISO ``publishedAt`` (no ``$D`` prefix), and an
    ``attributes`` sub-object carrying ``price`` and ``previewUrl``.

    ``attributes.price`` may be a number (e.g. ``39``), ``null`` (free template),
    or a string.  ``_format_price`` normalises all of these to a display string
    (``"$39"`` for paid, ``"Free"`` for free) so notifications show a currency
    symbol instead of a bare number, and a "Free" label instead of a blank.
    """
    attrs = item.get('attributes') or {}
    price = _format_price(attrs.get('price'))
    author_obj = item.get('author') or {}
    media = item.get('media') or []
    thumbnail = media[0].get('url', '') if media and isinstance(media[0], dict) else ''
    slug = item.get('slug', '') or ''
    return {
        'slug': slug,
        'title': item.get('title', '') or '',
        'meta_title': item.get('introduction', '') or '',
        'author': author_obj.get('name', '') or '',
        'author_slug': author_obj.get('slug', '') or '',
        'price': price,
        'url': f'{_MARKETPLACE_TEMPLATES_URL}{slug}/',
        'demo_url': attrs.get('previewUrl', '') or '',
        'thumbnail': thumbnail,
        'published_at': item.get('publishedAt', '') or '',
        'remixes': item.get('remixes') or 0,
    }


def _parse_rsc_data_array(body: str, seen: set, templates: list) -> int:
    """Parse template objects from embedded ``"data":[...]`` arrays in the RSC body.

    Since the June 2026 marketplace redesign, the newest-templates grid is embedded
    in the RSC stream as a client-query (SWR-style) cache: a JSON array under a
    ``"data"`` key whose elements are full template objects
    (``{"type":"template","slug":...,"author":{...},"attributes":{...},
    "publishedAt":...}``).  This array holds the *actual* newest listing
    (~120 items, newest-first) — distinct from the 12 curated ``"resource":{...}``
    featured blocks that ``_parse_rsc_body`` reads.  The pre-redesign parser keyed
    only on ``"resource":{`` and so silently missed every newly published template.

    New (deduplicated) templates are appended to ``templates`` in newest-first
    order.  Returns the number of ``"data"`` arrays that failed to parse as JSON
    (diagnostic only — a non-zero value hints at a format change).
    """
    parse_errors = 0
    pos = 0
    key = '"data":'
    while True:
        idx = body.find(key, pos)
        if idx == -1:
            break
        pos = idx + 1
        # Only consider a "data" value that is an array — skip object/scalar values.
        j = idx + len(key)
        while j < len(body) and body[j] in ' \t\r\n':
            j += 1
        if j >= len(body) or body[j] != '[':
            continue
        try:
            data = _extract_json_array(body, j)
        except ValueError:
            parse_errors += 1
            continue
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict) or item.get('type') != 'template':
                continue
            slug = item.get('slug')
            if not slug or slug in seen:
                continue
            seen.add(slug)
            templates.append(_new_format_template(item))
    return parse_errors


def _parse_rsc_body(body: str, seen: set, templates: list,
                    search_key: str = '"resource":{') -> int:
    """Parse template items from an RSC body, appending new ones to templates in-place.

    Supports two RSC payload formats:

    **New format (June 2026+):** Each item in the marketplace list is wrapped as
    ``{"resource":{...}}`` where the resource object contains ``id``, ``slug``,
    ``title``, ``introduction`` (= meta title), ``author`` (object), ``publishedAt``
    (plain ISO 8601, no ``$D`` prefix), ``media`` (array, first element is the
    thumbnail), and ``attributes`` (object containing ``price`` and ``previewUrl``).
    The search key ``'"resource":{'`` already includes the opening brace, so the
    JSON object starts at ``idx + len(search_key) - 1``.

    **Old format (pre-June 2026):** Items were emitted directly as ``"item":{...}``
    (or ``'"templateItem":``, ``'"marketplaceItem":``).  In this format the key
    ends with ``:`` and is followed by optional whitespace before the ``{``.  The
    parser detects the old format by checking whether ``search_key`` ends with
    ``'{'`` (new) or not (old).

    Returns the number of JSON parse failures (``ValueError`` from
    ``_extract_json_object``).  A non-zero value indicates that the RSC stream
    contained objects that looked like template items but could not be parsed —
    useful for diagnosing format changes without flooding the error log.
    """
    # New format: search key ends with '{' (the opening brace is part of the key)
    new_format = search_key.endswith('{')
    pos = 0
    parse_errors = 0
    while True:
        idx = body.find(search_key, pos)
        if idx == -1:
            break
        if new_format:
            # The '{' is the last character of search_key — the object starts there
            obj_start = idx + len(search_key) - 1
        else:
            # Old format: skip optional whitespace between the key colon and '{'
            obj_start = idx + len(search_key)
            while obj_start < len(body) and body[obj_start] in ' \t\r\n':
                obj_start += 1
            if obj_start >= len(body) or body[obj_start] != '{':
                pos = idx + 1
                continue
        try:
            item = _extract_json_object(body, obj_start)
            if 'id' not in item:
                # Not a template item — advance and keep searching
                pos = idx + 1
                continue
            slug = item.get('slug', '')
            if slug and slug not in seen:
                seen.add(slug)
                if new_format:
                    # New RSC format (June 2026+): fields moved into sub-objects.
                    # Shared with the embedded data-array parser so both paths map
                    # identical template objects the same way.
                    templates.append(_new_format_template(item))
                else:
                    # Old RSC format (pre-June 2026)
                    price_raw = item.get('price', '')
                    creator = item.get('creator') or {}
                    author = creator.get('name', '')
                    author_slug = creator.get('slug', '')
                    # RSC encoded Date objects with a "$D" prefix — strip it
                    published_raw = item.get('publishedAt', '')
                    published_at = (
                        published_raw[2:] if published_raw.startswith('$D')
                        else published_raw
                    )
                    thumbnail = item.get('thumbnail', '')
                    demo_url = item.get('publishedUrl', '')
                    meta_title = item.get('metaTitle', '')
                    price = _format_price(price_raw)
                    templates.append({
                        'slug': slug,
                        'title': item.get('title', ''),
                        'meta_title': meta_title,
                        'author': author,
                        'author_slug': author_slug,
                        'price': price,
                        'url': f'{_MARKETPLACE_TEMPLATES_URL}{slug}/',
                        'demo_url': demo_url,
                        'thumbnail': thumbnail,
                        'published_at': published_at,
                        'remixes': item.get('remixes') or 0,
                    })
        except ValueError:
            parse_errors += 1
        pos = idx + 1
    return parse_errors


def _extract_json_object(s: str, start: int) -> dict:
    """Extract and parse a balanced JSON object from s starting at position start."""
    depth = 0
    i = start
    in_string = False
    escape = False
    while i < len(s):
        c = s[i]
        if escape:
            escape = False
        elif c == '\\' and in_string:
            escape = True
        elif c == '"':
            in_string = not in_string
        elif not in_string:
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return json.loads(s[start:i + 1])
        i += 1
    raise ValueError('Unclosed JSON object')


def _extract_json_array(s: str, start: int):
    """Extract and parse a balanced JSON array from s starting at position start.

    Mirrors ``_extract_json_object`` but for ``[...]`` literals.  Used to lift the
    embedded ``"data":[...]`` newest-templates array out of the RSC stream.
    Returns the parsed list, or ``None`` if the array is unterminated.
    """
    depth = 0
    i = start
    in_string = False
    escape = False
    while i < len(s):
        c = s[i]
        if escape:
            escape = False
        elif c == '\\' and in_string:
            escape = True
        elif c == '"':
            in_string = not in_string
        elif not in_string:
            if c == '[':
                depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    return json.loads(s[start:i + 1])
        i += 1
    return None


# ---------------------------------------------------------------------------
# Category inference
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    'Food & Dining': ['restaurant', 'cafe', 'coffee', 'food', 'recipe', 'bakery', 'menu', 'chef', 'dining', 'pizza', 'sushi', 'bar & grill'],
    'Health & Fitness': ['fitness', 'gym', 'health', 'yoga', 'wellness', 'medical', 'clinic', 'dental', 'therapy', 'sport', 'workout', 'physio'],
    'Portfolio & Creative': ['portfolio', 'photographer', 'artist', 'creative', 'studio', 'gallery', 'personal', 'resume', 'cv'],
    'SaaS & Tech': ['saas', 'startup', 'app', 'software', 'tech', 'dashboard', 'analytics', 'ai', 'platform', 'devtool'],
    'Agency': ['agency', 'marketing', 'consulting', 'firm', 'digital agency'],
    'E-commerce & Retail': ['shop', 'store', 'ecommerce', 'e-commerce', 'fashion', 'clothing', 'jewelry', 'boutique'],
    'Real Estate': ['real estate', 'property', 'realty', 'apartment', 'housing', 'rental'],
    'Education': ['education', 'course', 'school', 'university', 'learning', 'academy', 'tutor'],
    'Blog & Magazine': ['blog', 'magazine', 'news', 'journal', 'editorial'],
    'Landing Page': ['landing', 'waitlist', 'coming soon', 'launch'],
    'Non-profit & Community': ['charity', 'nonprofit', 'non-profit', 'church', 'community', 'volunteer'],
}


def _compile_category_patterns(
    keywords_by_category: dict[str, list[str]],
) -> dict[str, list[re.Pattern]]:
    """Precompile a word-boundary regex for each category keyword.

    Plain substring matching produces false positives on short keywords:
    ``'ai'`` matches inside "ret**ai**l" and "em**ai**l", and ``'app'``
    matches inside "h**app**y" and "wr**app**er", silently miscategorising
    templates (e.g. a "Retail Store" template would be filed under
    "SaaS & Tech"). Anchoring each keyword with ``\\b`` requires it to appear
    as a whole word/phrase. Multi-word keywords such as ``'real estate'`` and
    keywords containing punctuation such as ``'e-commerce'`` keep working
    because ``\\b`` sits at the alphanumeric edges. Mirrors the word-boundary
    approach used by ``_has_word_start_phrase`` in ``reddit_leads.py``.
    """
    compiled: dict[str, list[re.Pattern]] = {}
    for category, keywords in keywords_by_category.items():
        compiled[category] = [
            re.compile(r'\b' + re.escape(kw) + r'\b') for kw in keywords
        ]
    return compiled


_CATEGORY_PATTERNS: dict[str, list[re.Pattern]] = _compile_category_patterns(CATEGORY_KEYWORDS)


def infer_category(template: dict) -> str:
    """Infer a category from the template's title and meta_title via keyword matching.

    Keywords are matched as whole words (``\\b`` anchored) so short keywords
    like ``'ai'`` and ``'app'`` do not match inside unrelated words such as
    "retail", "email", or "wrapper".
    """
    text = (template.get('title', '') + ' ' + template.get('meta_title', '')).lower()
    for category, patterns in _CATEGORY_PATTERNS.items():
        for pattern in patterns:
            if pattern.search(text):
                return category
    return 'Other'


def group_by_category(templates: list[dict]) -> dict[str, list[dict]]:
    """Group templates by inferred category, preserving CATEGORY_KEYWORDS order."""
    groups: dict[str, list[dict]] = {}
    for t in templates:
        cat = infer_category(t)
        groups.setdefault(cat, []).append(t)
    ordered: dict[str, list[dict]] = {}
    for cat in CATEGORY_KEYWORDS:
        if cat in groups:
            ordered[cat] = groups[cat]
    if 'Other' in groups:
        ordered['Other'] = groups['Other']
    return ordered


# ---------------------------------------------------------------------------
# Notion state store
# ---------------------------------------------------------------------------

def get_seen_slugs() -> set[str]:
    slugs: set[str] = set()
    cursor = None
    db_id = os.environ['NOTION_DATABASE_ID']
    while True:
        body: dict = {'page_size': 100}
        if cursor:
            body['start_cursor'] = cursor
        data = http_post(
            f'https://api.notion.com/v1/databases/{db_id}/query',
            body,
            headers=notion_headers(),
        )
        for page in data['results']:
            rt = page['properties'].get('Slug', {}).get('rich_text', [])
            if rt:
                slugs.add(rt[0]['plain_text'])
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return slugs


def save_to_notion(template: dict) -> None:
    if not side_effects_enabled():
        print(f'[observe-only] would save template to Notion: {template.get("slug", "")}')
        return
    props: dict = {
        'Name': {'title': [{'text': {'content': _truncate_for_notion(template['title'])}}]},
        'Slug': {'rich_text': [{'text': {'content': _truncate_for_notion(template['slug'])}}]},
        'URL': {'url': template['url']},
        'Author': {'rich_text': [{'text': {'content': _truncate_for_notion(template.get('author', ''))}}]},
        'Price': {'rich_text': [{'text': {'content': _truncate_for_notion(template.get('price', ''))}}]},
        'Discovered': {'date': {'start': datetime.now(timezone.utc).isoformat()}},
        'Category': {'select': {'name': infer_category(template)}},
    }
    if template.get('meta_title'):
        props['Meta Title'] = {'rich_text': [{'text': {'content': _truncate_for_notion(template['meta_title'])}}]}
    if template.get('demo_url'):
        props['Demo URL'] = {'url': template['demo_url']}
    if template.get('remixes'):
        props['Remixes'] = {'number': template['remixes']}
    published_at = template.get('published_at', '')
    if _is_valid_iso8601_date(published_at):
        props['Published'] = {'date': {'start': published_at}}
    elif published_at:
        # Date present but unparseable — log a warning and omit the field so
        # the template is still saved rather than causing a recurring Notion 400.
        # Mirrors the same defensive guard in reddit_leads.save_lead_to_notion.
        error_log.log_error(
            'framer_templates', 'warning',
            'Skipping invalid published_at for template',
            {'slug': template.get('slug', ''), 'published_at': published_at},
        )
    if template.get('thumbnail'):
        props['Thumbnail'] = {'url': template['thumbnail']}
    if template.get('author_slug'):
        props['Author URL'] = {'url': f'{_MARKETPLACE_PROFILE_URL}{template["author_slug"]}/'}

    try:
        http_post(
            'https://api.notion.com/v1/pages',
            {'parent': {'database_id': os.environ['NOTION_DATABASE_ID']}, 'properties': props},
            headers=notion_headers(),
        )
    except urllib.error.HTTPError as e:
        if e.code == 400 and 'Thumbnail' in props:
            # Thumbnail property may not exist in DB schema yet; retry without it
            notion_response = ''
            try:
                notion_response = e.read().decode('utf-8', errors='replace')[:500]
            except Exception:
                pass
            error_log.log_error(
                'framer_templates', 'warning',
                f'Notion 400 on save with Thumbnail for "{template.get("slug", "")}" — retrying without Thumbnail',
                {'slug': template.get('slug', ''), 'notion_response': notion_response},
            )
            props.pop('Thumbnail')
            http_post(
                'https://api.notion.com/v1/pages',
                {'parent': {'database_id': os.environ['NOTION_DATABASE_ID']}, 'properties': props},
                headers=notion_headers(),
            )
        else:
            raise


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

# Discord embed field limits (see Discord developer docs — "Embed Limits").
# Any field that exceeds these limits causes the webhook POST to be rejected
# with HTTP 400 ``Invalid Form Body`` — losing the notification for that
# template entirely.  Truncating defensively at build time means a
# pathological Framer template (e.g. a 300-char title) cannot break a batch.
_DISCORD_EMBED_TITLE_LIMIT = 256
_DISCORD_EMBED_DESCRIPTION_LIMIT = 4096


def _escape_md_link_text(text: str) -> str:
    """Escape characters that would break a Discord markdown link's text segment.

    Discord renders ``[TEXT](URL)`` as a clickable link with ``TEXT`` as the
    visible label.  The parser is non-greedy with ``]`` — the first unescaped
    ``]`` terminates the link text — so a template/author name containing ``]``
    (e.g. ``"Brand [Pro]"``) would render with a broken, mid-sentence link and
    the remainder of the title leaking out as plain text followed by a stray
    ``(url)``.  Backslash, ``[``, and ``]`` must be escaped with a leading
    backslash to preserve them verbatim inside the link text.  Other markdown
    metacharacters (``*``/``_``/`` ` ``/``~`` etc.) are kept unescaped here:
    Discord ignores them inside link-text in practice, and escaping them would
    visibly clutter the description for the common case of clean names.
    """
    if not text:
        return text
    return text.replace('\\', '\\\\').replace('[', r'\[').replace(']', r'\]')


def _escape_md_link_url(url: str) -> str:
    """Escape characters that would break a Discord markdown link's URL segment.

    Discord matches the URL portion of ``[TEXT](URL)`` up to the first
    unescaped ``)`` — so a URL containing ``)`` (most commonly when a Framer
    template's ``demo_url`` points at a CDN-style path with a parenthesised
    parameter) terminates the link early, leaving the rest of the URL as
    plain text.  Backslash and ``)`` are escaped with a leading backslash.
    Whitespace and other characters that should be URL-encoded are not handled
    here — Framer-issued URLs are well-formed slugs, so a defensive escape of
    just the link-terminating characters is sufficient.
    """
    if not url:
        return url
    return url.replace('\\', '\\\\').replace(')', r'\)')


def _build_embed(template: dict) -> dict:
    """Build a Discord embed dict for a single template.

    Fields are bounded to Discord's documented per-field limits so that a
    pathologically long title or meta_title cannot 400 the webhook and drop
    the notification.  The ``timestamp`` field is silently omitted when the
    stored ``published_at`` does not parse as ISO 8601 — mirrors the same
    defensive guard used by ``notify_discord_lead`` in ``reddit_leads.py``
    for the equivalent Reddit ``post_date`` field, and protects against a
    residual ``$D`` prefix left over from a future RSC format change.
    """
    author = template.get('author', 'unknown')
    author_slug = template.get('author_slug', '')
    price = template.get('price', '?')
    meta_title = template.get('meta_title', '')
    demo_url = template.get('demo_url', '')
    remixes = template.get('remixes') or 0
    if author_slug:
        # Escape ``]`` inside the link text — an author whose display name
        # contains ``[`` or ``]`` (e.g. ``"Jane [Studio]"``) would otherwise
        # terminate the markdown link early and spill ``(profile-url)`` into
        # the description as visible plain text.  ``author_slug`` is a Framer
        # slug (lowercase-alphanumeric + hyphens) so the URL segment is safe,
        # but we still defensively escape ``)`` in the URL for symmetry with
        # the demo-link path below.
        profile_url = f"{_MARKETPLACE_PROFILE_URL}{author_slug}/"
        author_text = f"[{_escape_md_link_text(author)}]({_escape_md_link_url(profile_url)})"
    else:
        author_text = author
    description = f"by {author_text} · **{price}**"
    if meta_title:
        description += f"\n{meta_title}"
    if demo_url:
        # ``demo_url`` is uncontrolled — a Framer template can point its live
        # preview at any URL.  Escape ``)`` so a parenthesised query-string
        # parameter cannot terminate the markdown link early and leak the
        # remainder of the URL into the description.
        description += f"\n[Live Demo]({_escape_md_link_url(demo_url)})"
    if remixes:
        description += f"\n{remixes} remix{'es' if remixes != 1 else ''}"
    embed: dict = {
        # Truncate to Discord's 256-char embed-title limit.  Framer template
        # titles are usually short, but the source field is uncontrolled — a
        # title approaching the limit would otherwise 400 the webhook.
        # Mirrors ``lead['title'][:256]`` already used by ``notify_discord_lead``
        # in ``reddit_leads.py``.
        'title': template['title'][:_DISCORD_EMBED_TITLE_LIMIT],
        'url': template['url'],
        'description': description[:_DISCORD_EMBED_DESCRIPTION_LIMIT],
        'color': 0x5865F2,
    }
    if template.get('thumbnail'):
        embed['image'] = {'url': template['thumbnail']}
    # Show when the template was published so Discord renders a human-readable
    # date.  Discord requires a valid ISO 8601 string here, so a malformed
    # value (e.g. a residual ``$D`` prefix from a future RSC format change, or
    # an unexpected null-style sentinel) would 400 the webhook for the whole
    # template.  Silently omit ``timestamp`` when the parse fails — matches
    # the defensive guard in ``notify_discord_lead`` in ``reddit_leads.py``.
    published_at = template.get('published_at', '')
    if published_at:
        try:
            datetime.fromisoformat(published_at)
            embed['timestamp'] = published_at
        except (ValueError, TypeError):
            pass
    return embed


def _build_summary_embed(templates: list[dict]) -> dict:
    """Build a single Discord embed summarising new templates grouped by category."""
    n = len(templates)
    noun = 'template' if n == 1 else 'templates'
    grouped = group_by_category(templates)
    lines: list[str] = []
    included = 0
    for category, items in grouped.items():
        category_header = f'**{category}**'
        lines.append(category_header)
        for t in items:
            author = t.get('author', 'unknown')
            price = t.get('price', '?')
            # Escape ``[``/``]`` in the title (link text) and ``)`` in the URL
            # so a template whose name contains ``"[Pro]"`` or whose URL
            # contains a parenthesised path cannot break the markdown link in
            # Discord — without escaping, the first unescaped ``]`` terminates
            # the link text and the remainder of the title leaks out as plain
            # text alongside a stray ``(url)``.  Author is rendered outside the
            # link, so it stays unescaped here (matching the per-template
            # ``_build_embed`` path which only escapes the author when it is
            # part of a markdown link).
            line = (
                f"- [{_escape_md_link_text(t['title'])}]"
                f"({_escape_md_link_url(t['url'])}) by {author} -- {price}"
            )
            if len('\n'.join(lines + [line])) > 3900:
                # If the category header was just added with no items under it yet,
                # remove it to avoid an orphaned header above the "... and N more" line.
                if lines and lines[-1] == category_header:
                    lines.pop()
                remaining = n - included
                lines.append(f'... and {remaining} more')
                break
            lines.append(line)
            included += 1
        else:
            lines.append('')  # blank line between categories
            continue
        break  # truncation triggered
    return {
        'title': f'{n} new Framer {noun}',
        'url': _MARKETPLACE_LISTING_URL,
        'description': '\n'.join(lines).rstrip(),
        'color': 0x5865F2,
    }


# Seconds to wait between successive Discord webhook POSTs in a single batch.
# Discord enforces tight per-route rate limits on webhook endpoints — empirical
# limits hover around 5 messages per 2s and a sliding global cap (often ~30
# messages per 60s).  Sending 20+ embeds back-to-back without pacing reliably
# trips a 429, after which ``_retry`` has to honour a server-supplied
# ``Retry-After`` (typically a few seconds) before each subsequent message —
# burning far more total time than a small proactive delay would.  Half a
# second between messages keeps us comfortably under the per-route cap while
# adding at most ~10s to a 20-template batch (a fraction of one ``Retry-After``
# stall).  Exposed as a module-level constant so tests can patch it to 0 to
# keep ``notify_discord_batch`` unit tests fast.
_DISCORD_INTER_MESSAGE_DELAY = 0.5


def notify_discord_batch(templates: list[dict]) -> None:
    """Send one Discord message per template, then a grouped summary embed.

    Individual template messages each contain a single embed with full
    details and thumbnail; the final message is a rich embed summarising
    all templates grouped by category, so the recap sits at the bottom of
    the channel where it is most useful as a quick index of the batch.

    A short ``_DISCORD_INTER_MESSAGE_DELAY`` is slept between consecutive
    webhook POSTs to stay under Discord's per-route rate limit (~5 msgs / 2s)
    on large batches.  Without this, a 20-template batch reliably triggers a
    429 and then has to honour each ``Retry-After`` on subsequent messages,
    which costs far more wall-clock time than the proactive pacing itself.
    """
    if not templates:
        return
    if not side_effects_enabled():
        print(f'[observe-only] would post {len(templates)} template(s) to Discord')
        return
    embeds = [_build_embed(t) for t in templates]
    payloads: list[dict] = [{'embeds': [embed]} for embed in embeds]
    payloads.append({'embeds': [_build_summary_embed(templates)]})
    for idx, payload in enumerate(payloads):
        if idx > 0 and _DISCORD_INTER_MESSAGE_DELAY > 0:
            # Sleep between messages (not before the first) to stay under
            # Discord's per-route rate limit.  See module-level comment above.
            time.sleep(_DISCORD_INTER_MESSAGE_DELAY)
        try:
            http_post(os.environ['DISCORD_WEBHOOK_URL_TEMPLATES'], payload)
        except urllib.error.HTTPError as e:
            # Capture the Discord API response body so an operator can
            # distinguish between a revoked webhook (401), a deleted
            # channel/webhook (404), rate-limiting (429), and a malformed-
            # payload rejection (400) — all of which otherwise log only
            # ``"HTTP Error <code>: <reason>"`` with no actionable signal.
            # Mirrors the diagnostic capture in ``post_to_x`` and
            # ``save_to_notion`` for Twitter / Notion HTTP errors.
            discord_response = ''
            try:
                discord_response = e.read().decode('utf-8', errors='replace')[:500]
            except Exception:
                pass
            titles = ', '.join(em['title'] for em in payload.get('embeds', []))
            label = titles or 'summary'
            print(f'Discord notification failed for [{label}]: {e}')
            error_log.log_error(
                'framer_templates', 'warning',
                'Discord batch notification failed',
                {
                    'label': label,
                    'status': e.code,
                    'error': str(e),
                    'discord_response': discord_response,
                },
            )
        except Exception as e:
            titles = ', '.join(em['title'] for em in payload.get('embeds', []))
            label = titles or 'summary'
            print(f'Discord notification failed for [{label}]: {e}')
            error_log.log_error(
                'framer_templates', 'warning',
                'Discord batch notification failed',
                {'label': label, 'error': str(e)},
            )


def notify_discord(template: dict) -> None:
    """Send a Discord notification for a single template (convenience wrapper)."""
    notify_discord_batch([template])


def _warn_discord(message: str, dedup_key: str | None = None,
                  suppress_minutes: int = _ALERT_SUPPRESS_MINUTES) -> None:
    warn_discord(message, 'framer_templates', _ALERT_STATE_PATH,
                 dedup_key=dedup_key, suppress_minutes=suppress_minutes)


# ---------------------------------------------------------------------------
# X / Twitter
# ---------------------------------------------------------------------------

_TWITTER_CRED_KEYS = (
    'TWITTER_API_KEY', 'TWITTER_API_SECRET',
    'TWITTER_ACCESS_TOKEN', 'TWITTER_ACCESS_TOKEN_SECRET',
)

_TWITTER_POST_URL = 'https://api.twitter.com/2/tweets'


def _percent_encode(s: str) -> str:
    """RFC 5849 percent-encoding (unreserved chars stay unencoded)."""
    return urllib.parse.quote(s, safe='')


def _oauth1_header(method: str, url: str, body_params: dict,
                   consumer_key: str, consumer_secret: str,
                   token: str, token_secret: str,
                   nonce: str | None = None,
                   timestamp: str | None = None) -> str:
    """Build an OAuth 1.0a Authorization header (HMAC-SHA1)."""
    oauth_params = {
        'oauth_consumer_key': consumer_key,
        'oauth_nonce': nonce or uuid.uuid4().hex,
        'oauth_signature_method': 'HMAC-SHA1',
        'oauth_timestamp': timestamp or str(int(time.time())),
        'oauth_token': token,
        'oauth_version': '1.0',
    }
    all_params = {**oauth_params, **body_params}
    param_str = '&'.join(
        f'{_percent_encode(k)}={_percent_encode(v)}'
        for k, v in sorted(all_params.items())
    )
    base_string = f'{method.upper()}&{_percent_encode(url)}&{_percent_encode(param_str)}'
    signing_key = f'{_percent_encode(consumer_secret)}&{_percent_encode(token_secret)}'
    sig = base64.b64encode(
        hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
    ).decode()
    oauth_params['oauth_signature'] = sig
    header_parts = ', '.join(
        f'{_percent_encode(k)}="{_percent_encode(v)}"'
        for k, v in sorted(oauth_params.items())
    )
    return f'OAuth {header_parts}'


def _build_tweet_text(templates: list[dict]) -> str:
    """Build a tweet summarising new templates (max 280 chars)."""
    n = len(templates)
    grouped = group_by_category(templates)
    cats = [cat for cat in grouped if cat != 'Other']
    if cats:
        cat_summary = ', '.join(cats[:3])
        if len(cats) > 3:
            cat_summary += f', and {len(cats) - 3} more'
        intro = f'{n} new Framer template{"s" if n != 1 else ""} -- spanning {cat_summary}.'
    else:
        intro = f'{n} new Framer template{"s" if n != 1 else ""} just dropped.'

    footer = '\nframer.com/community/marketplace/templates'
    # Build template lines, truncating to fit 280 chars
    lines: list[str] = []
    for t in templates:
        price = t.get('price', '?')
        lines.append(f'- {t["title"]} ({price})')

    # Assemble and truncate
    while lines:
        body = '\n'.join(lines)
        text = f'{intro}\n\n{body}\n{footer}'
        if len(text) <= 280:
            return text
        lines.pop()

    # Fallback: just intro + footer
    text = f'{intro}\n{footer}'
    if len(text) <= 280:
        return text
    # Ultra-fallback: truncate intro
    return text[:277] + '...'


def post_to_x(templates: list[dict]) -> None:
    """Post a tweet about new templates. No-op if Twitter credentials are not configured."""
    if not side_effects_enabled():
        print(f'[observe-only] would post {len(templates)} template(s) to X')
        return
    creds = {k: os.environ.get(k, '') for k in _TWITTER_CRED_KEYS}
    if not all(creds.values()):
        print('Twitter credentials not configured -- skipping X post.')
        return

    tweet_text = _build_tweet_text(templates)
    auth_header = _oauth1_header(
        'POST', _TWITTER_POST_URL, {},
        creds['TWITTER_API_KEY'], creds['TWITTER_API_SECRET'],
        creds['TWITTER_ACCESS_TOKEN'], creds['TWITTER_ACCESS_TOKEN_SECRET'],
    )
    try:
        http_post(
            _TWITTER_POST_URL,
            {'text': tweet_text},
            headers={'Authorization': auth_header},
        )
        print(f'Posted to X: {tweet_text[:80]}...')
    except urllib.error.HTTPError as e:
        # Capture the Twitter API response body so an operator can distinguish
        # between expired tokens (401), duplicate-content rejections (403),
        # rate-limiting (429), and the various other failure modes that all
        # otherwise log only ``"HTTP Error <code>: <reason>"`` with no actionable
        # signal.  Mirrors the same diagnostic pattern used by
        # ``save_to_notion`` / ``url_exists_in_notion`` / ``fetch_reddit_posts``.
        twitter_response = ''
        try:
            twitter_response = e.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        print(f'Failed to post to X: {e}')
        error_log.log_error(
            'framer_templates', 'warning',
            'Failed to post to X/Twitter',
            {
                'status': e.code,
                'error': str(e),
                'tweet_length': len(tweet_text),
                'twitter_response': twitter_response,
            },
        )
    except Exception as e:
        print(f'Failed to post to X: {e}')
        error_log.log_error(
            'framer_templates', 'warning',
            'Failed to post to X/Twitter',
            {'error': str(e), 'tweet_length': len(tweet_text)},
        )


# ---------------------------------------------------------------------------
# Save-loop short-circuit
# ---------------------------------------------------------------------------

# After this many consecutive Notion save failures with zero successes between
# them, treat Notion as broadly unhealthy and stop trying to save further
# templates.  Without this, a Notion outage during a 20-template batch burns
# ~14 s per template in retried timeouts (4 attempts x exponential backoff),
# totalling ~280 s of wasted work with no useful outcome.  Three failures with
# no successes is a strong signal that Notion is down for this run — bail out
# and let the next cron tick try again.  Mirrors the same pattern used by
# ``reddit_leads.py`` (``_CONSECUTIVE_DEDUP_FAILURE_SHORT_CIRCUIT``).
_CONSECUTIVE_SAVE_FAILURE_SHORT_CIRCUIT = 3


# Maximum number of templates to notify (Discord + X) in a single run.  All new
# templates are still persisted to Notion, but only the newest this many are
# announced — the older remainder is silently backfilled.  This protects the
# channel from a notification burst when a backlog accumulates (e.g. after an
# outage, or the first run after a parser fix that surfaces many previously
# missed templates).  The feed is newest-first, so the newest are the ones
# announced.
_MAX_NOTIFY_PER_RUN = 10


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv()

    missing = [
        k for k in ('NOTION_TOKEN', 'NOTION_DATABASE_ID', 'DISCORD_WEBHOOK_URL_TEMPLATES')
        if not os.environ.get(k)
    ]
    if missing:
        print(f'Missing required env vars: {", ".join(missing)}')
        raise SystemExit(1)

    try:
        templates = fetch_framer_templates()
    except Exception as e:
        msg = f'ERROR: fetch_framer_templates failed: {e}'
        print(msg)
        error_log.log_error(
            'framer_templates', 'error',
            'fetch_framer_templates raised an unexpected exception',
            {'error': str(e)},
        )
        _warn_discord(
            f'ERROR: framer_templates.py failed to fetch templates from Framer RSC: {e}'
            ' — Check GitHub Actions logs.',
            dedup_key='framer_templates:fetch_failed',
        )
        raise SystemExit(1)
    if len(templates) < 5:
        _warn_discord(
            f'WARNING: only {len(templates)} template(s) parsed from Framer RSC'
            ' — format may have changed. Check GitHub Actions logs.',
            dedup_key='framer_templates:few_templates_parsed',
        )
    # Wrap the seen-slugs fetch so a Notion misconfiguration (deleted DB,
    # revoked integration token, wrong NOTION_DATABASE_ID secret) surfaces
    # as a Discord alert and an error-log entry instead of crashing the
    # script silently with no operator-visible signal.  Mirrors the same
    # pattern used for fetch_framer_templates above and the dedup
    # object_not_found alerting in reddit_leads.py.
    try:
        seen_slugs = get_seen_slugs()
    except urllib.error.HTTPError as e:
        notion_response = ''
        try:
            notion_response = e.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        msg = f'ERROR: get_seen_slugs failed with HTTP {e.code}: {e}'
        print(msg)
        error_log.log_error(
            'framer_templates', 'error',
            'get_seen_slugs failed — Notion DB likely misconfigured',
            {'status': e.code, 'error': str(e), 'notion_response': notion_response},
        )
        _warn_discord(
            f'ERROR: framer_templates.py — get_seen_slugs returned HTTP {e.code}'
            f' (DB likely deleted, renamed, or no longer shared with the integration,'
            f' or NOTION_TOKEN expired/revoked). No new templates will be saved'
            f' until fixed. Check NOTION_DATABASE_ID and NOTION_TOKEN secrets.',
            dedup_key='framer_templates:seen_slugs_http_error',
        )
        raise SystemExit(1)
    except Exception as e:
        msg = f'ERROR: get_seen_slugs failed: {e}'
        print(msg)
        error_log.log_error(
            'framer_templates', 'error',
            'get_seen_slugs raised an unexpected exception',
            {'error': str(e)},
        )
        _warn_discord(
            f'ERROR: framer_templates.py — get_seen_slugs failed: {e}'
            ' — Check GitHub Actions logs.',
            dedup_key='framer_templates:seen_slugs_other_error',
        )
        raise SystemExit(1)

    new_templates = [t for t in templates if t['slug'] not in seen_slugs]
    is_first_run = len(seen_slugs) == 0

    print(f'{len(templates)} templates fetched, {len(new_templates)} new.')

    if not new_templates:
        print('Nothing new. All done.')
        _write_summary(f'## Framer Monitor\n✓ No new templates · {len(seen_slugs)} already tracked')
        return

    if is_first_run:
        print('First run — seeding DB without Discord notifications to avoid spam.')

    saved_templates: list[dict] = []
    consecutive_save_failures = 0
    save_short_circuited = False
    for template in new_templates:
        try:
            save_to_notion(template)
            saved_templates.append(template)
            consecutive_save_failures = 0
        except urllib.error.HTTPError as e:
            notion_response = ''
            try:
                notion_response = e.read().decode('utf-8', errors='replace')[:500]
            except Exception:
                pass
            print(f'Failed to save "{template["title"]}" to Notion: {e}')
            error_log.log_error(
                'framer_templates', 'error',
                f'Failed to save "{template["title"]}" to Notion',
                {'slug': template['slug'], 'error': str(e), 'notion_response': notion_response},
            )
            consecutive_save_failures += 1
            if consecutive_save_failures >= _CONSECUTIVE_SAVE_FAILURE_SHORT_CIRCUIT:
                save_short_circuited = True
                break
            continue
        except Exception as e:
            print(f'Failed to save "{template["title"]}" to Notion: {e}')
            error_log.log_error(
                'framer_templates', 'error',
                f'Failed to save "{template["title"]}" to Notion',
                {'slug': template['slug'], 'error': str(e)},
            )
            consecutive_save_failures += 1
            if consecutive_save_failures >= _CONSECUTIVE_SAVE_FAILURE_SHORT_CIRCUIT:
                save_short_circuited = True
                break
            continue
        action = 'Seeded' if is_first_run else 'Saved'
        print(f'{action}: {template["title"]}')

    if save_short_circuited:
        skipped = len(new_templates) - len(saved_templates) - consecutive_save_failures
        _warn_discord(
            f'WARNING: framer_templates.py — {consecutive_save_failures} consecutive'
            f' Notion save failures; Notion appears unreachable.'
            f' Short-circuited after saving {len(saved_templates)}/{len(new_templates)}'
            f' template(s). {skipped} template(s) skipped.'
            ' Check logs/errors.jsonl.',
            dedup_key='framer_templates:save_short_circuited',
        )
        error_log.log_error(
            'framer_templates', 'warning',
            f'Notion appears unreachable — short-circuited after {consecutive_save_failures}'
            f' consecutive save failures',
            {
                'consecutive_save_failures': consecutive_save_failures,
                'saved': len(saved_templates),
                'total_new': len(new_templates),
                'skipped': skipped,
            },
        )

    # Cap notifications to the newest _MAX_NOTIFY_PER_RUN so a backlog cannot spam
    # the channel.  saved_templates is newest-first, so the newest are announced and
    # the older remainder is silently backfilled (already persisted to Notion above).
    to_notify = saved_templates[:_MAX_NOTIFY_PER_RUN]
    backfilled = len(saved_templates) - len(to_notify)

    if not is_first_run and saved_templates:
        notify_discord_batch(to_notify)
        post_to_x(to_notify)
        if backfilled:
            print(f'Backfilled {backfilled} older template(s) to Notion without'
                  f' notifying (cap {_MAX_NOTIFY_PER_RUN}/run).')

    if is_first_run:
        print(f'Done. Seeded {len(saved_templates)} template(s).')
        _write_summary(f'## Framer Monitor\n🌱 First run — seeded {len(saved_templates)} template(s) silently')
    elif save_short_circuited or len(saved_templates) < len(new_templates):
        failed = len(new_templates) - len(saved_templates)
        print(f'Done. Saved {len(saved_templates)}/{len(new_templates)} template(s) ({failed} failed).')
        _write_summary(
            f'## Framer Monitor\n⚠️ {len(saved_templates)}/{len(new_templates)} new template(s) saved'
            f' ({failed} failed) · {len(seen_slugs)} already tracked'
        )
    elif backfilled:
        print(f'Done. Notified {len(to_notify)} newest; backfilled {backfilled} silently.')
        _write_summary(
            f'## Framer Monitor\n✨ {len(saved_templates)} new template(s) found'
            f' ({len(to_notify)} notified, {backfilled} backfilled) · {len(seen_slugs)} already tracked'
        )
    else:
        print(f'Done. Notified {len(saved_templates)} template(s).')
        _write_summary(f'## Framer Monitor\n✨ {len(saved_templates)} new template(s) found · {len(seen_slugs)} already tracked')


if __name__ == '__main__':
    main()
