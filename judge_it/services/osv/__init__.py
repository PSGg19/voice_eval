"""OSV (orchestrator-service) client package.

Usage:
    from judge_it.services.osv import osv_client
    meta = await osv_client.get_interaction_metadata(interaction_uuid)
"""

from judge_it.services.osv.client import (
    REGION_BASE_URLS,
    OSVClient,
    OSVError,
    client_for_region,
    normalise_region,
    osv_client,
    parse_interaction_ref,
)

__all__ = [
    "REGION_BASE_URLS",
    "OSVClient",
    "OSVError",
    "client_for_region",
    "normalise_region",
    "osv_client",
    "parse_interaction_ref",
]
