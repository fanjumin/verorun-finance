#!/usr/bin/env python3
"""memory_engine plugin — database layer.

Provides get_memory_engine_db() factory and the EVOLUTION_PHASES config
shared between backend routes and frontend rendering.
"""

from plugins._base.db import get_raw_connection, PgConnection

SCHEMA = 'memory_engine'

# Evolution Ring phase configuration (docs Appendix C.2).
# Order matters: ring draws phases in this sequence.
# Each entry: key (backend identifier), label_en (English), label_zh (Chinese).
EVOLUTION_PHASES = [
    {'key': 'task_event',    'label_en': 'Task Event',    'label_zh': 'Task Event'},
    {'key': 'mem_extract',   'label_en': 'Memory Extract', 'label_zh': 'Memory Extract'},
    {'key': 'reflexion',     'label_en': 'Reflexion',     'label_zh': 'Reflexion'},
    {'key': 'experience',    'label_en': 'Experience',    'label_zh': 'Experience'},
    {'key': 'prompt_evolve', 'label_en': 'Prompt Evolve', 'label_zh': 'Prompt Evolve'},
]


def get_memory_engine_db():
    """Open a connection with the plugin schema on the search path.

    All plugin tables resolve to memory_engine.*; main-DB tables must be
    referenced explicitly as public.<table>.
    """
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SET search_path TO %s, public", (SCHEMA,))
    return PgConnection(conn)
