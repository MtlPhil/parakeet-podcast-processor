"""Database layer using DuckDB for P³ storage.

The methods live in ``p3.db``, one module per subject, each class building on
the one before it: connection, schema, library (podcasts and episodes),
digests (transcripts and summaries), jobs, users, search.
"""

from .db.jobs import INTERRUPTED_ERROR
from .db.search import Search

__all__ = ["INTERRUPTED_ERROR", "P3Database"]


class P3Database(Search):
    """Everything P3 stores. Each thread gets its own cursor (see ``conn``)."""
