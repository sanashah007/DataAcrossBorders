from enum import Enum


class GenericCategory(str, Enum):
    """Fixed, nominal top-level category for a study. Shared across all hospital nodes."""

    NEURO = "Neuro"
    CARDIAC = "Cardiac"
    CORONARY_VASCULAR = "Coronary/Vascular"
    PULMONARY = "Pulmonary"
    GI = "GI"
    RENAL_GU = "Renal/GU"
    MSK = "MSK"
    OB_FETAL = "OB/Fetal"
    OTHER = "Other"


DIMENSIONS = ("location", "finding_type", "size", "other")
STATUSES = ("present", "absent", "uncertain")
