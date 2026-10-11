class ParserError(Exception):
    """Base class for every error the parser raises on purpose."""


class PasswordRequired(ParserError):
    pass


class UnsupportedFormat(ParserError):
    pass


class ScannedDocument(ParserError):
    pass


class LLMUnavailable(ParserError):
    pass


class LayoutNotUnderstood(ParserError):
    pass
