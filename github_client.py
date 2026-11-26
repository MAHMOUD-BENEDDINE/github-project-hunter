import base64
from collections import OrderedDict
from copy import deepcopy
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import logging
import math
import random
import time

import requests

from cache import PersistentCache
from config import Settings

logger = logging.getLogger("github_project_hunter.client")


class GitHubAPIError(RuntimeError):
    def __init__(self, message, *, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class GitHubClient:
    BASE_URL = "https://api.github.com"

    def __init__(
        self,
        settings: Settings,
        session=None,
        sleep=time.sleep,
        monotonic=time.monotonic,
        wall_time=time.time,
        disk_cache: PersistentCache | None = None,
    ):
        self.settings = settings
        self.session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self.sleep = sleep
        self.monotonic = monotonic
        self.wall_time = wall_time
        self._last_request = None
        self._cache = OrderedDict()
        self.rate_headers = {}
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": settings.api_version,
            "User-Agent": "github-project-hunter/0.2",
        }
        if settings.token:
            self.headers["Authorization"] = f"Bearer {settings.token}"

        # Initialize L2 persistent disk cache
        if disk_cache is not None:
            self.disk_cache = disk_cache
        elif getattr(settings, "persistent_cache_enabled", True):
            try:
                self.disk_cache = PersistentCache(cache_dir=getattr(settings, "cache_dir", ".cache"))
            except Exception as exc:
                logger.warning("Could not initialize persistent cache: %s", exc)
                self.disk_cache = None
        else:
            self.disk_cache = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        if self._owns_session:
            self.session.close()

    def _pace(self):
        interval = 2.1 if self.settings.token else 6.1
        if self._last_request is not None:
            remaining = interval - (self.monotonic() - self._last_request)
            if remaining > 0:
                self.sleep(remaining)
        self._last_request = self.monotonic()

    def _header_delay(self, headers):
        delays = []
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                seconds = float(retry_after)
                if math.isfinite(seconds):
                    delays.append(max(0.0, seconds))
            except ValueError:
                try:
                    when = parsedate_to_datetime(retry_after)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    delays.append(max(0.0, when.timestamp() - self.wall_time()))
                except (ValueError, TypeError, OverflowError):
                    pass

        if headers.get("X-RateLimit-Remaining") == "0":
            try:
                reset = float(headers.get("X-RateLimit-Reset", ""))
                if math.isfinite(reset):
                    delays.append(max(0.0, reset - self.wall_time()) + 1.0)
            except ValueError:
                pass

        return max(delays) if delays else None

    def _wait_or_raise(self, delay, error, attempt):
        if attempt + 1 >= self.settings.max_attempts or delay > self.settings.max_wait:
            raise error
        self.sleep(delay)

    def _cache_disk_key(self, path: str, params: dict) -> str:
        return f"{path}:{json.dumps(params, sort_keys=True, default=str)}"

    def get_json(self, path, params=None):
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "?" in path
            or "#" in path
            or ".." in path
        ):
            raise ValueError("Use a relative GitHub API path and separate params.")

        params = dict(params or {})
        key = (path, tuple(sorted(params.items())))

        # 1. Check L1 in-memory cache
        cached = self._cache.get(key)
        if cached:
            expires, payload = cached
            if expires > self.monotonic():
                self._cache.move_to_end(key)
                return deepcopy(payload)
            del self._cache[key]

        # 2. Check L2 persistent disk cache
        if self.disk_cache and self.settings.cache_ttl > 0:
            disk_key = self._cache_disk_key(path, params)
            cached_disk = self.disk_cache.get(disk_key, now=self.wall_time())
            if cached_disk is not None:
                # Promote to L1 memory cache
                self._cache[key] = (
                    self.monotonic() + self.settings.cache_ttl,
                    deepcopy(cached_disk),
                )
                self._cache.move_to_end(key)
                return deepcopy(cached_disk)

        for attempt in range(self.settings.max_attempts):
            self._pace()
            try:
                logger.debug("GET %s%s with params %s", self.BASE_URL, path, params)
                response = self.session.get(
                    self.BASE_URL + path,
                    params=params,
                    headers=self.headers,
                    timeout=(
                        self.settings.connect_timeout,
                        self.settings.read_timeout,
                    ),
                    allow_redirects=False,
                )
            except (requests.Timeout, requests.ConnectionError):
                delay = 2 ** attempt + random.uniform(0, 0.25)
                error = GitHubAPIError("GitHub request timed out or the connection failed.")
                self._wait_or_raise(delay, error, attempt)
                continue
            except requests.RequestException:
                raise GitHubAPIError("Unable to send the GitHub request.") from None

            try:
                status = response.status_code
                headers = response.headers
                self.rate_headers = {
                    k.lower(): v
                    for k, v in headers.items()
                    if k.lower().startswith("x-ratelimit-")
                }
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
            finally:
                response.close()

            if status == 200:
                if not isinstance(payload, dict):
                    raise GitHubAPIError("GitHub returned invalid JSON data.")
                if self.settings.cache_ttl > 0:
                    self._cache[key] = (
                        self.monotonic() + self.settings.cache_ttl,
                        deepcopy(payload),
                    )
                    self._cache.move_to_end(key)
                    while len(self._cache) > self.settings.cache_entries:
                        self._cache.popitem(last=False)

                    if self.disk_cache:
                        disk_key = self._cache_disk_key(path, params)
                        self.disk_cache.set(
                            disk_key,
                            payload,
                            self.settings.cache_ttl,
                            now=self.wall_time(),
                        )
                return payload

            message = (
                str(payload.get("message", "")).lower()
                if isinstance(payload, dict)
                else ""
            )
            rate_limited = status == 429 or (
                status == 403
                and (
                    headers.get("X-RateLimit-Remaining") == "0"
                    or "Retry-After" in headers
                    or "rate limit" in message
                    or "abuse" in message
                )
            )

            if rate_limited:
                delay = self._header_delay(headers)
                if delay is None:
                    delay = 60.0 * (2 ** attempt)
                else:
                    delay = max(delay, float(2 ** attempt))
                error = GitHubAPIError(
                    f"GitHub rate limit reached. Retry after at least {math.ceil(delay)} seconds.",
                    status=status,
                    retry_after=delay,
                )
                self._wait_or_raise(delay, error, attempt)
                continue

            if status in (500, 502, 503, 504):
                delay = max(
                    2 ** attempt + random.uniform(0, 0.25),
                    self._header_delay(headers) or 0,
                )
                error = GitHubAPIError(
                    f"GitHub temporarily failed with HTTP {status}.",
                    status=status,
                )
                self._wait_or_raise(delay, error, attempt)
                continue

            messages = {
                401: "GitHub rejected the token. Check GITHUB_TOKEN in .env.",
                403: "GitHub denied this request. Check token permissions.",
                404: "GitHub resource was not found or is not accessible.",
                410: "GitHub resource or API version is no longer available.",
                422: "GitHub rejected the query. Simplify keywords or filters.",
            }
            raise GitHubAPIError(
                messages.get(status, f"GitHub returned HTTP {status}."),
                status=status,
            )

        raise GitHubAPIError("GitHub request failed after bounded retries.")

    def get_rate_limit(self) -> dict:
        """Fetches current GitHub API rate limit status."""
        try:
            return self.get_json("/rate_limit")
        except GitHubAPIError as exc:
            logger.warning("Could not fetch rate limit: %s", exc)
            return {}

    def get_readme(self, owner: str, repo: str) -> str | None:
        """Fetches repository README content if accessible."""
        try:
            payload = self.get_json(f"/repos/{owner}/{repo}/readme")
            if isinstance(payload, dict) and "content" in payload:
                encoding = payload.get("encoding", "")
                if encoding == "base64":
                    content_bytes = base64.b64decode(payload["content"])
                    return content_bytes.decode("utf-8", errors="replace")
            return None
        except Exception as exc:
            logger.debug("README not available for %s/%s: %s", owner, repo, exc)
            return None
