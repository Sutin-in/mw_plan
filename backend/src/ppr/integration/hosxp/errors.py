"""HOSxP boundary errors."""

from __future__ import annotations


class HosxpError(Exception):
    """Base class for HOSxP integration errors."""


class HosxpUnavailableError(HosxpError):
    """HOSxP could not be reached. New PPRs must not be created from stale data (spec §26)."""


class HosxpConfigurationError(HosxpUnavailableError):
    """The site's HOSxP query configuration is missing or wrong (bad SQL, wrong columns).

    It is a subclass of ``HosxpUnavailableError`` on purpose: callers must refuse to act
    exactly as when HOSxP is down, never proceed with partial or guessed data.
    """
