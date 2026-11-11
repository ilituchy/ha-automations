"""GET-only JSON fetching with an on-disk cache.

Every outbound request is logged by URL before it is sent (porting doc §6.1:
the inputs to a recommendation are what you will be asked to defend).

Caching is per-URL with a caller-chosen TTL. ``ttl=None`` means "cache
forever", which is right for completed seasons: a 2024 transaction list will
never change, and refetching four seasons x 19 weeks every run would be ~80
pointless calls. Sleeper's stated ceiling is 1000 calls/minute with an IP block
(not a 429) as the penalty, so the cache is a safety feature, not an
optimisation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)

USER_AGENT = "dynasty-advisor/0.1 (personal, non-commercial)"


class FetchError(Exception):
    pass


class CachedHttp:
    def __init__(self, cache_dir: str, *, offline: bool = False) -> None:
        self.cache_dir = cache_dir
        self.offline = offline
        self.calls = 0
        os.makedirs(cache_dir, exist_ok=True)

    def _path(self, url: str) -> str:
        return os.path.join(self.cache_dir, hashlib.sha1(url.encode()).hexdigest() + ".json")

    def fetched_at(self, url: str) -> Optional[float]:
        p = self._path(url)
        return os.path.getmtime(p) if os.path.exists(p) else None

    def get_json(self, url: str, ttl: Optional[float]) -> Any:
        path = self._path(url)
        if os.path.exists(path):
            age = time.time() - os.path.getmtime(path)
            if self.offline or ttl is None or age < ttl:
                with open(path) as f:
                    return json.load(f)
        if self.offline:
            raise FetchError("offline and not cached: %s" % url)
        data = self._fetch(url)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return data

    def _fetch(self, url: str) -> Any:
        last: Optional[BaseException] = None
        for attempt in range(4):
            self.calls += 1
            log.info("GET %s", url)
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise FetchError("HTTP %s for %s" % (e.code, url)) from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
                time.sleep(2.0 * (attempt + 1))
        raise FetchError("giving up on %s: %s" % (url, last))
