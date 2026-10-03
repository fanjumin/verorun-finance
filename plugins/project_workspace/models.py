#!/usr/bin/env python3
"""project_workspace/models.py — Database layer for project_workspace plugin.

PostgreSQL schema: project_workspace (single-DB multi-schema, plugin standard sec.9.1).

All tables use project_id as the isolation boundary. Every query MUST
include a WHERE project_id = ? filter to enforce project isolation.

Key tables:
  - projects:          Top-level isolation unit
  - project_members:   Collaboration access control
  - documents:         Uploaded documents with processing status
  - document_chunks:   Vectorized fragments for RAG (pgvector)
  - citations:         Extracted references
  - qa_logs:           Query history with source traceability
"""

from plugins._base.db import get_raw_connection, PgConnection

SCHEMA = 'project_workspace'


def get_db():
    """Open a connection with the plugin schema on the search path.

    All plugin tables resolve to project_workspace.*;
    main-DB tables must be referenced explicitly as public.<table>.
    """
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SET search_path TO %s, public", (SCHEMA,))
    return PgConnection(conn)
