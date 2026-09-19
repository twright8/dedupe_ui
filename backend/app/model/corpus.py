# backend/app/model/corpus.py
"""Corpus statistics: fitted once per run, written down, and reused everywhere.

A feature like "how rare are the words in this name" is not a fact about a
pair. It is a fact about the pair **and the collection the words were counted
in**, so it is only a feature at all if every reader counts over the same
collection. Two things go wrong when they do not.

*The value depends on the batch.* The PSC organisation builder fitted its
TF-IDF over the units named in the pairs it was handed, so scoring a million
pairs and scoring one of them on its own gave that pair two different numbers.
Batching the scorer — which is what makes a sixteen-million-record run possible
— would have changed every value in the file.

*A pair cannot be explained.* The per-pair explanation screen builds the
features for one pair and prints them beside the model's bars. If the builder
fits over what it was handed, the explanation is arithmetic about a corpus of
two names, and it does not reproduce the number the run actually scored on.
That is not a small discrepancy: it is the screen that is meant to let a
reviewer check the machine.

So a builder that needs corpus statistics **declares** them, as a
``CorpusSpec``. The statistics are fitted ONCE, over every unit in the declared
scope, at scoring time. They are written into the run folder. Batched scoring,
applying a model to a finished run, and explaining one pair all read that file
back, and all three get the same answer.

Scope
-----
``scope="track"`` fits over the units of the track being scored, and
``scope="run"`` over every unit in the run. The difference matters and neither
is right for everyone:

* PSC uses ``track``. Its ``name_core`` is an organisation column; its 449,397
  person units carry none. Counting them in would add 449,397 empty documents
  to the collection, which raises every IDF by about the same amount and
  flattens the very distinction the feature exists to draw.
* donations uses ``run``, because that is what it has always done — its builder
  fits over the whole units frame, person rows included — and the values in the
  trained donations model must not move. Its person units carry no
  ``name_core`` either, so the same argument applies to it; it is recorded in
  ``docs/MODEL.md`` as a thing to change deliberately, with a retrain, rather
  than by accident here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

#: Where a run keeps what it fitted.
CORPUS_DIRNAME = "corpus"

SCOPES = ("track", "run")


@dataclass(frozen=True)
class CorpusSpec:
    """One set of corpus statistics a feature builder needs.

    *name* is the file it is stored under and the key the builder looks it up
    by. *column* is the unit column the documents come from. The vectoriser
    settings are stored with the fit, so a later change to them cannot silently
    re-interpret an older run's numbers.
    """

    name: str
    column: str
    scope: str = "track"
    max_features: int = 20000
    analyzer: str = "word"
    token_pattern: str = r"[A-Za-z0-9]+"
    lowercase: bool = True
    norm: str = "l2"

    def settings(self) -> dict:
        return {
            "column": self.column,
            "scope": self.scope,
            "max_features": self.max_features,
            "analyzer": self.analyzer,
            "token_pattern": self.token_pattern,
            "lowercase": self.lowercase,
            "norm": self.norm,
        }

    def filename(self, track: str) -> str:
        key = track if self.scope == "track" else "run"
        return f"{key}_{self.name}.json"


@dataclass
class FittedCorpus:
    """A vocabulary and an IDF vector, ready to transform text.

    ``transform`` never fits. That is the whole point: the numbers a batch gets
    are the numbers the run fitted, whatever is in the batch.
    """

    spec: CorpusSpec
    vocabulary: dict
    idf: list
    documents: int = 0
    _vectoriser: object = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict:
        return {
            "name": self.spec.name,
            "settings": self.spec.settings(),
            "documents": int(self.documents),
            "vocabulary": self.vocabulary,
            "idf": [float(v) for v in self.idf],
        }

    def vectoriser(self):
        if self._vectoriser is None:
            from sklearn.feature_extraction.text import TfidfVectorizer

            model = TfidfVectorizer(
                analyzer=self.spec.analyzer,
                token_pattern=self.spec.token_pattern,
                lowercase=self.spec.lowercase,
                norm=self.spec.norm,
                vocabulary=self.vocabulary,
            )
            model._validate_vocabulary()
            model.idf_ = np.asarray(self.idf, dtype="float64")
            self._vectoriser = model
        return self._vectoriser

    def transform(self, texts):
        """The TF-IDF matrix of *texts* under the fitted statistics."""
        return self.vectoriser().transform(list(texts))


def fit(spec: CorpusSpec, texts) -> FittedCorpus:
    """Fit one spec over *texts*, exactly as the old in-line builders did."""
    from sklearn.feature_extraction.text import TfidfVectorizer

    values = [("" if value is None or (isinstance(value, float) and np.isnan(value))
               else str(value)) for value in texts]
    model = TfidfVectorizer(
        analyzer=spec.analyzer, token_pattern=spec.token_pattern,
        lowercase=spec.lowercase, norm=spec.norm, max_features=spec.max_features,
    )
    model.fit(values)
    return FittedCorpus(
        spec=spec,
        vocabulary={term: int(index) for term, index in model.vocabulary_.items()},
        idf=[float(v) for v in model.idf_],
        documents=len(values),
        _vectoriser=None,
    )


def texts_for(spec: CorpusSpec, units: pd.DataFrame, track: str) -> pd.Series:
    """The documents one spec is fitted over, in unit order.

    The order is the units frame's own order, so the fit is reproducible from
    ``units.parquet`` alone and does not depend on which pairs exist.
    """
    if spec.column not in units.columns:
        return pd.Series(dtype="object")
    frame = units
    if spec.scope == "track" and "track" in units.columns:
        frame = units[units["track"] == track]
    return frame[spec.column]


def path_for(run_dir, spec: CorpusSpec, track: str) -> Path:
    return Path(run_dir) / CORPUS_DIRNAME / spec.filename(track)


def read(run_dir, spec: CorpusSpec, track: str) -> FittedCorpus | None:
    """A fit written down earlier, or None. Settings that moved mean None.

    A run that was scored with one vectoriser setting and is now being explained
    with another has to be refitted, not read: the stored numbers would be
    answers to a different question.
    """
    path = path_for(run_dir, spec, track)
    if not path.is_file():
        return None
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if stored.get("settings") != spec.settings():
        return None
    return FittedCorpus(
        spec=spec,
        vocabulary={str(k): int(v) for k, v in (stored.get("vocabulary") or {}).items()},
        idf=list(stored.get("idf") or []),
        documents=int(stored.get("documents") or 0),
    )


def write(run_dir, fitted: FittedCorpus, track: str) -> Path:
    path = path_for(run_dir, fitted.spec, track)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fitted.as_dict()), encoding="utf-8")
    return path


def specs_for(profile, track: str) -> list[CorpusSpec]:
    """What this profile's builder declares it needs for *track*.

    The declaration belongs to the profile. It is looked for on the profile
    first, and failing that on the profile's own feature module — both shipped
    profiles put their builder in ``<profile module>_features``, and the
    declaration lives beside the builder that reads it, which is the only place
    it can be kept true.
    """
    declared = getattr(profile, "corpus_specs", None)
    if declared is None:
        declared = _module_specs(profile)
    if declared is None:
        return []
    try:
        return list(declared(track) or [])
    except TypeError:
        return list(declared or [])


def _module_specs(profile):
    import importlib

    module_name = getattr(type(profile), "__module__", "")
    if not module_name:
        return None
    try:
        module = importlib.import_module(f"{module_name}_features")
    except ImportError:
        return None
    return getattr(module, "corpus_specs", None)


def for_run(run_dir, units, track: str, profile=None) -> dict:
    """``{name: FittedCorpus}`` for *track*, fitting and storing what is missing.

    *units* is the whole units frame, or a path to ``units.parquet``. Only the
    declared columns are read off a path, so asking for corpus statistics does
    not drag sixty columns of sixteen million units into memory.
    """
    if profile is None:
        from app.profiles import get_profile

        profile = get_profile()
    specs = specs_for(profile, track)
    if not specs:
        return {}

    out: dict = {}
    missing = []
    for spec in specs:
        found = read(run_dir, spec, track) if run_dir is not None else None
        if found is None:
            missing.append(spec)
        else:
            out[spec.name] = found
    if not missing:
        return out

    frame = _units_frame(units, missing)
    for spec in missing:
        fitted = fit(spec, texts_for(spec, frame, track))
        if run_dir is not None:
            write(run_dir, fitted, track)
        out[spec.name] = fitted
    return out


def _units_frame(units, specs) -> pd.DataFrame:
    if isinstance(units, pd.DataFrame):
        return units
    import pyarrow.parquet as pq

    path = Path(units)
    available = set(pq.ParquetFile(path).schema_arrow.names)
    wanted = ["unit_id", "track"] + [s.column for s in specs]
    keep = [c for c in dict.fromkeys(wanted) if c in available]
    return pd.read_parquet(path, columns=keep)


#: How a fitted corpus travels to a feature builder. It rides in the references
#: dict because that is already the one channel every builder is handed, and
#: adding a parameter would change a signature three profiles implement.
REFERENCE_KEY = "__corpus__"


def attach(references: dict | None, fitted: dict) -> dict:
    """*references* with the fitted corpus on it, without mutating the caller's."""
    out = dict(references or {})
    out[REFERENCE_KEY] = fitted
    return out


def from_references(references: dict | None, name: str) -> FittedCorpus | None:
    return ((references or {}).get(REFERENCE_KEY) or {}).get(name)
