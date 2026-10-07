"""In-memory mock HOSxP (spec §42). Test and development use only.

All identifiers in mock data are prefixed ``MOCK-`` so they can never be mistaken
for real HOSxP identifiers or codes.
"""

from ppr.integration.hosxp.mock.auth import MockHosxpAuthenticationAdapter
from ppr.integration.hosxp.mock.gateway import MockHosxpData, MockHosxpGateway

__all__ = ["MockHosxpAuthenticationAdapter", "MockHosxpData", "MockHosxpGateway"]
