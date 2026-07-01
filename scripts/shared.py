"""
Shared utilities used by all monitoring scripts.

HTTP helpers, retry logic, Notion helpers, Discord alert suppression,
dotenv loading, and GitHub Actions summary writing.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import error_log

_RETRY_AFTER_MAX_SECONDS = 60


def side_effects_enabled() -> bool:
    """Whether the monitoring flow may perform external side effects.

    The monitoring scripts run in two places: the production GitHub Actions
    cron (which sets ``GITHUB_ACTIONS=true``) and the self-improvement cloud
    routine, which runs the same scripts to observe behaviour while developing
    changes. Only the cron should write to Notion, post to Discord, or tweet —
    a sandbox run must stay read-only so its in-progress code cannot spam the
    public channels or pollute the production database. ``ENABLE_SIDE_EFFECTS=1``
    is an explicit override for tests and deliberate local production runs.
    """
    return (
        os.environ.get('GITHUB_ACTIONS') == 'true'
        or os.environ.get('ENABLE_SIDE_EFFECTS') == '1'
    )


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
        return exc.code in (429, 500, 502, 503, 504, 529)
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, TimeoutError):
        return True
    return False


def _parse_retry_after(value: str) -> float | None:
    """Parse an HTTP Retry-After header value into seconds, or None."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
        return seconds if seconds >= 0 else None
    except ValueError:
        pass
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
    """Run *fn* with exponential backoff, honouring Retry-After on 429s."""
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
    raise last_exc


def http_get(url: str, headers: dict | None = None, ssl_context=None) -> str:
    def _do():
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'automation-bot/1.0', **(headers or {})},
        )
        kwargs = {'timeout': 30}
        if ssl_context is not None:
            kwargs['context'] = ssl_context
        with urllib.request.urlopen(req, **kwargs) as r:
            return r.read().decode('utf-8')
    return _retry(_do)


def http_post(url: str, data: dict, headers: dict | None = None, ssl_context=None) -> dict:
    body = json.dumps(data).encode('utf-8')
    def _do():
        req = urllib.request.Request(
            url,
            data=body,
            headers={'Content-Type': 'application/json', 'User-Agent': 'automation-bot/1.0', **(headers or {})},
            method='POST',
        )
        kwargs = {'timeout': 30}
        if ssl_context is not None:
            kwargs['context'] = ssl_context
        with urllib.request.urlopen(req, **kwargs) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    return _retry(_do)


def http_patch(url: str, data: dict, headers: dict | None = None, ssl_context=None) -> dict:
    body = json.dumps(data).encode('utf-8')
    def _do():
        req = urllib.request.Request(
            url,
            data=body,
            headers={'Content-Type': 'application/json', 'User-Agent': 'automation-bot/1.0', **(headers or {})},
            method='PATCH',
        )
        kwargs = {'timeout': 30}
        if ssl_context is not None:
            kwargs['context'] = ssl_context
        with urllib.request.urlopen(req, **kwargs) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    return _retry(_do)


def notion_headers() -> dict:
    return {
        'Authorization': f'Bearer {os.environ["NOTION_TOKEN"]}',
        'Notion-Version': '2022-06-28',
    }


def is_valid_iso8601_date(value: str) -> bool:
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


def truncate_for_notion(value: str, limit: int = 2000) -> str:
    """Truncate *value* so its UTF-16 code-unit length fits Notion's limit."""
    if not value:
        return value
    truncated = value[:limit]
    while len(truncated.encode('utf-16-le')) // 2 > limit:
        truncated = truncated[:-1]
    return truncated


# ---------------------------------------------------------------------------
# Alert suppression
# ---------------------------------------------------------------------------

_DEFAULT_ALERT_SUPPRESS_MINUTES = 60


def load_alert_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_alert_state(state: dict, path: str) -> None:
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except OSError as e:
        print(f'[alert_state] failed to save: {e}', file=sys.stderr)


def should_suppress_alert(
    key: str,
    state_path: str,
    suppress_minutes: int = _DEFAULT_ALERT_SUPPRESS_MINUTES,
    now: datetime | None = None,
) -> bool:
    """Return True if an alert with *key* was sent within the suppress window."""
    state = load_alert_state(state_path)
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


def record_alert_sent(
    key: str,
    state_path: str,
    now: datetime | None = None,
) -> None:
    state = load_alert_state(state_path)
    state[key] = (now or datetime.now(timezone.utc)).isoformat()
    save_alert_state(state, state_path)


def warn_discord(
    message: str,
    script_name: str,
    alert_state_path: str,
    dedup_key: str | None = None,
    suppress_minutes: int = _DEFAULT_ALERT_SUPPRESS_MINUTES,
) -> None:
    """Send a system-level warning to the dedicated alerts webhook.

    If *dedup_key* is provided and was sent within *suppress_minutes*,
    the alert is silently skipped. Timestamp is only recorded after a
    successful POST.

    When side effects are disabled (a sandbox/self-improvement run), the
    alert is printed but not posted, so an observing agent still sees it
    without spamming the production alerts channel.
    """
    if not side_effects_enabled():
        print(f'[observe-only] would alert ({script_name}): {message}')
        return
    if dedup_key and should_suppress_alert(dedup_key, alert_state_path, suppress_minutes):
        print(f'[alert] Suppressing duplicate alert (key={dedup_key})')
        return
    webhook_url = os.environ.get('DISCORD_ALERTS_WEBHOOK_URL')
    if not webhook_url:
        print('DISCORD_ALERTS_WEBHOOK_URL not set — skipping alert.')
        return
    try:
        http_post(webhook_url, {'content': f'[framerlabs-automations] {message}'})
    except urllib.error.HTTPError as e:
        discord_response = ''
        try:
            discord_response = e.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        print(f'Failed to send Discord alert: {e}')
        error_log.log_error(
            script_name, 'warning',
            'Failed to send Discord alert',
            {'status': e.code, 'error': str(e), 'discord_response': discord_response},
        )
        return
    except Exception as e:
        print(f'Failed to send Discord alert: {e}')
        error_log.log_error(script_name, 'warning', 'Failed to send Discord alert', {'error': str(e)})
        return
    if dedup_key:
        record_alert_sent(dedup_key, alert_state_path)


def write_summary(text: str) -> None:
    """Append a markdown summary to the GitHub Actions job summary file."""
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not path:
        return
    try:
        with open(path, 'a') as f:
            f.write(text + '\n')
    except OSError:
        pass
