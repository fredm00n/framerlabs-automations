#!/usr/bin/env python3
"""
Monitors Reddit RSS feeds for potential Framer freelance leads.

Phase 1 (this script, runs every 15 min via GitHub Actions):
  Fetch RSS → light keyword filter → dedup against Notion → save as "pending".
  No Discord notifications, no LLM reasoning.

Phase 2 (hourly dedicated Claude session on Haiku, see REDDIT_LEADS_REVIEWER.md):
  Review pending leads with reasoning → approve/reject → notify Discord.
"""
import html as _html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import error_log

# Seconds to wait between individual subreddit RSS fetches to avoid Reddit
# rate-limiting (HTTP 429) when fetching 43 feeds in rapid succession.
_INTER_FEED_DELAY = 1.5

# Mimic a real browser to avoid Reddit blocking automated requests.
_REDDIT_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'


# ---------------------------------------------------------------------------
# Subreddit categories & feed URLs
# ---------------------------------------------------------------------------

_HIRING = frozenset({
    'forhire', 'hiring', 'DesignJobs', 'freelance', 'HungryArtists', 'jobbit',
})
_DESIGN_TECH = frozenset({
    'framer', 'figma', 'webdev', 'web_design', 'Frontend', 'UI_Design',
    'userexperience', 'graphic_design', 'design', 'css', 'reactjs',
    'javascript', 'webdesign', 'html',
})
_NOCODE = frozenset({
    'nocode', 'NoCodeSaaS', 'Webflow', 'Bubble',
})
_BUSINESS = frozenset({
    'startups', 'Entrepreneur', 'SaaS', 'solopreneur', 'smallbusiness', 'digitalnomad',
})
_MARKETING = frozenset({
    'marketing', 'digitalmarketing', 'PPC', 'SEO', 'ecommerce',
})
_INDUSTRY = frozenset({
    'restaurateur', 'RealEstate', 'fitness', 'personaltrainer',
    'legaladvice', 'medicine', 'IndieDev', 'gamedev',
})

REDDIT_FEEDS: dict[str, str] = {
    s: f'https://www.reddit.com/r/{s}/.rss'
    for s in (*_HIRING, *_DESIGN_TECH, *_NOCODE, *_BUSINESS, *_MARKETING, *_INDUSTRY)
}


# ---------------------------------------------------------------------------
# Light filter signal sets
# ---------------------------------------------------------------------------

_WEB_SIGNALS = frozenset({
    'website', 'web design', 'web designer', 'web developer',
    'landing page', 'framer', 'figma', 'portfolio', 'frontend',
    'ui/ux', 'web app', 'react', 'html', 'css',
})
_HIRE_SIGNALS = frozenset({
    'hire', 'hiring', 'looking for', 'need a ', 'need someone', 'seeking',
    'want to hire', 'want someone', 'need help with', 'looking to hire',
    'open to offers', 'taking on clients', '[hiring]',
})
_PAYMENT_SIGNALS = frozenset({
    'pay', 'paid', 'budget', 'rate', 'quote', 'per hour', 'hourly',
    'fixed price', 'compensation', '$', '£', '€',
})
_FRAMER_SIGNALS = frozenset({'framer'})
_BUSINESS_WEB = frozenset({
    'website', 'landing page', 'web design', 'web designer',
    'web developer', 'framer', 'figma',
})
_MARKETING_WEB = frozenset({'website', 'landing page', 'web design', 'web designer', 'framer', 'figma'})

# Posts matching these are almost always job seekers advertising, not clients
_JOB_SEEKER_SIGNALS = frozenset({
    '[for hire]', 'available for hire', "i'm available", 'i am available',
    'my portfolio', 'check out my work', 'dm me for work', 'hire me',
})
_ALWAYS_EXCLUDE = frozenset({
    'how to ', 'how do i ', 'tutorial', 'course', 'learning framer',
    'framer beginner', 'getting started with',
    'feedback on my', 'critique my', 'roast my',
    'what do you think', 'need feedback', 'honest feedback',
    'frustrated with', 'disappointed with', 'beware of', 'warning:',
    'framer pricing', 'framer cost', 'framer vs', 'how much does framer',
    'framer subscription',
})

# Phrases excluded only when the *first* word appears as a whole word —
# avoids false positives where the phrase is a substring of an unrelated
# word.  ``'rate my'`` must not match inside ``'migrate my website'``
# because ``'migrate'`` ends in ``'rate'``; plain substring matching would
# silently drop the textbook hiring post "[Hiring] need to migrate my
# website from Squarespace to Framer, paid".  Using ``\b`` before the
# first word catches ``'rate my design'`` (a genuine
# feedback-request post) but not ``'migrate my …'``.
_ALWAYS_EXCLUDE_WORD_START_PHRASES = frozenset({
    'rate my',
})


def _has(text: str, signals: frozenset) -> bool:
    tl = text.lower()
    return any(s in tl for s in signals)


def _has_word_start_phrase(text: str, phrases: frozenset) -> bool:
    """Return True if any phrase in *phrases* appears with its first word as a whole word.

    Unlike plain substring matching, this prevents phrases from matching when
    the first word is a suffix of a longer word.  For example, ``'rate my'``
    matches ``'rate my design'`` but not ``'migrate my website'`` (where
    ``'rate'`` is embedded inside ``'migrate'``).  Mirrors the same helper in
    ``dub-affiliate-scanner/scripts/reddit_scanner.py``.
    """
    tl = text.lower()
    return any(bool(re.search(r'\b' + re.escape(p), tl)) for p in phrases)


def passes_light_filter(title: str, content: str, subreddit: str) -> bool:
    """Return True if this post is worth saving for Claude to review."""
    text = f'{title} {content}'

    if _has(text, _ALWAYS_EXCLUDE) or _has(text, _JOB_SEEKER_SIGNALS):
        return False

    if _has_word_start_phrase(text, _ALWAYS_EXCLUDE_WORD_START_PHRASES):
        return False

    if subreddit in _HIRING:
        # Job boards: any post with a web/design tech signal is worth reviewing
        return _has(text, _WEB_SIGNALS)

    if subreddit in _DESIGN_TECH:
        # Design/tech communities: need framer + intent, or hiring + payment
        return (
            (_has(text, _FRAMER_SIGNALS) and _has(text, _HIRE_SIGNALS))
            or (_has(text, _HIRE_SIGNALS) and _has(text, _PAYMENT_SIGNALS))
        )

    if subreddit in _NOCODE:
        # No-code tools: hiring + web signal + payment (more specific to cut noise)
        return (
            _has(text, _HIRE_SIGNALS)
            and _has(text, _WEB_SIGNALS)
            and _has(text, _PAYMENT_SIGNALS)
        )

    if subreddit in _BUSINESS:
        return _has(text, _BUSINESS_WEB) and _has(text, _HIRE_SIGNALS)

    if subreddit in _MARKETING | _INDUSTRY:
        return _has(text, _MARKETING_WEB) and _has(text, _HIRE_SIGNALS)

    # Unknown subreddit: require all three signals
    return _has(text, _WEB_SIGNALS) and _has(text, _HIRE_SIGNALS) and _has(text, _PAYMENT_SIGNALS)


