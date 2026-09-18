# backend/app/pipeline/dedupe/__init__.py
"""Within-dataset dedupe stages.

These replace the two-dataset linkage stages in ``app/pipeline`` one slice at a
time. Slice 1 has one stage: load the records.
"""
