"""Awesome Statement Parser — universal, fully local bank & credit card statement parsing."""
from .errors import LayoutNotUnderstood, LLMUnavailable, ParserError, PasswordRequired, ScannedDocument, UnsupportedFormat
from .models import LayoutSpec, ParseResult
from .pipeline import parse_statement, parse_statement_sync
from .serialize import result_to_dict

__version__ = "0.1.0"

__all__ = [
    "parse_statement",
    "parse_statement_sync",
    "result_to_dict",
    "LayoutSpec",
    "ParseResult",
    "ParserError",
    "PasswordRequired",
    "UnsupportedFormat",
    "ScannedDocument",
    "LLMUnavailable",
    "LayoutNotUnderstood",
]
