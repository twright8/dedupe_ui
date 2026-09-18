# backend/app/model/__init__.py
"""The GBT that decides pairs (`docs/MODEL.md`, `docs/MODEL_API.md`).

One model per track. Splink finds the candidates and supplies a prior score;
this package turns a scored pair into a feature row, trains a gradient boosted
tree on labels, calibrates it, grades it on a frozen test set, and keeps the
versions.

| Module | Holds |
|---|---|
| `features` | the feature contract, the generic features, and the assembly |
| `references` | outside tables a profile declares, such as UK name frequencies |
| `settings` | the model's numbers, read out of `linkage_settings` |
| `dataset` | training rows from labels, their weights, and the folds |
| `train` | the fit, the calibration, the grading, the importance, the report |
| `store` | versions on disk, and which one is active |
| `jobs` | training in the background, with progress |
| `scoring` | scoring a run's pairs with an active model, and explaining one pair |
"""
