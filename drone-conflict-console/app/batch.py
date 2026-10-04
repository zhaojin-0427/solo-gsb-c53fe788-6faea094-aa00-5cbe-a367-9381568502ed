"""Execution batches and deviation early-warning evaluation.

A *batch* freezes one saved feasible plan: the input trajectories (snapshot),
the safety thresholds (R, H) and the planned take-off offsets.  Batch state
then only changes through per-sortie actual-take-off reports carrying a
strictly increasing revision number.

Given a non-negative current time ``t`` the warning evaluation classifies
every sortie:

* **reported** — an actual take-off offset has been reported; it flies at the
  reported time;
* **planned** — not reported yet, but its planned take-off is not earlier
  than ``t``: it is still predictable and flies at the planned time;
* **overdue** — not reported and its planned take-off is already in the past
  (strictly earlier than ``t``): excluded from prediction and the result is
  flagged incomplete.

Conflicts reuse the continuous-time detector in :mod:`app.geometry` on the
effective (reported / planned) schedule and are reported for instants
``>= t``.  The evaluation is a pure function of the frozen batch and the
stored reports, so reloading a batch and re-evaluating always gives the same
result.

A *replay* compares the warning evaluated on the complete report states
immediately before and after one accepted report node (both at the same
``t``): per sortie pair it reports the newly appeared and the resolved
conflict time intervals — intersecting intervals contribute only their
changed parts — plus the first-conflict times and the overdue-set changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .geometry import EPS, conflicts_between_sorties

REPORTED = "reported"
PLANNED = "planned"
OVERDUE = "overdue"


@dataclass(frozen=True)
class Report:
    actual_offset: int
    revision: int


class EarlyTimeReject(Exception):
    """Raised when ``t`` is earlier than a reported actual take-off."""

    def __init__(self, t: int, violations: list[dict]):
        self.t = t
        self.violations = violations
        super().__init__(f"t={t} is earlier than a reported actual take-off")


def _conflict_dto(sorties_by_id, takeoff_a, takeoff_b, rec, t):
    """Project a conflict record onto ``[t, ...]`` and refresh its witness."""
    start = max(float(t), rec.time_start)
    end = rec.time_end
    length = end - start
    witness = start if length <= 1e-9 else 0.5 * (start + end)
    pa = sorties_by_id[rec.sortie_a].position(witness - takeoff_a)
    pb = sorties_by_id[rec.sortie_b].position(witness - takeoff_b)
    dx, dy = pa[0] - pb[0], pa[1] - pb[1]
    return {
        "sortie_a": rec.sortie_a,
        "sortie_b": rec.sortie_b,
        "time_start": round(start, 6),
        "time_end": round(end, 6),
        "witness_time": round(witness, 6),
        "a_pos": [round(v, 6) for v in pa],
        "b_pos": [round(v, 6) for v in pb],
        "horizontal_distance": round((dx * dx + dy * dy) ** 0.5, 6),
        "vertical_distance": round(abs(pa[2] - pb[2]), 6),
    }


def evaluate_warning(sorties, planned_offsets: dict[str, int],
                     reports: dict[str, Report], safety_radius: float,
                     safety_height: float, t: int) -> dict:
    """Compute the deviation warning for a frozen batch at current time ``t``.

    Raises :class:`EarlyTimeReject` when ``t`` precedes a reported take-off.
    """
    # A reported sortie has actually taken off at its offset; an evaluation
    # time before that instant is inconsistent and must be rejected.
    violations = [
        {"sortie_id": sid, "actual_takeoff": rep.actual_offset}
        for sid, rep in sorted(reports.items())
        if t < rep.actual_offset
    ]
    if violations:
        raise EarlyTimeReject(t, violations)

    by_id = {s.id: s for s in sorties}
    statuses: dict[str, str] = {}
    effective: dict[str, int] = {}
    for s in sorties:
        rep = reports.get(s.id)
        if rep is not None:
            statuses[s.id] = REPORTED
            effective[s.id] = rep.actual_offset
        elif planned_offsets[s.id] >= t:
            statuses[s.id] = PLANNED
            effective[s.id] = planned_offsets[s.id]
        else:
            statuses[s.id] = OVERDUE

    # Continuous-time conflicts on the effective schedule, restricted to the
    # participating (non-overdue) sorties and to instants at or after t.
    conflicts: list[dict] = []
    for i, sa in enumerate(sorties):
        if statuses[sa.id] == OVERDUE:
            continue
        for sb in sorties[i + 1:]:
            if statuses[sb.id] == OVERDUE:
                continue
            recs = conflicts_between_sorties(
                sa, effective[sa.id], sb, effective[sb.id],
                safety_radius, safety_height,
            )
            for rec in recs:
                if rec.time_end < t - EPS:
                    continue
                conflicts.append(
                    _conflict_dto(by_id, effective[sa.id], effective[sb.id], rec, t)
                )
    conflicts.sort(key=lambda r: (r["time_start"], r["sortie_a"], r["sortie_b"]))
    first_conflict_time = conflicts[0]["time_start"] if conflicts else None

    sortie_rows = []
    deviations = []
    for s in sorties:
        sid = s.id
        status = statuses[sid]
        rep = reports.get(sid)
        planned = planned_offsets[sid]
        row = {
            "id": sid,
            "status": status,
            "planned_offset": planned,
            "actual_offset": rep.actual_offset if rep is not None else None,
            "revision": rep.revision if rep is not None else None,
            "takeoff_offset": effective.get(sid),
            "deviation_seconds": None,
        }
        if rep is not None:
            deviation = rep.actual_offset - planned
            row["deviation_seconds"] = deviation
            deviations.append({
                "sortie_id": sid,
                "planned_offset": planned,
                "actual_offset": rep.actual_offset,
                "revision": rep.revision,
                "deviation_seconds": deviation,
            })
        sortie_rows.append(row)

    overdue_ids = [row["id"] for row in sortie_rows if row["status"] == OVERDUE]
    return {
        "t": t,
        "complete": not overdue_ids,
        "incomplete": bool(overdue_ids),
        "overdue_sorties": overdue_ids,
        "sorties": sortie_rows,
        "conflicts": conflicts,
        "first_conflict_time": first_conflict_time,
        "reported_deviations": deviations,
    }


# ---------------------------------------------------------------------------
# Warning-change replay: difference between two warning evaluations
# ---------------------------------------------------------------------------

_INTERVAL_EPS = 1e-9  # endpoints of the same unchanged conflict compare equal


def _pair_intervals(warning: dict) -> dict[tuple[str, str], list[tuple[float, float]]]:
    """Conflict intervals of a warning, grouped by sortie pair and sorted."""
    out: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for c in warning["conflicts"]:
        key = (c["sortie_a"], c["sortie_b"])
        out.setdefault(key, []).append((c["time_start"], c["time_end"]))
    for ivs in out.values():
        ivs.sort()
    return out


def _subtract_intervals(mine: list[tuple[float, float]],
                        others: list[tuple[float, float]]
                        ) -> list[tuple[float, float]]:
    """Parts of ``mine`` not covered by ``others``.

    Intervals that merely touch at an endpoint do not subtract from each
    other; overlaps with positive length remove exactly the shared part, so
    two intersecting intervals only contribute their changed portion.
    """
    remaining: list[tuple[float, float]] = []
    for s, e in mine:
        pieces = [(s, e)]
        for os, oe in others:
            nxt = []
            for ps, pe in pieces:
                if oe <= ps + _INTERVAL_EPS or os >= pe - _INTERVAL_EPS:
                    nxt.append((ps, pe))  # disjoint (or endpoint-touching)
                    continue
                if os > ps + _INTERVAL_EPS:
                    nxt.append((ps, min(os, pe)))
                if oe < pe - _INTERVAL_EPS:
                    nxt.append((max(oe, ps), pe))
            pieces = nxt
        remaining.extend(pieces)
    return remaining


def diff_warnings(before: dict, after: dict) -> dict:
    """Diff of two warning evaluations made at the same ``t``.

    ``pairs`` lists, per sortie pair, the conflict time intervals that were
    added resp. resolved between the two evaluations; ``first_conflict_time``
    and the overdue set are reported for both sides with their changes.
    """
    before_pairs = _pair_intervals(before)
    after_pairs = _pair_intervals(after)
    pairs = []
    for key in sorted(set(before_pairs) | set(after_pairs)):
        b_ivs = before_pairs.get(key, [])
        a_ivs = after_pairs.get(key, [])
        added = _subtract_intervals(a_ivs, b_ivs)
        resolved = _subtract_intervals(b_ivs, a_ivs)
        if added or resolved:
            pairs.append({
                "sortie_a": key[0],
                "sortie_b": key[1],
                "added": [[round(s, 6), round(e, 6)] for s, e in added],
                "resolved": [[round(s, 6), round(e, 6)] for s, e in resolved],
            })
    overdue_before = list(before["overdue_sorties"])
    overdue_after = list(after["overdue_sorties"])
    set_before, set_after = set(overdue_before), set(overdue_after)
    return {
        "pairs": pairs,
        "first_conflict_time_before": before["first_conflict_time"],
        "first_conflict_time_after": after["first_conflict_time"],
        "complete_before": before["complete"],
        "complete_after": after["complete"],
        "overdue_before": overdue_before,
        "overdue_after": overdue_after,
        "overdue_added": sorted(set_after - set_before),
        "overdue_resolved": sorted(set_before - set_after),
    }
