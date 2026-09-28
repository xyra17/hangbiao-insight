"""Postgres checkpointer fenced by the investigation's execution generation."""
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row


class FencedPostgresSaver(PostgresSaver):
    run_id = None
    lease_token = None

    @contextmanager
    def _cursor(self, *, pipeline=False):
        # Keep the lease read and checkpoint write in one transaction. The share
        # lock prevents a new owner from claiming the run during that write.
        with self.lock:
            if self.run_id is None:
                with self.conn.cursor(binary=True, row_factory=dict_row) as cursor:
                    yield cursor
            else:
                with self.conn.transaction(), self.conn.cursor(binary=True, row_factory=dict_row) as cursor:
                    cursor.execute(
                        "SELECT lease_token FROM insight_investigationrun WHERE id = %s FOR SHARE",
                        (self.run_id,),
                    )
                    row = cursor.fetchone()
                    if not row or row["lease_token"] != self.lease_token:
                        raise RuntimeError("investigation_lease_superseded")
                    yield cursor
