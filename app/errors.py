class RetryableError(Exception):
    """A temporary failure. Exception messages must not be logged."""


class PermanentError(Exception):
    """A request that should not be retried."""
