from .base import SourceProfile, apply_profile
from .bdr import BDR_PROFILE
from .part import PART_PROFILE

PROFILES: dict[str, SourceProfile] = {
    "bdr": BDR_PROFILE,
    "part": PART_PROFILE,
}

__all__ = ["SourceProfile", "apply_profile", "PROFILES", "BDR_PROFILE", "PART_PROFILE"]
