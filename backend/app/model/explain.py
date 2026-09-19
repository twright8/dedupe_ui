# backend/app/model/explain.py
"""Why the model gave one pair the score it did.

LightGBM's `pred_contrib` returns exact Tree-SHAP values: a base value plus one
signed number per feature, which together sum to the raw margin in log-odds.
They are exact, not sampled, and one pass over the trees, so a review screen can
ask for them per pair.

Only the two units and their own evidence rows are read, which keeps a pair
detail cheap on a PSC-sized run. The whole units frame is still handed to the
feature builder, because the organisation TF-IDF weights are fitted over every
unit — a feature's value must not depend on how many pairs were asked about at
once.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app.model import features as feature_lib
from app.model import corpus as corpus_lib
from app.model import references as reference_lib
from app.model import store
# The sentence that travels with every explanation, so it is read beside the
# evidence rather than only on the model panel (`MODEL.md`).
from app.model.train import KNOWN_LIMIT

logger = logging.getLogger(__name__)


def explain(pair: pd.DataFrame, units: pd.DataFrame, track: str, version: int,
            events: pd.DataFrame | None = None, profile=None,
            gamma_levels: dict | None = None,
            corpus: dict | None = None) -> dict | None:
    """The signed contribution of every feature to one pair's score.

    *pair* is a one-row frame with `unit_id_l` and `unit_id_r`, plus whatever
    generic columns `pairs.parquet` carries — the Splink match weight and the
    gammas are features too, so they have to come from the file rather than be
    worked out again.

    *gamma_levels* is the run's trained Splink model read back
    (``pairs_reader.comparison_levels``), which is what turns a bare
    ``gamma_surname = 2`` into "Jaro-Winkler ≥ 0.92".
    """
    stored = store.load_features(track, version)
    booster = store.load_booster(track, version)
    if booster is None or not stored or not len(pair):
        return None

    # The corpus statistics are the run's own, read back from the run folder.
    # Without them this screen would fit a TF-IDF over a corpus of two names and
    # print numbers the scoring run never produced (`app/model/corpus.py`).
    references = corpus_lib.attach(reference_lib.load(profile), corpus or {})
    built, _meta = feature_lib.build(pair, units, track, events=events,
                                     references=references, profile=profile)
    columns = [f["name"] for f in stored]
    X = built.reindex(columns=columns).astype("float64")

    raw = float(booster.predict(X.to_numpy())[0])
    calibration = store.load_calibration(track, version)
    score = float(store.apply_calibration(np.asarray([raw]), calibration)[0])
    contributions = booster.predict(X.to_numpy(), pred_contrib=True)[0]

    # How to put a value into words is presentation, not model semantics, so a
    # version trained before `render` existed borrows the profile's current
    # answer rather than falling back to bare numbers. `categories` is NOT
    # borrowed: the codes are what the model learnt on, and reading them off a
    # newer list would mislabel an older model.
    live = {f.name: f.render for f in
            feature_lib.metadata(pair.columns, track, profile)}
    by_name = {}
    for feature in stored:
        meta = dict(feature)
        if not meta.get("render"):
            meta["render"] = ("category" if meta.get("categories")
                              else live.get(meta["name"], "number"))
        by_name[meta["name"]] = meta
    items = []
    for name, value in zip(columns, contributions[:-1]):
        if not float(value):
            continue
        cell = X.iloc[0][name]
        meta = by_name.get(name, {})
        items.append({
            "name": name,
            "label": meta.get("label", name),
            "group": meta.get("group"),
            "value": None if pd.isna(cell) else float(cell),
            # The number on its own is not readable — 0.87 means one thing for a
            # name similarity and another for the log of a count, and a category
            # code means nothing at all. Every row therefore carries the value in
            # words, rendered from the metadata stored with THIS version, so an
            # older model is always read the way it was written.
            "value_label": feature_lib.render_value(meta, cell, gamma_levels),
            "contribution": round(float(value), 6),
        })
    items.sort(key=lambda row: -abs(row["contribution"]))

    summary = store.summary(track, version) or {}
    return {
        "track": track,
        "version": int(version),
        "graded": bool(summary.get("graded")),
        "raw": round(raw, 6),
        "score": round(score, 6),
        "base": round(float(contributions[-1]), 6),
        "contributions": items,
        "known_limit": KNOWN_LIMIT,
    }
