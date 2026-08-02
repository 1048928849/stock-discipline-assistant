from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session


@contextmanager
def transaction_scope(db: Session) -> Iterator[Session]:
    """Own a transaction, or a savepoint when the caller already owns one."""
    transaction = db.begin_nested() if db.in_transaction() else db.begin()
    try:
        yield db
    except Exception:
        transaction.rollback()
        raise
    else:
        transaction.commit()
