from datetime import datetime, timedelta


class SyncError(Exception):
    """Expected configuration, API, or synchronization failure."""


class APIError(SyncError):
    def __init__(self, service, status, endpoint, message=""):
        self.service = service
        self.status = status
        self.endpoint = endpoint
        self.message = message
        super().__init__(
            "{} API HTTP {} at {}{}".format(
                service,
                status,
                endpoint,
                ": " + message if message else "",
            )
        )


class SpotifyRateLimitError(APIError):
    def __init__(
        self,
        endpoint,
        retry_after=None,
        message="",
        reason=None,
        status=429,
    ):
        self.retry_after = retry_after
        self.spotify_message = message
        self.reason = reason
        self.retry_at = (
            datetime.now().astimezone() + timedelta(seconds=retry_after)
            if retry_after is not None
            else None
        )
        super().__init__(
            "Spotify",
            status,
            endpoint,
            message or "HTTP 429 without a usable Retry-After value",
        )
