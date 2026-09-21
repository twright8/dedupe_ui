# backend/app/pipeline/dedupe/__init__.py
"""Within-dataset dedupe stages.

These replace the two-dataset linkage stages in ``app/pipeline`` one slice at a
time. Six stages run today: load the records, give each one a track and clean
it, put together the records a match key agrees on, score the pairs the keys
left undecided, cluster what was accepted, and give every record a durable
entity ID.

The names a user sees are in ``app/vocabulary.py``, with every other label the
tool shows. Stages are named and never numbered (`docs/DESIGN.md` D21): three
numberings are in use and renumbering one breaks the other two.
"""

from app import vocabulary

# What GET /api/pipeline/stages serves. The list includes the derived-column
# step and the model step, which run inside and beside the others, because the
# How it works page names all eight and this list is what it reads.
STAGES = [dict(stage) for stage in vocabulary.STAGES]
