# backend/app/profiles/psc.py
"""PSC profile — a stub. The bulk loader arrives with slice 8."""

from pathlib import Path

import pandas as pd

from app.profiles.base import DEFAULT_TRACKS, DisplayColumn, InputSpec, Profile


class PscProfile(Profile):
    """People with significant control — persons and businesses across companies."""

    def __init__(self):
        super().__init__(
            key="psc",
            title="PSC reconciliation",
            subtitle="Give one entity ID to PSC records that are the same person or business",
            input=InputSpec(
                label="PSC snapshot",
                extensions=[".zip"],
                help="The Companies House bulk PSC snapshot.",
            ),
            tracks=list(DEFAULT_TRACKS),
            display_columns=[
                DisplayColumn("name", "Name", "text"),
                DisplayColumn("track", "Track", "text"),
            ],
            priority_columns=[],
        )

    def load_records(self, input_path: Path) -> tuple[pd.DataFrame, dict]:
        raise NotImplementedError(
            "The PSC loader is not built yet. Run the donations profile instead."
        )
