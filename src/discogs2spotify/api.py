import math
import time
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

from .errors import APIError, SpotifyRateLimitError

LOGGER = logging.getLogger("discogs2spotify.api")
MAX_AUTOMATIC_RETRY_DELAY = 300


class HTTPTransport:
    def __init__(self, delay=1.05, retries=5, sleeper=time.sleep):
        self.delay = delay
        self.retries = retries
        self.sleeper = sleeper
        self.last_request = {}
        self.request_count = 0
        self.rate_limit_count = 0

    def request(self, service, url, method="GET", headers=None, data=None):
        host = urlsplit(url).netloc
        if data is not None and LOGGER.isEnabledFor(logging.DEBUG):
            content_type = next(
                (
                    value
                    for key, value in (headers or {}).items()
                    if key.lower() == "content-type"
                ),
                "unknown",
            )
            endpoint = urlsplit(url).path or "/"
            if content_type.split(";", 1)[0].strip().lower() == "application/json":
                LOGGER.debug(
                    "%s %s %s payload=%s",
                    service,
                    method,
                    endpoint,
                    data.decode("utf-8", errors="replace"),
                )
            else:
                LOGGER.debug(
                    "%s %s %s payload=<%s; %d bytes>",
                    service,
                    method,
                    endpoint,
                    content_type,
                    len(data),
                )
        for attempt in range(self.retries + 1):
            elapsed = time.monotonic() - self.last_request.get(host, 0)
            if self.delay > elapsed:
                self.sleeper(self.delay - elapsed)
            request = urllib.request.Request(
                url, data=data, headers=headers or {}, method=method
            )
            LOGGER.debug("%s %s %s", service, method, urlsplit(url).path or "/")
            try:
                self.request_count += 1
                with urllib.request.urlopen(request, timeout=30) as response:
                    self.last_request[host] = time.monotonic()
                    return response.read()
            except urllib.error.HTTPError as error:
                self.last_request[host] = time.monotonic()
                endpoint = urlsplit(url).path or "/"
                if error.code == 429:
                    self.rate_limit_count += 1
                try:
                    error_body = error.read(4096).decode("utf-8", errors="replace")
                    LOGGER.debug(
                        "%s %s %s response=%s",
                        service,
                        method,
                        endpoint,
                        error_body,
                    )
                    parsed_error = json.loads(error_body)
                    error_info = (
                        parsed_error.get("error")
                        if isinstance(parsed_error, dict)
                        else None
                    )
                    detail = (
                        error_info.get("message")
                        if isinstance(error_info, dict)
                        else None
                    )
                    reason = (
                        error_info.get("reason")
                        if isinstance(error_info, dict)
                        else None
                    )
                except (ValueError, AttributeError, KeyError, OSError):
                    detail = None
                    reason = None
                retry_after = error.headers.get("Retry-After") if error.headers else None
                parsed_retry_after = None
                if retry_after is not None:
                    try:
                        value = float(retry_after)
                        if math.isfinite(value) and value >= 0:
                            parsed_retry_after = value
                    except (TypeError, ValueError):
                        pass
                if error.code == 429 and service == "Spotify":
                    spotify_message = str(detail or "")
                    normalized_reason = str(reason or "").upper()
                    if normalized_reason == "QUOTA_EXCEEDED":
                        message = spotify_message or "Spotify quota exhausted"
                        if parsed_retry_after is not None:
                            message += "; server requested a retry after {} seconds".format(
                                int(parsed_retry_after)
                            )
                        raise SpotifyRateLimitError(
                            endpoint,
                            parsed_retry_after,
                            message,
                            normalized_reason,
                        )
                    if parsed_retry_after is None:
                        raise SpotifyRateLimitError(
                            endpoint,
                            message=(
                                "Spotify HTTP 429 without a usable Retry-After value"
                                + (
                                    ": {}".format(spotify_message)
                                    if spotify_message
                                    else ""
                                )
                            ),
                            reason=normalized_reason or None,
                        )
                    if parsed_retry_after > MAX_AUTOMATIC_RETRY_DELAY:
                        message = spotify_message or "Spotify rate limit"
                        message += "; server requested a retry after {} seconds".format(
                            int(parsed_retry_after)
                        )
                        raise SpotifyRateLimitError(
                            endpoint,
                            parsed_retry_after,
                            message,
                            normalized_reason or None,
                        )
                    if attempt < self.retries:
                        LOGGER.info(
                            "Spotify rate limit reached at %s; retrying after %.1f seconds",
                            endpoint,
                            parsed_retry_after,
                        )
                        self.sleeper(parsed_retry_after)
                        continue
                    raise SpotifyRateLimitError(
                        endpoint,
                        parsed_retry_after,
                        "{}; rate limit persisted after automatic retries".format(
                            spotify_message or "Spotify rate limit"
                        ),
                        normalized_reason or None,
                    )
                if error.code == 429 or 500 <= error.code < 600:
                    if attempt < self.retries:
                        try:
                            delay = float(retry_after)
                        except (TypeError, ValueError):
                            delay = min(2 ** attempt, 30)
                        if delay > MAX_AUTOMATIC_RETRY_DELAY:
                            raise APIError(
                                service,
                                error.code,
                                endpoint,
                                "{}server requested a retry after {} seconds; "
                                "stopping instead of waiting more than {} seconds. "
                                "Rerun after the indicated wait".format(
                                    (detail + "; ") if detail else "",
                                    int(delay),
                                    MAX_AUTOMATIC_RETRY_DELAY,
                                ),
                            )
                        LOGGER.debug(
                            "%s %s returned HTTP %s; retry %d/%d in %.1fs",
                            service, endpoint, error.code, attempt + 1, self.retries, delay,
                        )
                        self.sleeper(max(0.0, delay))
                        continue
                message = ""
                if error.code == 429:
                    message = "rate limited after {} retries".format(attempt)
                elif 500 <= error.code < 600:
                    message = "server error after {} retries".format(attempt)
                elif detail:
                    message = detail[:240]
                elif error.code in (400, 401, 403, 404, 409, 422):
                    message = "request was rejected"
                raise APIError(service, error.code, endpoint, message)
            except (urllib.error.URLError, TimeoutError) as error:
                self.last_request[host] = time.monotonic()
                if attempt < self.retries:
                    LOGGER.debug(
                        "%s %s network failure; retry %d/%d",
                        service, endpoint, attempt + 1, self.retries,
                    )
                    self.sleeper(min(2 ** attempt, 30))
                    continue
                endpoint = urlsplit(url).path or "/"
                reason = error.reason if isinstance(error, urllib.error.URLError) else error
                raise APIError(
                    service,
                    "network",
                    endpoint,
                    "{} after {} retries".format(reason, attempt),
                )
        raise APIError(service, "network", urlsplit(url).path or "/", "retry limit exceeded")


def json_request(transport, service, url, method="GET", headers=None, payload=None):
    import json

    request_headers = dict(headers or {})
    data = None
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    response = transport.request(service, url, method, request_headers, data)
    if not response:
        return {}
    try:
        return json.loads(response.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise APIError(service, "invalid response", urlsplit(url).path, str(error))
