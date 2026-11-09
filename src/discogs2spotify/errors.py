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
