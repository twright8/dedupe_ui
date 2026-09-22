# backend/app/pipeline/dedupe/score_eval.py
"""Score the exact groups plus the accepted pairs against the existing labels.

``exact_eval.json`` asks what the match keys alone do to the decisions a
previous review made. This asks the same of the whole chain: the exact groups,
joined up by every pair the scorer accepted. It is what the owner tunes rules
against, so the arithmetic here has to be right.

Entities come from connected components over the units — the accepted pairs are
edges, and SciPy labels the components, so nothing walks a pair of records.
Precision and recall then reuse ``rules/keys_eval``, which counts from group
sizes with n·(n−1)/2, so the figures here and in ``exact_eval.json`` mean the
same thing and can be put side by side.
"""

from pathlib import Path

import numpy as np
import pandas as pd

from app import vocabulary
from app.pipeline.dedupe import label_overlay
from app.rules import keys, keys_eval, vetoes

HISTOGRAM_BINS = 50

# Named apart from `keys_eval.AGREEMENTS`, which answers a different
# question with the same word (docs/TERMINOLOGY_AUDIT.md B19). This one is
# about one accepted pair against the earlier grouping.
IMPORT_AGREEMENTS = vocabulary.IMPORT_AGREEMENTS

#: The only record columns this module reads. `keys_eval` wants the track and
#: the old entity id, and `_as_groups` wants the id. The other sixty columns of
#: a PSC record are nothing to do with scoring, and reading them was costing a
#: gigabyte for no purpose.
RECORD_COLUMNS = ("record_id", "track", keys_eval.LABEL_COLUMN)


def components(units: pd.DataFrame, accepted: pd.DataFrame) -> pd.Series:
    """A component id per unit: the units the accepted pairs join together."""
    ids = units["unit_id"].astype(str).to_numpy()
    n = len(ids)
    if n == 0:
        return pd.Series(dtype="int64")

    if len(accepted):
        position = pd.Series(np.arange(n), index=ids)
        left = accepted["unit_id_l"].astype(str).map(position)
        right = accepted["unit_id_r"].astype(str).map(position)
        keep = (left.notna() & right.notna()).to_numpy()
        rows = left[keep].to_numpy(dtype="int32")
        cols = right[keep].to_numpy(dtype="int32")
    else:
        rows = np.empty(0, dtype="int32")
        cols = np.empty(0, dtype="int32")
    return pd.Series(components_of(rows, cols, n), index=ids, name="component")


