"""A urllib response stand-in for tests that patch a module's ``_open``."""

import io


class Response(io.BytesIO):
    """Readable body with the status, headers and final URL urllib reports."""

    def __init__(self, body=b"", status=200, headers=None, *, url=""):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}
        self.url = url

    def geturl(self):
        return self.url
