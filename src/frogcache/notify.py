"""ntfy notifications (info on successful updates, alert on failures).

Governing ADR: docs/decisions/ADR-001.
"""

import sys
import urllib.error
import urllib.request

from frogcache.data_models.config import Config
from frogcache.net import SAFE_OPENER, checked_url, redact_url


def send_ntfy(config: Config, title: str, message: str, priority: str = "default") -> None:
    """POST a notification to the configured ntfy topic.

    Args:
        config: runtime config; ntfy_url/ntfy_token must be set.
        title: notification title (ntfy 'Title' header).
        message: notification body.
        priority: ntfy priority (min|low|default|high|urgent).
    Raises:
        RuntimeError: if not configured or the POST fails. Callers decide
            whether a notification failure should fail the run (it should not —
            the cache update itself is the primary outcome).
    """
    if not config.ntfy_url or not config.ntfy_token:
        raise RuntimeError("NTFY_URL/NTFY_TOKEN not configured; cannot notify")
    request = urllib.request.Request(
        checked_url(config.ntfy_url),
        data=message.encode(),
        headers={
            "Authorization": f"Bearer {config.ntfy_token}",
            "Title": title,
            "Priority": priority,
            "Tags": "package,frogcache",
        },
        method="POST",
    )
    try:
        # SAFE_OPENER re-validates redirects and strips Authorization cross-host,
        # so the bearer token can never leak to a redirect target
        with SAFE_OPENER.open(request, timeout=config.http_timeout) as response:
            if response.status >= 300:
                raise RuntimeError(f"POST {redact_url(config.ntfy_url)} -> HTTP {response.status}")
    except urllib.error.URLError as err:
        raise RuntimeError(f"POST {redact_url(config.ntfy_url)} failed: {err}") from err


def notify_safe(config: Config, title: str, message: str, priority: str = "default") -> None:
    """send_ntfy that never raises — logs to stderr instead.

    Args:
        config: runtime config.
        title: notification title.
        message: notification body.
        priority: ntfy priority.
    """
    try:
        send_ntfy(config, title, message, priority)
    except RuntimeError as err:
        print(f"notification failed: {err}", file=sys.stderr)