def components_of(rows: np.ndarray, cols: np.ndarray, n: int) -> np.ndarray:
    """Connected components over integer-coded edges. One label per unit.

    The codes are the point. Mapping fifteen million unit ids through a pandas
    index of strings costs a hash table of fifteen million Python strings before
    SciPy sees a single edge; two int32 arrays cost eight bytes a pair and
    nothing per unit.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    graph = coo_matrix(
        (np.ones(len(rows), dtype="int8"), (rows, cols)), shape=(n, n)
    )
    _count, labels = connected_components(graph, directed=False)
    return labels



def _as_groups(records: pd.DataFrame, members: pd.DataFrame,
               component: pd.Series) -> pd.DataFrame:
    """Records labelled by their component, shaped like a groups frame.

    ``keys_eval`` reads ``status == 'merged'``, so every record joins in; a
    component of one contributes no pairs to either score, which is exactly what
    an unmerged record should contribute.
    """
    frame = members[["record_id", "unit_id"]].copy()
    frame["record_id"] = frame["record_id"].astype(str)
    frame["group_id"] = "C" + frame["unit_id"].astype(str).map(component).astype(str)
    tracks = records.set_index(records["record_id"].astype(str))["track"] \
        if "track" in records.columns else None
    frame["track"] = frame["record_id"].map(tracks) if tracks is not None else None
    frame["status"] = keys.MERGED
    return frame[["record_id", "group_id", "track", "status"]]



def _vetoed(pairs: pd.DataFrame) -> np.ndarray:
    """Which pairs a veto stopped. All False on a run with no vetoes."""
    if not len(pairs) or "vetoed_by" not in pairs.columns:
        return np.zeros(len(pairs), dtype=bool)
    return pairs["vetoed_by"].notna().to_numpy()





# ---------------------------------------------------------------------------
# Reading the pairs a batch at a time
# ---------------------------------------------------------------------------

#: The pair columns this module reads. The gammas and the priority totals are
#: nothing to do with the evaluation, and at a hundred million pairs they are
#: most of the file.
PAIR_COLUMNS = (
    "unit_id_l", "unit_id_r", "track", "match_probability", "gbt_score",
    "score_bucket", "bucket", "decided_by", "import_disagrees", "vetoed_by",
    "veto_conflicts_import",
)

DEFAULT_BATCH_ROWS = 500_000


def pair_batches(pairs, batch_rows: int | None = None):
    """*pairs* as a sequence of bounded frames, whether it is one or a file.

    A frame is yielded whole — it is already in memory and the caller chose to
    put it there, which is what the tests and the small donations runs do. A
    path is read in batches of the columns above, so a run with a hundred
    million pairs costs one batch at a time and nothing more.
    """
    if isinstance(pairs, (str, Path)):
        import pyarrow.parquet as pq

        handle = pq.ParquetFile(str(pairs))
        available = set(handle.schema_arrow.names)
        columns = [c for c in PAIR_COLUMNS if c in available]
        rows = batch_rows or DEFAULT_BATCH_ROWS
        empty = True
        for batch in handle.iter_batches(batch_size=rows, columns=columns):
            frame = batch.to_pandas()
            if len(frame):
                empty = False
                yield frame
        if empty:
            yield pd.DataFrame(columns=columns)
    else:
        yield pairs


class _Edges:
    """Integer-coded edges of one accepted-pair set, gathered across batches.

    The codes are positions in the units array, so a component pass is two
    int32 arrays rather than a hash table of every unit id. ``None`` means the
    set does not exist at all — a run no model has scored has no model edges,
    which is a different statement from a model that accepted nothing.
    """

    def __init__(self, position: pd.Series, optional: bool = False):
        self._position = position
        self._left: list[np.ndarray] = []
        self._right: list[np.ndarray] = []
        self._seen = not optional
        self.rows = 0

    def add(self, frame: pd.DataFrame, mask=None) -> None:
        if mask is not None:
            frame = frame[mask]
        if not len(frame):
            return
        left = frame["unit_id_l"].astype(str).map(self._position)
        right = frame["unit_id_r"].astype(str).map(self._position)
        keep = (left.notna() & right.notna()).to_numpy()
        if not keep.any():
            return
        self._left.append(left[keep].to_numpy(dtype="int32"))
        self._right.append(right[keep].to_numpy(dtype="int32"))
        self.rows += int(keep.sum())

    def saw_values(self) -> None:
        self._seen = True

    def arrays(self):
        if not self._seen:
            return None
        if not self._left:
            return np.empty(0, dtype="int32"), np.empty(0, dtype="int32")
        return np.concatenate(self._left), np.concatenate(self._right)


def _figures_from_edges(records, members, units, edges: _Edges) -> tuple[dict, int] | None:
    """``keys_eval``'s figures for one set of accepted edges."""
    arrays = edges.arrays()
    if arrays is None:
        return None
    rows, cols = arrays
    ids = units["unit_id"].astype(str).to_numpy()
    labels = components_of(rows, cols, len(ids))
    component = pd.Series(labels, index=ids, name="component")
    entities = int(component.nunique()) if len(component) else 0
    return keys_eval.evaluate(records, _as_groups(records, members, component)), entities


def _agreement_of(batch: pd.DataFrame, id_lookup: pd.Series) -> pd.Series:
    """``agrees`` / ``disagrees`` / ``unknown`` per pair, from one id lookup."""
    left = batch["unit_id_l"].astype(str).map(id_lookup)
    right = batch["unit_id_r"].astype(str).map(id_lookup)
    both = left.notna() & right.notna()
    return pd.Series(
        np.where(both & (left == right), "agrees",
                 np.where(both, "disagrees", "unknown")),
        index=batch.index,
    )


def _add_at(edges: "_Edges", batch: pd.DataFrame, column: str, high: float,
            lines: dict | None = None, track_high: dict | None = None) -> None:
    """Add the pairs one score alone would accept, ignoring every overlay.

    *track_high* is ``{track: accept line}`` for the tracks that set one of
    their own; *lines* is the graded models' ``{track: (review, high)}``. Both
    are read per track, so a run whose person track accepts at 0.96 and whose
    organisation track accepts at 0.92 is measured at the line each one used.
    """
    if column not in batch.columns:
        return
    values = pd.to_numeric(batch[column], errors="coerce")
    if not values.notna().any():
        return
    edges.saw_values()
    limits = np.full(len(batch), float(high))
    if track_high or lines:
        tracks = batch["track"].to_numpy() if "track" in batch.columns \
            else np.full(len(batch), None)
        for track, line in (track_high or {}).items():
            limits[tracks == track] = float(line)
        for track, (_review, model_high) in (lines or {}).items():
            limits[tracks == track] = float(model_high)
    edges.add(batch, (values >= limits).to_numpy())


