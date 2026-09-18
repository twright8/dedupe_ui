# backend/app/pipeline/dedupe/__init__.py
"""Within-dataset dedupe stages.

These replace the two-dataset linkage stages in ``app/pipeline`` one slice at a
time. Three stages exist so far: load the records, assign tracks and clean, then
group them on the ruleset's match keys.
"""

# What GET /api/pipeline/stages serves. One entry per stage that exists today —
# never a stage that is only planned.
STAGES = [
    {
        "key": "load",
        "label": "Load",
        "description": "Read the input file into one row per record (records_raw.parquet).",
    },
    {
        "key": "clean",
        "label": "Clean",
        "description": (
            "Assign each record a track and run that track's cleaning rules "
            "(records.parquet)."
        ),
    },
    {
        "key": "exact",
        "label": "Exact keys",
        "description": (
            "Group records that agree on a match key, hold the groups a guard "
            "stops, and score the result against the existing labels "
            "(exact_groups.parquet)."
        ),
    },
]