# ---------------------------------------------------------------------------
# Environment & HTTP helpers (same patterns as framer_templates.py)
# ---------------------------------------------------------------------------

def load_dotenv() -> None:
    try:
        with open('.env') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                key, _, val = line.partition('=')
                if key.strip() and key.strip() not in os.environ:
                    os.environ[key.strip()] = val.strip()
    except FileNotFoundError:
        pass


def _should_retry(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (429, 500, 502, 503, 504)
    if isinstance(exc, urllib.error.URLError):
        return True  # network/connection errors
    # Read timeouts raised after a connection is already established (e.g.
    # during ``response.read()``) propagate as bare ``TimeoutError`` /
    # ``socket.timeout``, NOT wrapped in ``URLError``.  Without this branch
    # the existing ``"The read operation timed out"`` failures observed in
    # logs/errors.jsonl bypass retry entirely.
    if isinstance(exc, TimeoutError):
        return True
    return False


# Upper bound on the sleep we will honour from a server-supplied ``Retry-After``
# header.  Reddit's rate-limit responses can carry values in the hundreds of
# seconds; sleeping that long inside a 15-minute cron run would consume most of
# the run window without doing useful work, so we cap at 60 seconds while still
# respecting the signal that we should slow down.
_RETRY_AFTER_MAX_SECONDS = 60


def _parse_retry_after(value: str) -> float | None:
    """Parse an HTTP ``Retry-After`` header value into a number of seconds.

    Per RFC 7231 §7.1.3, ``Retry-After`` is either a non-negative integer
    number of seconds (e.g. ``"5"``) or an HTTP-date (e.g.
    ``"Wed, 21 Oct 2026 07:28:00 GMT"``).  Returns the delay in seconds for
    either form, or ``None`` if the value is missing/malformed so the caller
    can fall back to its default backoff.
    """
    if not value:
        return None
    value = value.strip()
    # Integer-seconds form (the common case for Discord / Notion / Twitter).
    try:
        seconds = float(value)
        return seconds if seconds >= 0 else None
    except ValueError:
        pass
    # HTTP-date form.
    try:
        from email.utils import parsedate_to_datetime
        target = parsedate_to_datetime(value)
        if target is None:
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = (target - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


def _retry(fn, max_attempts: int = 4):
    """Run *fn* with exponential backoff, honouring ``Retry-After`` on 429s.

    When the server responds with HTTP 429 and a ``Retry-After`` header, we
    sleep at least that long (clamped to ``_RETRY_AFTER_MAX_SECONDS``) before
    the next attempt instead of the default exponential schedule.  Reddit's
    RSS endpoints rate-limit aggressively when fetching 43 feeds back-to-back,
    so respecting ``Retry-After`` recovers in the minimum time the server
    asked for instead of guessing with a fixed backoff that may be too short.
    """
    import time
    delay = 2
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not _should_retry(exc) or attempt == max_attempts - 1:
                raise
            sleep_for: float = delay
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                headers = getattr(exc, 'headers', None)
                if headers is not None:
                    retry_after = _parse_retry_after(headers.get('Retry-After', ''))
                    if retry_after is not None:
                        sleep_for = max(sleep_for, min(retry_after, _RETRY_AFTER_MAX_SECONDS))
            time.sleep(sleep_for)
            delay *= 2
    raise last_exc  # unreachable but satisfies type checkers


def http_get(url: str, headers: dict | None = None) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    def _do():
        req = urllib.request.Request(
            url,
            headers={'User-Agent': _REDDIT_USER_AGENT, **(headers or {})},
        )
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            return r.read().decode('utf-8')
    return _retry(_do)


def http_post(url: str, data: dict, headers: dict | None = None) -> dict:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    body = json.dumps(data).encode('utf-8')
    def _do():
        req = urllib.request.Request(
            url,
            data=body,
            headers={'Content-Type': 'application/json', 'User-Agent': 'automation-bot/1.0', **(headers or {})},
            method='POST',
        )
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    return _retry(_do)


def http_patch(url: str, data: dict, headers: dict | None = None) -> dict:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    body = json.dumps(data).encode('utf-8')
    def _do():
        req = urllib.request.Request(
            url,
            data=body,
            headers={'Content-Type': 'application/json', 'User-Agent': 'automation-bot/1.0', **(headers or {})},
            method='PATCH',
        )
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    return _retry(_do)


def notion_headers() -> dict:
    return {
        'Authorization': f'Bearer {os.environ["NOTION_TOKEN"]}',
        'Notion-Version': '2022-06-28',
    }


# ---------------------------------------------------------------------------
# RSS fetching & parsing
# ---------------------------------------------------------------------------

def _clean_html(text: str) -> str:
    """Strip HTML tags and decode entities from RSS content."""
    if not text:
        return ''
    text = _html.unescape(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


_ATOM_NS = {'atom': 'http://www.w3.org/2005/Atom'}


def fetch_reddit_posts(
    subreddit: str,
    feed_url: str,
    error_samples: list[str] | None = None,
) -> list[dict] | None:
    """Fetch and parse an Atom RSS feed.

    Returns a list of post dicts on success (possibly empty for a feed with no
    entries), or None if the feed could not be fetched or parsed.

    When *error_samples* is provided, a short descriptor of the failure cause
    (e.g. ``"r/forhire HTTP 500"`` or ``"r/forhire URLError"``) is appended on
    failure.  The caller uses this to surface a sample of root causes in the
    aggregated fetch-failure Discord alert — without it, an operator seeing
    ``"Majority of subreddit feeds failed (25/43)"`` has no signal about
    whether the cause is a Reddit-wide outage (all HTTP 500), rate-limiting
    (all HTTP 429), a network blip (URLError), or a mix.  The list is appended
    to unconditionally so the caller can cap it at the call site if desired.
    """
    def _record(sample: str) -> None:
        if error_samples is not None:
            error_samples.append(sample)

    try:
        body = http_get(feed_url)
    except urllib.error.HTTPError as e:
        body_preview = ''
        try:
            body_preview = e.read().decode('utf-8', errors='replace')[:200]
        except Exception:
            pass
        print(f'HTTP {e.code} fetching r/{subreddit}: {e}')
        error_log.log_error(
            'reddit_leads', 'warning',
            f'Failed to fetch r/{subreddit}',
            {
                'feed_url': feed_url,
                'status': e.code,
                'body_preview': body_preview,
                'error': str(e),
            },
        )
        _record(f'r/{subreddit} HTTP {e.code}')
        return None
    except Exception as e:
        print(f'Failed to fetch r/{subreddit}: {e}')
        error_log.log_error(
            'reddit_leads', 'warning',
            f'Failed to fetch r/{subreddit}',
            {'feed_url': feed_url, 'error': str(e)},
        )
        _record(f'r/{subreddit} {type(e).__name__}')
        return None

    posts = []
    try:
        root = ET.fromstring(body)
        for entry in root.findall('atom:entry', _ATOM_NS):
            title_el = entry.find('atom:title', _ATOM_NS)
            link_el = entry.find('atom:link', _ATOM_NS)
            published_el = entry.find('atom:published', _ATOM_NS)
            updated_el = entry.find('atom:updated', _ATOM_NS)
            content_el = entry.find('atom:content', _ATOM_NS)

            title = _clean_html(title_el.text or '') if title_el is not None else ''
            link = link_el.get('href', '') if link_el is not None else ''
            # Prefer <published> (original post creation time) over <updated>
            # (which reflects the last comment or edit time and can be much later).
            published = (published_el.text or '') if published_el is not None else ''
            updated = (updated_el.text or '') if updated_el is not None else ''
            post_date = published or updated
            content = _clean_html(content_el.text or '') if content_el is not None else ''

            if title and link:
                posts.append({
                    'title': title,
                    'url': link,
                    'post_date': post_date,
                    'content': content,
                    'subreddit': subreddit,
                })
    except ET.ParseError as e:
        # Capture the first 500 chars of the body so a maintainer can tell at a
        # glance what Reddit returned when XML parsing fails.  A ``ParseError``
        # almost always means Reddit returned HTTP 200 with a body that is not
        # the expected Atom XML — most often the HTML "reddit broke!" error
        # page (Reddit serves this with 200 in some shard-outage modes, not
        # always 500), a JSON-formatted rate-limit response, or an
        # authentication/captcha challenge HTML.  Without ``body_preview`` the
        # log only contains the ``xml.etree`` parse-position message (e.g.
        # ``"syntax error: line 1, column 0"``) which gives no signal about
        # which of those failure modes is at play.  Mirrors the same
        # ``body_preview`` capture already used in the HTTP error branch above.
        body_preview = body[:500] if body else ''
        print(f'Failed to parse RSS for r/{subreddit}: {e}')
        error_log.log_error(
            'reddit_leads', 'warning',
            f'Failed to parse RSS for r/{subreddit}',
            {'error': str(e), 'body_preview': body_preview},
        )
        _record(f'r/{subreddit} ParseError')
        return None

    return posts


# ---------------------------------------------------------------------------
# Notion state store
# ---------------------------------------------------------------------------

def url_exists_in_notion(url: str, db_id: str) -> bool:
    """Return True if this URL is already stored in the Notion DB."""
    data = http_post(
        f'https://api.notion.com/v1/databases/{db_id}/query',
        {
            'filter': {'property': 'URL', 'url': {'equals': url}},
            'page_size': 1,
        },
        headers=notion_headers(),
    )
    return len(data.get('results', [])) > 0


def _is_valid_iso8601_date(value: str) -> bool:
    """Return True if *value* is a non-empty string that Python can parse as an
    ISO 8601 datetime.  Notion's date API field requires a valid ISO 8601 string;
    a malformed value causes an HTTP 400 that will recur on every subsequent run
    because the page is never created and dedup never triggers."""
    if not value:
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False


def _truncate_for_notion(value: str, limit: int = 2000) -> str:
    """Truncate *value* so its UTF-16 code-unit length is <= *limit*.

    Notion's rich_text/title length validator counts UTF-16 code units, not
    Python code points.  Supplementary Unicode characters (e.g. most emoji)
    are 1 Python code point but 2 UTF-16 code units, so a Python ``[:2000]``
    slice can yield a string Notion considers 2001+ chars long, causing a
    400 ``validation_error`` (observed in ``logs/errors.jsonl`` on
    2026-04-29 for r/smallbusiness).

    We trim by repeatedly dropping the trailing code point until the UTF-16
    encoding fits.  Returning a shorter (but valid) string is preferable to
    a 400 that loses the entire lead.
    """
    if not value:
        return value
    # Fast path: most strings are pure BMP and will fit after a simple slice.
    truncated = value[:limit]
    while len(truncated.encode('utf-16-le')) // 2 > limit:
        # Drop one code point at a time.  In practice this loops at most as
        # many times as there are supplementary characters in the slice
        # (typically 0-1 for Reddit content).
        truncated = truncated[:-1]
    return truncated


def save_lead_to_notion(lead: dict, db_id: str) -> None:
    """Save a new lead to Notion with status 'pending'."""
    props: dict = {
        'Name': {'title': [{'text': {'content': _truncate_for_notion(lead['title'])}}]},
        'URL': {'url': lead['url']},
        'Subreddit': {'select': {'name': lead['subreddit']}},
        'Content': {'rich_text': [{'text': {'content': _truncate_for_notion(lead['content'])}}]},
        'Status': {'select': {'name': 'pending'}},
        'Discovered': {'date': {'start': datetime.now(timezone.utc).isoformat()}},
    }
    post_date = lead.get('post_date', '')
    if _is_valid_iso8601_date(post_date):
        props['Post Date'] = {'date': {'start': post_date}}
    elif post_date:
        # Date present but unparseable — log a warning and omit the field so
        # the lead is still saved rather than causing a recurring Notion 400.
        error_log.log_error(
            'reddit_leads', 'warning',
            'Skipping invalid post_date for lead',
            {'url': lead.get('url', ''), 'post_date': post_date},
        )
    http_post(
        'https://api.notion.com/v1/pages',
        {'parent': {'database_id': db_id}, 'properties': props},
        headers=notion_headers(),
    )


def save_failed_sentinel_to_notion(lead: dict, db_id: str) -> None:
    """Write a minimal 'failed' sentinel page to Notion so future dedup checks skip this URL.

    Called after a non-retriable save error (e.g. HTTP 400) to prevent the same
    URL from being re-attempted on every subsequent run while it remains in the
    RSS feed.  The sentinel only stores the URL and a 'failed' status — no title
    or content that might have triggered the original error.
    """
    try:
        http_post(
            'https://api.notion.com/v1/pages',
            {
                'parent': {'database_id': db_id},
                'properties': {
                    'Name': {'title': [{'text': {'content': '[save-failed sentinel]'}}]},
                    'URL': {'url': lead['url']},
                    'Status': {'select': {'name': 'failed'}},
                    'Discovered': {'date': {'start': datetime.now(timezone.utc).isoformat()}},
                },
            },
            headers=notion_headers(),
        )
    except Exception as sentinel_exc:
        # If writing the sentinel also fails, log but don't raise — the original
        # error is what matters and has already been logged by the caller.
        error_log.log_error(
            'reddit_leads', 'warning',
            'Failed to write save-failed sentinel to Notion',
            {'url': lead.get('url', ''), 'error': str(sentinel_exc)},
        )


def get_pending_leads(db_id: str) -> list[dict]:
    """Return all leads with Status = 'pending' from Notion."""
    leads = []
    cursor = None
    while True:
        body: dict = {
            'filter': {'property': 'Status', 'select': {'equals': 'pending'}},
            'page_size': 100,
        }
        if cursor:
            body['start_cursor'] = cursor
        data = http_post(
            f'https://api.notion.com/v1/databases/{db_id}/query',
            body,
            headers=notion_headers(),
        )
        for page in data.get('results', []):
            props = page['properties']
            title_rt = props.get('Name', {}).get('title', [])
            subreddit_sel = props.get('Subreddit', {}).get('select') or {}
            content_rt = props.get('Content', {}).get('rich_text', [])
            post_date_prop = props.get('Post Date', {}).get('date') or {}
            leads.append({
                'page_id': page['id'],
                'title': title_rt[0]['plain_text'] if title_rt else '',
                'url': props.get('URL', {}).get('url', '') or '',
                'subreddit': subreddit_sel.get('name', ''),
                'content': content_rt[0]['plain_text'] if content_rt else '',
                'post_date': post_date_prop.get('start', ''),
            })
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return leads


def get_unnotified_approved_leads(db_id: str) -> list[dict]:
    """Return all leads with Status='approved' AND Notified=False from Notion.

    These are leads the reviewer marked approved in a previous session whose
    ``--notify`` invocation failed (e.g. transient Discord 5xx, expired
    webhook, network blip).  Without this recovery hook, an approved lead
    that fails to notify is silently lost forever: the next reviewer session
    only inspects ``Status=pending`` leads, so the approved-but-unnotified
    page is never picked up again.

    The reviewer session calls ``--list-unnotified-approved`` after handling
    pending leads so it can re-run ``--notify PAGE_ID`` on each entry.  The
    Review Notes set in the original approval are preserved on the page, so
    the retry uses the same explanation in the Discord embed.
    """
    leads = []
    cursor = None
    while True:
        body: dict = {
            'filter': {
                'and': [
                    {'property': 'Status', 'select': {'equals': 'approved'}},
                    {'property': 'Notified', 'checkbox': {'equals': False}},
                ],
            },
            'page_size': 100,
        }
        if cursor:
            body['start_cursor'] = cursor
        data = http_post(
            f'https://api.notion.com/v1/databases/{db_id}/query',
            body,
            headers=notion_headers(),
        )
        for page in data.get('results', []):
            props = page['properties']
            title_rt = props.get('Name', {}).get('title', [])
            subreddit_sel = props.get('Subreddit', {}).get('select') or {}
            content_rt = props.get('Content', {}).get('rich_text', [])
            notes_rt = props.get('Review Notes', {}).get('rich_text', [])
            post_date_prop = props.get('Post Date', {}).get('date') or {}
            leads.append({
                'page_id': page['id'],
                'title': title_rt[0]['plain_text'] if title_rt else '',
                'url': props.get('URL', {}).get('url', '') or '',
                'subreddit': subreddit_sel.get('name', ''),
                'content': content_rt[0]['plain_text'] if content_rt else '',
                'review_notes': notes_rt[0]['plain_text'] if notes_rt else '',
                'post_date': post_date_prop.get('start', ''),
            })
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return leads


def get_lead_by_id(page_id: str) -> dict:
    """Fetch a single Notion page and return it as a lead dict."""
    raw = http_get(
        f'https://api.notion.com/v1/pages/{page_id}',
        headers=notion_headers(),
    )
    page = json.loads(raw)
    props = page.get('properties', {})
    title_rt = props.get('Name', {}).get('title', [])
    subreddit_sel = props.get('Subreddit', {}).get('select') or {}
    content_rt = props.get('Content', {}).get('rich_text', [])
    notes_rt = props.get('Review Notes', {}).get('rich_text', [])
    # ``Post Date`` is the original Reddit publish time saved by Phase 1.  It is
    # included in the returned lead so that ``notify_discord_lead`` can surface
    # it as the Discord embed ``timestamp`` — this lets a human reading the
    # leads channel see at a glance how stale a lead is (a 5-day-old post is
    # much less actionable than a 2-hour-old one) without clicking through.
    post_date_prop = props.get('Post Date', {}).get('date') or {}
    return {
        'page_id': page['id'],
        'title': title_rt[0]['plain_text'] if title_rt else '',
        'url': props.get('URL', {}).get('url', '') or '',
        'subreddit': subreddit_sel.get('name', ''),
        'content': content_rt[0]['plain_text'] if content_rt else '',
        'review_notes': notes_rt[0]['plain_text'] if notes_rt else '',
        'post_date': post_date_prop.get('start', ''),
    }


# The set of Status values the script and reviewer session legitimately use.
# Notion ``select`` fields silently create a new option for any unknown name —
# so a reviewer typo like ``"approve"`` (missing the trailing ``"d"``) or
# ``"rejcted"`` would not raise, but the resulting page would not match the
# ``Status='approved'`` filter used by ``--notify`` / ``--list-unnotified-
# approved``, and would not match the ``Status='pending'`` filter used by
# ``--list-pending`` either — the lead would be orphaned and silently lost.
# ``pending`` and ``failed`` are written by Phase 1 (this script's ``main()``);
# ``approved`` and ``rejected`` are written by the Phase 2 reviewer session
# via the ``--update-status`` CLI.
_VALID_STATUSES = frozenset({'pending', 'approved', 'rejected', 'failed'})


def update_lead_status(page_id: str, status: str, notes: str) -> None:
    """Update the Status and Review Notes fields on a Notion page.

    ``status`` is validated against ``_VALID_STATUSES``; an unknown value raises
    ``ValueError`` *before* any Notion request is made.  Without this guard, a
    typo would silently create a brand-new Notion select option and orphan the
    lead (it would no longer match any of the queries the rest of the script
    relies on — ``Status='pending'``, ``'approved'``, ``'rejected'``, or
    ``'failed'``), so catching it at the source is much cheaper than recovering
    from it later.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(
            f'Invalid status {status!r}; expected one of '
            f'{sorted(_VALID_STATUSES)}'
        )
    http_patch(
        f'https://api.notion.com/v1/pages/{page_id}',
        {
            'properties': {
                'Status': {'select': {'name': status}},
                'Review Notes': {'rich_text': [{'text': {'content': _truncate_for_notion(notes)}}]},
            }
        },
        headers=notion_headers(),
    )


def mark_notified(page_id: str) -> None:
    """Set the Notified checkbox to True on a Notion page."""
    http_patch(
        f'https://api.notion.com/v1/pages/{page_id}',
        {'properties': {'Notified': {'checkbox': True}}},
        headers=notion_headers(),
    )


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def notify_discord_lead(lead: dict) -> bool:
    """Send a Discord embed for an approved lead.

    Returns True if the webhook POST succeeded, False otherwise.  The
    exception is still swallowed (and logged) so callers do not need a
    try/except, but the return value lets the ``--notify`` CLI avoid
    flipping the Notion ``Notified`` checkbox when Discord is down — a
    silent failure there would mean the lead never reaches the channel
    yet is treated as delivered, so it would never be retried.
    """
    review_notes = lead.get('review_notes', '')
    description = f"**Why this is a lead:** {review_notes}" if review_notes else ''
    embed: dict = {
        'title': lead['title'][:256],
        'url': lead['url'],
        'description': description,
        'color': 0x00B0F4,
        'author': {'name': f"r/{lead['subreddit']}"},
    }
    # Surface the Reddit post's original publish time as the embed timestamp so
    # Discord renders a human-readable "X hours/days ago" indicator under the
    # embed.  This lets the operator see at a glance how stale a lead is — a
    # 5-day-old hiring post is much less actionable than a fresh one — without
    # opening Reddit.  Discord requires a valid ISO 8601 string here, so an
    # empty/malformed value is silently omitted (a parsable check is cheap and
    # avoids letting a bad value 400 the webhook for a whole batch of leads).
    post_date = lead.get('post_date', '')
    if post_date:
        try:
            datetime.fromisoformat(post_date)
            embed['timestamp'] = post_date
        except (ValueError, TypeError):
            pass
    try:
        http_post(os.environ['DISCORD_WEBHOOK_URL_LEADS'], {'embeds': [embed]})
        return True
    except urllib.error.HTTPError as e:
        # Capture the Discord API response body so an operator can distinguish
        # between a revoked webhook (401), a deleted channel/webhook (404), a
        # rate-limit (429), and a malformed-payload rejection (400) — all of
        # which otherwise log only ``"HTTP Error <code>: <reason>"`` with no
        # actionable signal.  Mirrors the same diagnostic pattern already used
        # by ``save_lead_to_notion``, ``url_exists_in_notion``,
        # ``fetch_reddit_posts``, and ``post_to_x`` in ``framer_templates.py``.
        discord_response = ''
        try:
            discord_response = e.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        print(f'Discord notification failed for "{lead["title"]}": {e}')
        error_log.log_error(
            'reddit_leads', 'warning',
            f'Discord notification failed for "{lead["title"]}"',
            {
                'status': e.code,
                'error': str(e),
                'url': lead.get('url', ''),
                'subreddit': lead.get('subreddit', ''),
                'discord_response': discord_response,
            },
        )
        return False
    except Exception as e:
        print(f'Discord notification failed for "{lead["title"]}": {e}')
        error_log.log_error(
            'reddit_leads', 'warning',
            f'Discord notification failed for "{lead["title"]}"',
            {'error': str(e), 'url': lead.get('url', ''), 'subreddit': lead.get('subreddit', '')},
        )
        return False


# ---------------------------------------------------------------------------
# Alert suppression (cross-run de-duplication)
# ---------------------------------------------------------------------------
#
# A sustained outage produces one identical Discord alert per 15-min cron
# tick.  Today's incident fired the same dedup-failure alert 4× in ~30 min
# — none of the repeats carried new diagnostic information.  We persist the
# last-sent timestamp of each alert key to a small JSON file that the
# workflow commits alongside ``logs/errors.jsonl``; subsequent runs skip
# the alert if it was sent inside the suppression window.
#
# Per-script state file path (no cross-script writes → no push races).
_ALERT_STATE_PATH = 'state/alert_state-reddit_leads.json'
# Minutes to suppress an identical alert key after a successful send.  Picked
# so a 4-tick / 60-min outage produces exactly one alert; further alerts
# only fire when the issue persists beyond the window, which is itself a
# useful signal that "we are still seeing this".
_ALERT_SUPPRESS_MINUTES = 60


def _load_alert_state(path: str | None = None) -> dict:
    """Load the persisted alert-state map; return {} on any error.

    Never raises — a missing/corrupt state file simply means "no recent
    alert known", which is safe (the worst case is one extra Discord
    notification, not a missed alert).

    ``path`` is looked up from the module-level constant when omitted so
    test patches of ``_ALERT_STATE_PATH`` take effect (Python evaluates
    default argument expressions once at definition time, which would
    otherwise bake the original path into every helper).
    """
    if path is None:
        path = _ALERT_STATE_PATH
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_alert_state(state: dict, path: str | None = None) -> None:
    """Persist *state* to disk, creating parent directories if needed."""
    if path is None:
        path = _ALERT_STATE_PATH
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except OSError as e:
        # Failing to persist the suppression record only means the next
        # run won't suppress this alert key — non-fatal.
        print(f'[alert_state] failed to save: {e}', file=sys.stderr)


def _should_suppress_alert(
    key: str,
    suppress_minutes: int = _ALERT_SUPPRESS_MINUTES,
    state_path: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Return True if an alert with *key* was sent within the suppress window."""
    state = _load_alert_state(state_path)
    last_sent_str = state.get(key)
    if not last_sent_str:
        return False
    try:
        last_sent = datetime.fromisoformat(last_sent_str)
    except (ValueError, TypeError):
        return False
    if last_sent.tzinfo is None:
        last_sent = last_sent.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    elapsed = (current - last_sent).total_seconds()
    return elapsed < suppress_minutes * 60


def _record_alert_sent(
    key: str,
    state_path: str | None = None,
    now: datetime | None = None,
) -> None:
    state = _load_alert_state(state_path)
    state[key] = (now or datetime.now(timezone.utc)).isoformat()
    _save_alert_state(state, state_path)


def _warn_discord(message: str, dedup_key: str | None = None,
                  suppress_minutes: int = _ALERT_SUPPRESS_MINUTES) -> None:
    """Send a system-level warning to the dedicated alerts webhook.

    If *dedup_key* is provided and the same key was sent successfully
    within the last *suppress_minutes* (per the persisted state file),
    the alert is silently skipped.  The send timestamp is only recorded
    after a successful POST, so a transient Discord 5xx does not suppress
    the next attempt when the webhook recovers.
    """
    if dedup_key and _should_suppress_alert(dedup_key, suppress_minutes):
        print(f'[alert] Suppressing duplicate alert (key={dedup_key})')
        return
    webhook_url = os.environ.get('DISCORD_ALERTS_WEBHOOK_URL')
    if not webhook_url:
        print('DISCORD_ALERTS_WEBHOOK_URL not set — skipping alert.')
        return
    try:
        http_post(webhook_url, {'content': f'[framerlabs-automations] {message}'})
    except urllib.error.HTTPError as e:
        # Capture the Discord API response body so a misconfigured alerts
        # webhook (revoked, deleted, rate-limited, malformed payload) can be
        # diagnosed from logs/errors.jsonl alone — without it the log says
        # only ``"HTTP Error <code>: <reason>"``.  Mirrors the diagnostic
        # capture in ``notify_discord_lead`` above.
        discord_response = ''
        try:
            discord_response = e.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        print(f'Failed to send Discord alert: {e}')
        error_log.log_error(
            'reddit_leads', 'warning',
            'Failed to send Discord alert',
            {'status': e.code, 'error': str(e), 'discord_response': discord_response},
        )
        return
    except Exception as e:
        print(f'Failed to send Discord alert: {e}')
        error_log.log_error('reddit_leads', 'warning', 'Failed to send Discord alert', {'error': str(e)})
        return
    if dedup_key:
        _record_alert_sent(dedup_key)


def _write_summary(text: str) -> None:
    """Append a markdown summary to the GitHub Actions job summary file, if running in CI."""
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not path:
        return
    try:
        with open(path, 'a') as f:
            f.write(text + '\n')
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main monitoring flow
# ---------------------------------------------------------------------------

# When dedup-check failures meet or exceed this threshold within a single run,
# emit a Discord alert.  The thresholds are split because the failure modes
# are very different:
#   * A Notion ``object_not_found`` 404 is almost always a permanent
#     misconfiguration (deleted DB, integration access revoked, wrong DB id
#     in the secret) — every subsequent post will silently skip its dedup
#     check, no leads will be saved, and operators won't notice until the
#     lead flow dries up days later.  Alert on the first occurrence so the
#     misconfiguration surfaces immediately.
#   * Other dedup failures (transient network errors, read timeouts, generic
#     5xx) are individually OK to skip but a sustained burst within a single
#     run also warrants a heads-up.
_DEDUP_OBJECT_NOT_FOUND_ALERT_THRESHOLD = 1
_DEDUP_OTHER_FAILURE_ALERT_THRESHOLD = 5

# After this many *consecutive* dedup failures with zero successes between
# them, treat Notion as broadly unhealthy and short-circuit the rest of the
# run.  PR #92 only short-circuited on Notion's ``object_not_found`` code,
# which left the run grinding through 43 doomed dedup calls when Notion was
# down for any other reason (timeouts, generic 5xx, connection errors).  At
# ~30s timeout × 4 retries per failed call, a 43-post run could exceed the
# 15-min cron tick.  Three failures with no successes between them is a
# strong signal that Notion is down for this run — bail out and let the
# next cron tick try again.  Reset to 0 on any successful dedup or save.
_CONSECUTIVE_DEDUP_FAILURE_SHORT_CIRCUIT = 3


def _notion_preflight(db_id: str) -> tuple[bool, str, int | None, str]:
    """Probe Notion with one cheap query before entering the main feed loop.

    Returns ``(ok, error_type, http_status, body_preview)``:
      * On success: ``(True, '', None, '')``.
      * On ``object_not_found`` 404: ``('object_not_found', 404, body)``
        so the caller can fire the same DB-misconfigured alert it would
        for an in-flight ``object_not_found``.
      * On any other ``HTTPError``: ``(False, f'HTTP <code>', code, body)``.
      * On any other exception: ``(False, type(exc).__name__, None, '')``.

    The probe uses a synthetic URL that will never match a real lead so
    the query is always a single-page Notion call that returns
    ``results: []`` — minimal cost on the happy path.  Performed via a
    dedicated function (not ``url_exists_in_notion``) so test patches
    targeting ``url_exists_in_notion`` for in-flight dedup behaviour are
    not also exercised by the preflight — keeps the test surface small.
    """
    try:
        http_post(
            f'https://api.notion.com/v1/databases/{db_id}/query',
            {
                'filter': {'property': 'URL', 'url': {'equals': '__preflight__never_matches__'}},
                'page_size': 1,
            },
            headers=notion_headers(),
        )
        return True, '', None, ''
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        if 'object_not_found' in body:
            return False, 'object_not_found', e.code, body
        return False, f'HTTP {e.code}', e.code, body
    except Exception as e:
        return False, type(e).__name__, None, ''


def main() -> None:
    load_dotenv()

    missing = [k for k in ('NOTION_TOKEN', 'NOTION_REDDIT_LEADS_DB_ID') if not os.environ.get(k)]
    if missing:
        print(f'Missing required env vars: {", ".join(missing)}')
        raise SystemExit(1)

    db_id = os.environ['NOTION_REDDIT_LEADS_DB_ID']

    # Notion preflight: one cheap query against the configured DB before we
    # enter the per-feed loop.  When Notion is broadly unreachable, today's
    # behaviour was a 43-call cascade where each failed dedup burned ~120s
    # of retried timeouts — exceeding the 15-min cron window.  A single
    # probe up front catches the "Notion is down for this run" case in
    # seconds, lets us fire one alert (suppressed across subsequent ticks
    # by ``dedup_key``), and exits before any Reddit traffic.
    preflight_ok, preflight_err, preflight_status, preflight_body = _notion_preflight(db_id)
    if not preflight_ok:
        error_log.log_error(
            'reddit_leads', 'error',
            'Notion preflight failed — skipping run',
            {
                'error_type': preflight_err,
                'status': preflight_status,
                'notion_response': preflight_body,
            },
        )
        if preflight_err == 'object_not_found':
            _warn_discord(
                'ERROR: reddit_leads.py — Notion preflight returned object_not_found.'
                ' DB likely deleted, renamed, or no longer shared with the integration.'
                ' Skipping this run. Check NOTION_REDDIT_LEADS_DB_ID secret and'
                ' logs/errors.jsonl.',
                dedup_key='reddit_leads:notion_preflight_object_not_found',
            )
        else:
            _warn_discord(
                f'WARNING: reddit_leads.py — Notion preflight failed ({preflight_err}).'
                ' Notion appears unreachable; skipping this run to avoid a 43-call'
                ' failure cascade. Check logs/errors.jsonl.',
                dedup_key='reddit_leads:notion_preflight_other',
            )
        print(f'Notion preflight failed: {preflight_err} — exiting early.')
        _write_summary(
            '## Reddit Leads Monitor\n'
            f'⚠️ Notion preflight failed ({preflight_err}) — skipped this run.'
        )
        return

    total_saved = 0
    fetch_errors = 0
    dedup_errors = 0
    dedup_object_not_found_errors = 0
    # Sample of dedup-failure context (subreddit + status code or error type),
    # surfaced in the Discord alert so an operator can see the most-likely root
    # cause without opening the log file.  Capped to keep the alert readable.
    dedup_error_samples: list[str] = []
    # Sample of fetch-failure context (subreddit + HTTP status or exception
    # type), surfaced in the >50% / 100% fetch-failure Discord alert so the
    # operator can distinguish at a glance between a Reddit-wide outage
    # (all "HTTP 500"), rate-limiting (all "HTTP 429"), a network blip (all
    # "URLError"), or a parsing regression (all "ParseError") — without it
    # the alert only carries the failure count, which gives no signal about
    # whether to wait it out, throttle harder, or investigate.
    fetch_error_samples: list[str] = []
    # Flag set the first time the dedup check returns Notion ``object_not_found``.
    # Once true, the configured ``NOTION_REDDIT_LEADS_DB_ID`` is misconfigured
    # (deleted, renamed, or no longer shared with the integration), so every
    # subsequent dedup and every save in the run will fail the same way.  We
    # stop fetching further subreddit feeds to (a) save the remaining ~60s of
    # ``_INTER_FEED_DELAY`` pacing + retry-backoff on Reddit + Notion calls,
    # and (b) avoid flooding ``logs/errors.jsonl`` with one warning entry per
    # filtered post (each entry contributing nothing diagnostic beyond the
    # first one).  The single alert at the end of the run still fires.
    db_misconfigured = False
    # Counter for *consecutive* dedup failures with no successes between them.
    # Resets to 0 on any successful dedup or save.  When it reaches
    # ``_CONSECUTIVE_DEDUP_FAILURE_SHORT_CIRCUIT`` we treat Notion as
    # in-flight unhealthy and short-circuit the rest of the run, even when
    # the failure mode is not ``object_not_found``.  This generalises the
    # PR #92 fix to cover today's incident classes (TimeoutError, URLError,
    # generic 5xx) that were deliberately left out of #92's narrow scope.
    consecutive_dedup_failures = 0
    notion_likely_down = False

    for i, (subreddit, feed_url) in enumerate(REDDIT_FEEDS.items()):
        if db_misconfigured or notion_likely_down:
            # Skip the remaining RSS fetches — Notion is in a state where no
            # further work in this run can succeed.  The end-of-main alert
            # path still fires below.
            break
        if i > 0:
            time.sleep(_INTER_FEED_DELAY)
        posts = fetch_reddit_posts(subreddit, feed_url, fetch_error_samples)
        if posts is None:
            fetch_errors += 1
            continue
        for post in posts:
            if not passes_light_filter(post['title'], post['content'], subreddit):
                continue
            # Dedup check is kept in its own try/except so that a transient
            # Notion API failure here does not (a) log a misleading "Error
            # saving lead" message, or (b) incorrectly write a failed-sentinel
            # that permanently blacklists the URL before any save was even
            # attempted.
            try:
                exists = url_exists_in_notion(post['url'], db_id)
                # Any successful dedup query resets the consecutive-failure
                # counter — Notion is clearly reachable for at least some
                # calls in this run, so the broad short-circuit should not
                # fire on the next isolated failure.
                consecutive_dedup_failures = 0
                if exists:
                    continue
            except urllib.error.HTTPError as e:
                # Capture the Notion API response body to help diagnose recurring
                # 404s (deleted DB? integration access revoked? transient
                # outage?).  Without this, the log only shows ``"HTTP Error 404:
                # Not Found"`` which gives no signal about which of those causes
                # is at play.  Mirrors the pattern used by save_lead_to_notion.
                notion_response = ''
                try:
                    notion_response = e.read().decode('utf-8', errors='replace')[:500]
                except Exception:
                    pass
                print(f'Error checking dedup for r/{subreddit}: {e}')
                error_log.log_error(
                    'reddit_leads', 'warning',
                    f'Dedup check failed for r/{subreddit} — skipping post',
                    {
                        'url': post['url'],
                        'status': e.code,
                        'error': str(e),
                        'notion_response': notion_response,
                    },
                )
                dedup_errors += 1
                consecutive_dedup_failures += 1
                # Treat ``object_not_found`` specially: it means the configured
                # NOTION_REDDIT_LEADS_DB_ID is invalid / not shared with the
                # integration, so every dedup check for the rest of the run
                # will fail the same way.  Tracked separately so a single
                # occurrence is enough to fire the alert.
                if 'object_not_found' in notion_response:
                    dedup_object_not_found_errors += 1
                    # First time we see this in a run: short-circuit the
                    # remaining work.  We've already logged the warning and
                    # captured a sample; doing N more dedup attempts (one per
                    # filtered post for the remaining subreddits) would only
                    # flood the log file with the same error and consume the
                    # rest of the 15-minute cron window on doomed Notion +
                    # Reddit RSS calls.  Set the flag so the outer loop
                    # breaks; the existing alert path at the end of ``main()``
                    # still fires with the single sample we collected.
                    db_misconfigured = True
                elif consecutive_dedup_failures >= _CONSECUTIVE_DEDUP_FAILURE_SHORT_CIRCUIT:
                    # Sustained dedup failures with no successes between
                    # them: Notion is in-flight unhealthy (timeouts, 5xx,
                    # connection errors).  Generalises the PR #92 narrow
                    # ``object_not_found`` short-circuit so the run does
                    # not grind through 40+ doomed dedup calls.
                    notion_likely_down = True
                if len(dedup_error_samples) < 5:
                    dedup_error_samples.append(f'r/{subreddit} HTTP {e.code}')
                if db_misconfigured or notion_likely_down:
                    break  # exit the inner per-post loop; outer loop break is next
                continue  # skip this post; do not attempt to save or write sentinel
            except Exception as e:
                print(f'Error checking dedup for r/{subreddit}: {e}')
                error_log.log_error(
                    'reddit_leads', 'warning',
                    f'Dedup check failed for r/{subreddit} — skipping post',
                    {'url': post['url'], 'error': str(e)},
                )
                dedup_errors += 1
                consecutive_dedup_failures += 1
                if len(dedup_error_samples) < 5:
                    dedup_error_samples.append(f'r/{subreddit} {type(e).__name__}')
                if consecutive_dedup_failures >= _CONSECUTIVE_DEDUP_FAILURE_SHORT_CIRCUIT:
                    notion_likely_down = True
                    break  # exit the inner per-post loop; outer loop break is next
                continue  # skip this post; do not attempt to save or write sentinel
            try:
                save_lead_to_notion(post, db_id)
                # A successful save also proves Notion is reachable — reset
                # the consecutive-failure counter so an in-flight degradation
                # short-circuit only fires on a sustained run of failures.
                consecutive_dedup_failures = 0
                total_saved += 1
                print(f'Saved: [r/{subreddit}] {post["title"]}')
            except urllib.error.HTTPError as e:
                body_preview = ''
                try:
                    body_preview = e.read().decode('utf-8', errors='replace')[:500]
                except Exception:
                    pass
                print(f'Error saving lead from r/{subreddit}: {e}')
                error_log.log_error(
                    'reddit_leads', 'error',
                    f'Error saving lead from r/{subreddit}',
                    {'url': post['url'], 'error': str(e), 'notion_response': body_preview},
                )
                # For non-retriable errors (e.g. 400 Bad Request), write a
                # sentinel page so the dedup check blocks future retries of the
                # same URL while it remains in the RSS feed.
                if not _should_retry(e):
                    save_failed_sentinel_to_notion(post, db_id)
            except Exception as e:
                print(f'Error saving lead from r/{subreddit}: {e}')
                error_log.log_error(
                    'reddit_leads', 'error',
                    f'Error saving lead from r/{subreddit}',
                    {'url': post['url'], 'error': str(e)},
                )

    # Cap the inline sample to the first 5 entries so the Discord alert stays
    # readable.  Five is enough to spot a single dominant failure mode (e.g.
    # "all HTTP 500") without flooding the channel; full per-feed context
    # remains in logs/errors.jsonl for deeper inspection.
    fetch_sample_str = (
        '; '.join(fetch_error_samples[:5]) if fetch_error_samples else 'none'
    )
    if fetch_errors == len(REDDIT_FEEDS):
        _warn_discord(
            f'WARNING: reddit_leads.py failed to fetch any subreddit feeds'
            f' — possible network issue. Samples: {fetch_sample_str}.'
            ' Check logs/errors.jsonl.',
            dedup_key='reddit_leads:fetch_all_failed',
        )
        error_log.log_error(
            'reddit_leads', 'error',
            'All subreddit feeds failed — possible network issue',
            {
                'feed_count': len(REDDIT_FEEDS),
                'samples': fetch_error_samples[:5],
            },
        )
    elif fetch_errors > len(REDDIT_FEEDS) // 2:
        _warn_discord(
            f'WARNING: reddit_leads.py failed to fetch {fetch_errors}/{len(REDDIT_FEEDS)} subreddit feeds'
            f' — possible partial network issue or Reddit rate-limiting.'
            f' Samples: {fetch_sample_str}. Check logs/errors.jsonl.',
            dedup_key='reddit_leads:fetch_majority_failed',
        )
        error_log.log_error(
            'reddit_leads', 'warning',
            f'Majority of subreddit feeds failed ({fetch_errors}/{len(REDDIT_FEEDS)})',
            {
                'fetch_errors': fetch_errors,
                'feed_count': len(REDDIT_FEEDS),
                'samples': fetch_error_samples[:5],
            },
        )

    # Dedup-check failure alerting: dedup failures cause every affected post to
    # be silently skipped (no save attempted, no sentinel written).  In normal
    # operation they should be 0; a sustained burst within a single run almost
    # always means Notion is misconfigured (deleted DB, revoked integration,
    # bad secret) and the script will save no new leads until fixed.  See the
    # 2026-05-04 incident in logs/errors.jsonl for an example.
    if dedup_object_not_found_errors >= _DEDUP_OBJECT_NOT_FOUND_ALERT_THRESHOLD:
        sample_str = '; '.join(dedup_error_samples) if dedup_error_samples else 'none'
        _warn_discord(
            f'ERROR: reddit_leads.py — Notion dedup check returned object_not_found'
            f' (DB likely deleted, renamed, or no longer shared with the integration).'
            f' {dedup_errors} dedup failure(s) this run. Samples: {sample_str}.'
            ' All affected posts skipped. Check logs/errors.jsonl and the'
            ' NOTION_REDDIT_LEADS_DB_ID secret.',
            dedup_key='reddit_leads:dedup_object_not_found',
        )
        error_log.log_error(
            'reddit_leads', 'error',
            'Notion dedup check returned object_not_found — DB likely misconfigured',
            {
                'dedup_errors': dedup_errors,
                'dedup_object_not_found_errors': dedup_object_not_found_errors,
                'samples': dedup_error_samples,
            },
        )
    elif notion_likely_down:
        # Hit the consecutive-failure short-circuit.  Distinct alert from
        # the ``object_not_found`` case because the operator action is
        # different: this signals an in-flight Notion outage (timeouts,
        # 5xx, connection errors) rather than a DB misconfiguration.
        sample_str = '; '.join(dedup_error_samples) if dedup_error_samples else 'none'
        _warn_discord(
            f'WARNING: reddit_leads.py — {consecutive_dedup_failures} consecutive'
            f' dedup-check failures; Notion appears unreachable. Short-circuited'
            f' the run after {dedup_errors} total failure(s). Samples: {sample_str}.'
            ' Check logs/errors.jsonl.',
            dedup_key='reddit_leads:dedup_notion_likely_down',
        )
        error_log.log_error(
            'reddit_leads', 'warning',
            f'Notion appears unreachable — short-circuited after {consecutive_dedup_failures} consecutive dedup failures',
            {
                'dedup_errors': dedup_errors,
                'consecutive_dedup_failures': consecutive_dedup_failures,
                'samples': dedup_error_samples,
            },
        )
    elif dedup_errors >= _DEDUP_OTHER_FAILURE_ALERT_THRESHOLD:
        sample_str = '; '.join(dedup_error_samples) if dedup_error_samples else 'none'
        _warn_discord(
            f'WARNING: reddit_leads.py — {dedup_errors} dedup-check failure(s) this run.'
            f' Samples: {sample_str}. Affected posts skipped (no sentinel written).'
            ' Check logs/errors.jsonl.',
            dedup_key='reddit_leads:dedup_other_failures',
        )
        error_log.log_error(
            'reddit_leads', 'warning',
            f'{dedup_errors} dedup-check failure(s) in single run',
            {'dedup_errors': dedup_errors, 'samples': dedup_error_samples},
        )

    print(f'Done. Saved {total_saved} new lead(s). ({fetch_errors} subreddit(s) unreachable)')
    _write_summary(
        f'## Reddit Leads Monitor\n'
        f'\U0001f4e5 {total_saved} new lead(s) saved · {fetch_errors}/{len(REDDIT_FEEDS)} subreddit(s) unreachable'
    )


# ---------------------------------------------------------------------------
# CLI interface for the daily reviewer session (REDDIT_LEADS_REVIEWER.md)
# ---------------------------------------------------------------------------

def cli(args: list[str]) -> None:
    """Dispatch a CLI invocation.

    Extracted from the ``__main__`` block so the dispatch logic can be unit
    tested without ``runpy.run_path`` re-importing the module (which would
    break ``unittest.mock`` patches against ``scripts.reddit_leads.*``).
    """
    if not args:
        main()
        return

    if args[0] == '--list-pending':
        load_dotenv()
        leads = get_pending_leads(os.environ['NOTION_REDDIT_LEADS_DB_ID'])
        print(json.dumps(leads, indent=2))
        return

    if args[0] == '--list-unnotified-approved':
        # Reviewer recovery hook: leads that were approved in a previous
        # session but whose ``--notify`` failed (e.g. transient Discord
        # outage) end up Status=approved + Notified=False.  ``--list-pending``
        # never picks them up again because it filters Status=pending only,
        # so without this command they would be silently lost.
        load_dotenv()
        leads = get_unnotified_approved_leads(os.environ['NOTION_REDDIT_LEADS_DB_ID'])
        print(json.dumps(leads, indent=2))
        return

    if args[0] == '--update-status' and len(args) >= 4:
        load_dotenv()
        page_id, status = args[1], args[2]
        notes = ' '.join(args[3:])
        # Validate the status before hitting Notion so a reviewer typo is
        # rejected with a clear error message instead of silently creating a
        # bogus select option that would orphan the lead from all later queries.
        try:
            update_lead_status(page_id, status, notes)
        except ValueError as e:
            print(f'update-status failed: {e}', file=sys.stderr)
            raise SystemExit(2)
        print(f'Updated {page_id} → {status}')
        return

    if args[0] == '--notify' and len(args) >= 2:
        load_dotenv()
        page_id = args[1]
        lead = get_lead_by_id(page_id)
        # Only mark the page as Notified after Discord has accepted the
        # webhook — otherwise a failed POST would still flip the checkbox
        # and the lead would silently never reach the channel nor be
        # retried on the next reviewer run.
        if not notify_discord_lead(lead):
            print(f'Notify failed for {page_id} — leaving Notified checkbox unset', file=sys.stderr)
            raise SystemExit(1)
        mark_notified(page_id)
        print(f'Notified: {lead["title"]}')
        return

    print(f'Unknown arguments: {args}', file=sys.stderr)
    raise SystemExit(1)


if __name__ == '__main__':
    cli(sys.argv[1:])