def _set_figures(records, members, units, edges: "_Edges") -> dict | None:
    result = _figures_from_edges(records, members, units, edges)
    if result is None:
        return None
    figures, entities = result
    return {
        "entities_after": entities,
        "pair_precision": figures["pair_precision"],
        "pair_recall": figures["pair_recall"],
        "accepted_pairs": edges.rows,
        "by_track": figures["by_track"],
    }


class _Tally:
    """The per-pair counts, gathered a batch at a time.

    Every number here is a sum over rows, so a batch contributes to it and is
    then thrown away. That is the whole reason this class exists: the figures
    used to be fifteen separate passes over one frame holding every pair in the
    run.
    """

    def __init__(self):
        self.pairs = 0
        self._buckets = {"bucket": {}, "score_bucket": {}}
        self._human_buckets: dict = {}
        self.decided_by_import = 0
        self.import_disagrees = 0
        self._vetoed = 0
        self._vetoed_from_accept = 0
        self._conflicts = 0
        self._by_veto: dict = {}
        self._review_pairs = 0
        self._review_score = 0
        self._review_agreement = {name: 0 for name in IMPORT_AGREEMENTS}
        self._histogram = {name: np.zeros(HISTOGRAM_BINS, dtype="int64")
                           for name in IMPORT_AGREEMENTS}

    @staticmethod
    def _count_into(store: dict, values: pd.Series) -> None:
        for name, count in values.value_counts().items():
            store[name] = store.get(name, 0) + int(count)

    def add(self, batch: pd.DataFrame, agreement: pd.Series,
            no_veto: np.ndarray, human: pd.DataFrame) -> None:
        if not len(batch):
            return
        self.pairs += len(batch)
        for column in ("bucket", "score_bucket"):
            if column in batch.columns:
                self._count_into(self._buckets[column], batch[column])
        if "bucket" in human.columns:
            self._count_into(self._human_buckets, human["bucket"])
        if "decided_by" in batch.columns:
            self.decided_by_import += int((batch["decided_by"] == "import").sum())
        if "import_disagrees" in batch.columns:
            self.import_disagrees += int(
                batch["import_disagrees"].fillna(False).sum())

        would_accept = no_veto == "accept"
        if "vetoed_by" in batch.columns:
            vetoed = batch["vetoed_by"].notna().to_numpy()
            self._vetoed += int(vetoed.sum())
            self._vetoed_from_accept += int((vetoed & would_accept).sum())
            if vetoed.any():
                hit = batch.loc[vetoed, "vetoed_by"]
                accepted_hit = would_accept[vetoed]
                for veto_id in pd.unique(hit):
                    rows = (hit == veto_id).to_numpy()
                    entry = self._by_veto.setdefault(
                        str(veto_id), {"pairs": 0, "from_accept": 0})
                    entry["pairs"] += int(rows.sum())
                    entry["from_accept"] += int((rows & accepted_hit).sum())
        if "veto_conflicts_import" in batch.columns:
            self._conflicts += int(
                batch["veto_conflicts_import"].fillna(False).sum())

        if "bucket" in batch.columns:
            self._review_pairs += int((batch["bucket"] == "review").sum())
        if "score_bucket" in batch.columns:
            in_review = (batch["score_bucket"] == "review").to_numpy()
            self._review_score += int(in_review.sum())
            for name in IMPORT_AGREEMENTS:
                self._review_agreement[name] += int(
                    (in_review & (agreement == name).to_numpy()).sum())

        edges = np.linspace(0.0, 1.0, HISTOGRAM_BINS + 1)
        values = pd.to_numeric(batch["match_probability"], errors="coerce")
        for name in IMPORT_AGREEMENTS:
            selected = values[(agreement == name).to_numpy()].dropna()
            counts, _ = np.histogram(selected.to_numpy(dtype="float64"), bins=edges)
            self._histogram[name] += counts.astype("int64")

    def buckets(self, column: str) -> dict:
        store = self._buckets[column]
        return {name: int(store.get(name, 0))
                for name in ("accept", "review", "reject")}

    def human_buckets(self) -> dict:
        return {name: int(self._human_buckets.get(name, 0))
                for name in ("accept", "review", "reject")}

    def veto_counts(self) -> dict:
        return {
            "vetoed": self._vetoed,
            "vetoed_from_accept": self._vetoed_from_accept,
            "conflicts_import": self._conflicts,
            "by_veto": self._by_veto,
        }

    def review_block(self) -> dict:
        block = {"pairs": self._review_pairs,
                 "score_bucket_pairs": self._review_score}
        for name in IMPORT_AGREEMENTS:
            block[f"import_{name}"] = self._review_agreement[name]
        return block

    def histogram(self) -> dict:
        edges = np.linspace(0.0, 1.0, HISTOGRAM_BINS + 1)
        result = {"bins": HISTOGRAM_BINS,
                  "edges": [round(float(e), 4) for e in edges]}
        for name in IMPORT_AGREEMENTS:
            result[name] = [int(c) for c in self._histogram[name]]
        return result


