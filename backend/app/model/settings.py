# backend/app/model/settings.py
"""The model's numbers, read out of `linkage_settings` under a `model` key.

They live beside the Splink settings because they are settings of the same kind:
a user edits them on the Thresholds & Splink tab, they are versioned with the
ruleset, and a run records the version it used. Every one has a default, so an
older config version with no `model` key trains exactly as this file says.

Each default is a judgement, and the reason is written beside it. A number
without a reason is a number nobody can argue with, which is the wrong kind of
settled.
"""

from __future__ import annotations

DEFAULTS: dict = {
    # --- what counts as a graded model -------------------------------------
    # `MODEL.md`: under this many human labels the model is a cold start —
    # trained and calibrated on the imported labels, never allowed to decide a
    # pair. 50 is `MODEL.md`'s figure. `roe_ui`'s own experiment
    # (docs/experiments/gbt-decision-2026-06-04.md) found the GBT only clearly
    # beats Splink at 300–400 labels, so 50 is the floor for "worth grading",
    # not the point at which the model is good.
    "min_human_labels": 50,
    # A frozen test set smaller than this cannot support a threshold: a Wilson
    # lower bound on 10 rows is so far below the estimate that no line ever
    # reaches the target precision anyway.
    "min_test_labels": 20,
    # The auto-accept line is the lowest score whose precision lower bound
    # reaches this. `MODEL.md` and `LINKAGE.md` both name 0.99.
    "target_precision": 0.99,

    # --- training row weights ----------------------------------------------
    # A human label is a decision made about this pair, by someone who looked at
    # it. Nothing else is worth as much, so it is the unit.
    "human_weight": 1.0,
    # A group decision (cluster_merge / cluster_split) is a human label about a
    # whole group; `MODEL.md` says to treat it as one.
    "decision_weight": 1.0,
    # An imported agreement is a real earlier merge, but made almost entirely on
    # the name (the known limit), and there are thousands of them against a
    # handful of human labels. A quarter means four imported agreements carry
    # the weight of one human TRUE: enough to shape the model at cold start,
    # not enough to own it once reviewers start work.
    "import_agree_weight": 0.25,
    # Two donors sitting in different earlier groups is the weakest signal in
    # the set (D11 calls it a weak "not the same"): the two groups may simply
    # never have been compared. A tenth, so it tilts the boundary and no more.
    "import_disagree_weight": 0.10,

    # --- how many imported positives to keep --------------------------------
    # An absolute row cap, so a PSC-sized run trains in seconds rather than
    # minutes. Past a couple of hundred thousand rows the extra ones are near
    # copies of the same name-agreement pattern and buy nothing.
    "import_agree_max_rows": 200000,
    # The second cap, which only bites once humans have labelled anything: at
    # most this many imported positives per human label. 300 human labels then
    # allow 15,000 imported ones — the prior survives, the human labels move the
    # boundary.
    "import_agree_per_human_label": 50,

    # --- the fit ------------------------------------------------------------
    "n_folds": 4,
    "seed": 42,
    "n_estimators": 300,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 20,
    "reg_lambda": 0.5,
    # Mean |SHAP| over this many rows at most. Tree-SHAP is exact but costs one
    # pass per tree per row; 20,000 rows pins the mean to three decimal places.
    "shap_max_rows": 20000,
    # The ablation retrains the model once per feature group per fold. Three
    # folds keeps six groups to eighteen fits, which is seconds, and the
    # ablation is a ranking of groups rather than a number anyone quotes.
    "ablation_folds": 3,
}

# Every setting that must be a whole number of at least one.
_POSITIVE_INTS = ("min_human_labels", "min_test_labels", "import_agree_max_rows",
                  "import_agree_per_human_label", "n_folds", "n_estimators",
                  "num_leaves", "min_child_samples", "shap_max_rows",
                  "ablation_folds")
_ZERO_TO_ONE = ("target_precision", "human_weight", "decision_weight",
                "import_agree_weight", "import_disagree_weight", "learning_rate")


def get(linkage_settings: dict | None) -> dict:
    """The model settings, defaults filled in. Unknown keys are ignored."""
    given = {}
    if isinstance(linkage_settings, dict):
        raw = linkage_settings.get("model")
        if isinstance(raw, dict):
            given = raw
    out = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        if key not in given or given[key] is None:
            continue
        value = given[key]
        if isinstance(value, bool):
            continue
        if isinstance(default, int) and not isinstance(default, bool):
            if isinstance(value, int):
                out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = float(value)
    return out


def validate(linkage_settings) -> list[dict]:
    """Every problem with the `model` block, as ``[{path, message}]``.

    Empty means good, which is also what an absent block gives.
    """
    errors: list[dict] = []
    if not isinstance(linkage_settings, dict):
        return errors
    given = linkage_settings.get("model")
    if given is None:
        return errors
    if not isinstance(given, dict):
        return [{"path": "linkage_settings.model", "message": "model must be an object"}]

    for key, value in given.items():
        path = f"linkage_settings.model.{key}"
        if key not in DEFAULTS:
            errors.append({"path": path, "message": f"Unknown model setting '{key}'"})
            continue
        if value is None:
            continue
        if isinstance(value, bool):
            errors.append({"path": path, "message": f"{key} must be a number"})
            continue
        if key in _POSITIVE_INTS:
            if not isinstance(value, int) or value < 1:
                errors.append({"path": path,
                               "message": f"{key} must be a whole number of 1 or more"})
        elif key in _ZERO_TO_ONE:
            if not isinstance(value, (int, float)) or not 0 < float(value) <= 1:
                errors.append({"path": path,
                               "message": f"{key} must be a number above 0 and at most 1"})
        elif not isinstance(value, (int, float)):
            errors.append({"path": path, "message": f"{key} must be a number"})
    return errors


def lgbm_params(settings: dict, monotone: list[int], seed: int | None = None) -> dict:
    """LightGBM's keyword arguments, so the trainer and the ablation agree."""
    return dict(
        objective="binary",
        n_estimators=int(settings["n_estimators"]),
        learning_rate=float(settings["learning_rate"]),
        num_leaves=int(settings["num_leaves"]),
        min_child_samples=int(settings["min_child_samples"]),
        reg_lambda=float(settings["reg_lambda"]),
        random_state=int(seed if seed is not None else settings["seed"]),
        monotone_constraints=list(monotone),
        deterministic=True,
        force_row_wise=True,
        num_threads=1,
        verbosity=-1,
    )
