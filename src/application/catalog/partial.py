"""Request-local aggregate metadata; never stored on the shared use case."""


class PartialRows(list):
    def __init__(self, rows=(), *, partial=False, rate_limited=False):
        super().__init__(rows)
        self.partial = partial
        self.rate_limited = rate_limited


class CommercialRows(dict):
    def __init__(self):
        super().__init__()
        self.error: Exception | None = None

    def record_error(self, error):
        # Preserve Retry-After over less specific transport/validation failures.
        if self.error is None or getattr(error, "status", None) == 429:
            self.error = error
