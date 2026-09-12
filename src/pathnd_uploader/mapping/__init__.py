from .base import SourceProfile, apply_profile
from .bdr import BDR_PROFILE

PROFILES: dict[str, SourceProfile] = {
    "bdr": BDR_PROFILE,
}

__all__ = ["SourceProfile", "apply_profile", "PROFILES", "BDR_PROFILE"]
