class Error(Exception):
    def __init__(self, text, code=1, reason=None):
        super().__init__(text)
        self.code = code
        self.reason_code = reason  # a machine-readable reason, for the refusals that have one


def blocked(exc):
    """Whether an OS error is what a sandbox gives: a permission error, or a read-only file system."""
    import errno
    return isinstance(exc, PermissionError) or getattr(exc, "errno", None) == errno.EROFS


def store_error(exc):
    text = "The store couldn't be read or written. Check its permissions and available space."
    if blocked(exc):
        text += " Probably blocked by the session's sandbox; use the kaiusap tools instead."
    return Error(text, 4)