def evaluate(
    records: pd.DataFrame,
    groups: pd.DataFrame,
    units: pd.DataFrame,
    members: pd.DataFrame,
    pairs,
    thresholds: dict | None = None,
    applied: pd.DataFrame | None = None,
    model_lines: dict | None = None,
) -> dict:
    """What the exact groups plus the accepted pairs do to the existing labels.

    *applied* is the active human labels mapped onto this run's unit pairs
    (``label_overlay.outcomes``). It adds the ``with_human`` figure set and
    changes nothing else, so the sets beside it stay comparable.

    *model_lines* is ``{track: (review, high)}`` for the graded models. It adds
    two more sets, ``splink_only`` and ``model_only``, which are the same
    arithmetic run on each score on its own. Without them "did the model help"
    cannot be answered from the file: ``score_only`` follows whichever score is
    deciding, so it changes meaning the moment a model is applied.
    """
    exact_only = keys_eval.evaluate(records, groups)
    high = float((thresholds or {}).get("high") or 1.0)
    # The accept line of every track that set one of its own, so `splink_only`
    # is measured at the line each track really used (docs/LINKAGE.md).
    track_high = (thresholds or {}).get("high_by_track") or {}

    ids = units["unit_id"].astype(str).to_numpy()
    position = pd.Series(np.arange(len(ids)), index=ids)
    id_lookup = units.set_index(ids)["existing_entity_id"] \
        if "existing_entity_id" in units.columns \
        else pd.Series(dtype="object", index=ids)
    id_lookup = id_lookup[~id_lookup.index.duplicated(keep="first")]

    verdicts = label_overlay.decisions(applied) if applied is not None else None
    keyed = None
    n_true = n_false = 0
    if verdicts is not None and len(verdicts):
        is_true = verdicts["is_match"].astype(str).str.upper() == "TRUE"
        n_true, n_false = int(is_true.sum()), int((~is_true).sum())
        keyed = verdicts.set_index(["unit_id_l", "unit_id_r"])["is_match"]

    sets = {
        "combined": _Edges(position),
        "score_only": _Edges(position),
        "without_vetoes": _Edges(position),
        "without_vetoes_score": _Edges(position),
        "with_human": _Edges(position),
        "splink_only": _Edges(position, optional=True),
        "model_only": _Edges(position, optional=True),
    }
    totals = _Tally()
    per_track: dict[str, _Tally] = {}

    for batch in pair_batches(pairs):
        if not len(batch):
            continue
        agreement = _agreement_of(batch, id_lookup)
        stopped = _vetoed(batch)
        no_veto = vetoes.bucket_without_vetoes(batch)
        human = label_overlay.apply_to_pairs(batch, applied) \
            if applied is not None and len(applied) else batch

        # The import overlay accepts a pair because the two units already carry
        # the same old entity id, so those accepts cannot be evidence that the
        # scorer found anything. `score_only` leaves them out, and it is the
        # figure to tune blocking rules and comparisons against.
        sets["combined"].add(batch, (batch["bucket"] == "accept").to_numpy())
        sets["score_only"].add(
            batch, ((batch["score_bucket"] == "accept") & ~stopped).to_numpy())
        # The same run with no veto in it, so the cost of a veto in recall and
        # its gain in precision can be read side by side (RULESET.md, Vetoes).
        sets["without_vetoes"].add(batch, no_veto == "accept")
        sets["without_vetoes_score"].add(
            batch, (batch["score_bucket"] == "accept").to_numpy())
        sets["with_human"].add(human, (human["bucket"] == "accept").to_numpy())
        _add_at(sets["splink_only"], batch, "match_probability", high,
                track_high=track_high)
        _add_at(sets["model_only"], batch, "gbt_score", high, model_lines,
                track_high=track_high)

        totals.add(batch, agreement, no_veto, human)
        for track in pd.unique(batch["track"].dropna()):
            mask = (batch["track"] == track).to_numpy()
            tally = per_track.setdefault(str(track), _Tally())
            tally.add(batch[mask], agreement[mask], no_veto[mask], human[mask])

    # A TRUE label joins its two units even when the scorer never offered the
    # pair, so those edges are added after the scan rather than found in it.
    if keyed is not None:
        extra = verdicts[(verdicts["is_match"].astype(str).str.upper() == "TRUE").to_numpy()]
        sets["with_human"].add(extra.drop_duplicates(subset=["unit_id_l", "unit_id_r"]))

    combined, entities_after = _figures_from_edges(records, members, units, sets["combined"])
    score_only, score_only_entities = _figures_from_edges(
        records, members, units, sets["score_only"])
    nv_figures, nv_entities = _figures_from_edges(
        records, members, units, sets["without_vetoes"])
    nvs_figures, nvs_entities = _figures_from_edges(
        records, members, units, sets["without_vetoes_score"])
    human_figures, human_entities = _figures_from_edges(
        records, members, units, sets["with_human"])

    without_vetoes = {
        "entities_after": nv_entities,
        "pair_precision": nv_figures["pair_precision"],
        "pair_recall": nv_figures["pair_recall"],
        "accepted_pairs": sets["without_vetoes"].rows,
        "by_track": nv_figures["by_track"],
        "score_only": {
            "entities_after": nvs_entities,
            "pair_precision": nvs_figures["pair_precision"],
            "pair_recall": nvs_figures["pair_recall"],
            "accepted_pairs": sets["without_vetoes_score"].rows,
            "by_track": nvs_figures["by_track"],
        },
    }
    with_human = {
        "entities_after": human_entities,
        "pair_precision": human_figures["pair_precision"],
        "pair_recall": human_figures["pair_recall"],
        "labels_applied": n_true + n_false,
        "labels_true": n_true,
        "labels_false": n_false,
        "by_bucket": totals.human_buckets(),
        "by_track": human_figures["by_track"],
    }

    by_track = {}
    for track in sorted(per_track):
        tally = per_track[track]
        by_track[track] = {
            "pairs": tally.pairs,
            "by_bucket": tally.buckets("bucket"),
            "by_score_bucket": tally.buckets("score_bucket"),
            "decided_by_import": tally.decided_by_import,
            "import_disagrees": tally.import_disagrees,
            "review": tally.review_block(),
            "histogram": tally.histogram(),
            "vetoes": tally.veto_counts(),
            **{k: v for k, v in (combined["by_track"].get(track) or {}).items()},
            "score_only": score_only["by_track"].get(track),
            "without_vetoes": {
                **(without_vetoes["by_track"].get(track) or {}),
                "score_only": without_vetoes["score_only"]["by_track"].get(track),
            },
            "with_human": with_human["by_track"].get(track),
            "exact_only": exact_only["by_track"].get(track),
            "units": int((units["track"] == track).sum())
            if "track" in units.columns else None,
        }

    return {
        "thresholds": thresholds or {},
        "units_total": int(len(units)),
        "pairs_total": totals.pairs,
        "by_bucket": totals.buckets("bucket"),
        "by_score_bucket": totals.buckets("score_bucket"),
        "decided_by_import": totals.decided_by_import,
        "import_disagrees": totals.import_disagrees,
        "vetoes": totals.veto_counts(),
        "entities_after": entities_after,
        "pair_precision": combined["pair_precision"],
        "pair_recall": combined["pair_recall"],
        "labelled_pairs": combined["labelled_pairs"],
        "labelled_pairs_agreeing": combined["labelled_pairs_agreeing"],
        "manual_pairs": combined["manual_pairs"],
        "manual_pairs_found": combined["manual_pairs_found"],
        "conflicts": combined["conflicts"],
        "review": totals.review_block(),
        "histogram": totals.histogram(),
        "by_track": by_track,
        "score_only": {
            "entities_after": score_only_entities,
            "pair_precision": score_only["pair_precision"],
            "pair_recall": score_only["pair_recall"],
            "by_track": score_only["by_track"],
        },
        "without_vetoes": without_vetoes,
        "with_human": with_human,
        "exact_only": {
            "entities_after": int(len(units)),
            "pair_precision": exact_only["pair_precision"],
            "pair_recall": exact_only["pair_recall"],
            "by_track": exact_only["by_track"],
        },
        # The two scores, each on its own, so the owner can read "what does the
        # model change" straight off the file. `model_only` is null on a run no
        # model has scored.
        "splink_only": _set_figures(records, members, units, sets["splink_only"]),
        "model_only": _set_figures(records, members, units, sets["model_only"]),
    }
