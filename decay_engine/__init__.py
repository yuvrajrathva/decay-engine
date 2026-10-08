"""decay-engine: keep an AI agent's memory store clean over time.

    from decay_engine import DecayEngine, DecayPolicy
    from decay_engine.adapters.chroma import ChromaAdapter

    engine = DecayEngine(ChromaAdapter(collection))
    print(engine.sweep().summary())        # dry run by default
    engine.sweep(dry_run=False)            # actually apply
"""

from .engine import (
    COLD_FLAG,
    STRENGTH_FIELD,
    SWEPT_AT_FIELD,
    DecayEngine,
    EvictionLimitExceeded,
    ScopeNotSupported,
    SweepReport,
)
from .models import FieldMap, MemoryRecord, MemorySignals, utcnow
from .policies.base import Action, Policy, Verdict
from .policies.decay import DecayPolicy

__version__ = "0.1.0"

__all__ = [
    "Action",
    "COLD_FLAG",
    "DecayEngine",
    "DecayPolicy",
    "EvictionLimitExceeded",
    "FieldMap",
    "MemoryRecord",
    "MemorySignals",
    "Policy",
    "STRENGTH_FIELD",
    "ScopeNotSupported",
    "SWEPT_AT_FIELD",
    "SweepReport",
    "Verdict",
    "__version__",
    "utcnow",
]
