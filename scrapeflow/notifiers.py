"""Notification channels.

No channels ship yet – everything is visible in the dashboard and via the JSON API.
To add one (e-mail, ntfy, Slack, ...), subclass ``Notifier`` and append an instance to
``NOTIFIERS`` (e.g. based on settings). Checks call ``notify_all`` when new or changed files
were stored.
"""

import logging

from .models import CheckRun, Site

log = logging.getLogger(__name__)


class Notifier:
    name = "base"

    async def send(self, site: Site, run: CheckRun) -> None:  # pragma: no cover
        raise NotImplementedError


NOTIFIERS: list[Notifier] = []


async def notify_all(site: Site, run: CheckRun) -> bool:
    """Send the result to every configured channel. Returns True if at least one succeeded."""
    delivered = False
    for notifier in NOTIFIERS:
        try:
            await notifier.send(site, run)
            delivered = True
        except Exception:
            log.exception("Notifier %s failed for site %s", notifier.name, site.id)
    return delivered
