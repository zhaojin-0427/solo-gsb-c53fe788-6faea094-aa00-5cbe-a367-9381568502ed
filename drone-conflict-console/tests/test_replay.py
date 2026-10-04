"""Warning-change replay (预警变更复盘) validation.

Covers:

* replay nodes are recorded atomically with accepted report writes — one
  node per accepted write, in commit order, with complete before/after
  report-state snapshots; idempotent retries and rejected revisions create
  no node;
* batches that existed before replay nodes are not back-filled: the first
  post-upgrade accepted report uses the latest known state as its baseline;
* concurrent submissions from two OS processes sharing one SQLite file
  produce a node chain in actual commit order;
* the warning diff: per-pair added/resolved conflict intervals (intersecting
  intervals only contribute their changed parts), first-conflict times and
  overdue changes, both sides evaluated at the same ``t``;
* the HTTP endpoint: unknown batch/node -> 404, negative ``t`` -> 400, a
  ``t`` earlier than a reported take-off on either side rejects the whole
  replay, and the existing report/warning responses stay compatible;
* reloading a batch and replaying again gives identical results.

Run:  python tests/test_replay.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.batch import diff_warnings, evaluate_warning, Report  # noqa: E402
from app.model import Waypoint, make_sortie  # noqa: E402
from app.storage import Store  # noqa: E402

FAILS = 0


def check(cond, msg):
    global FAILS
    if not cond:
        FAILS += 1
        print("FAIL:", msg)


def two_crossing_sorties():
    """A eastbound, B northbound, crossing at (100, 0), same altitude."""
    a = make_sortie("A", 1, 20, [
        Waypoint(0, 0, 20, 0), Waypoint(100, 0, 20, 10), Waypoint(200, 0, 20, 20)])
    b = make_sortie("B", 1, 20, [
        Waypoint(100, -100, 20, 0), Waypoint(100, 100, 20, 20)])
    return [a, b]


def _make_batch(store, sortie_ids=("A", "B"), offsets=None):
    sid = store.create_scenario({"sorties": []}, 30, 10)
    offsets = offsets or {s: 0 for s in sortie_ids}
    bid = store.create_batch(sid, "b", {"sorties": [{"id": s} for s in sortie_ids]},
                             30, 10, {"offsets": offsets})
    return bid


def _check_chain(store, bid, expect_events, msg_prefix):
    """The stored node chain must be contiguous and self-consistent, and its
    final after-state must equal the authoritative batch_reports rows."""
    events = [store.get_report_event(bid, e["seq"])
              for e in store.get_report_events(bid)]
    check(len(events) == expect_events,
          f"{msg_prefix}: expected {expect_events} nodes, got {len(events)}")
    check([e["seq"] for e in events] == list(range(1, expect_events + 1)),
          f"{msg_prefix}: seqs contiguous from 1, got {[e['seq'] for e in events]}")
    for prev, nxt in zip(events, events[1:]):
        check(prev["after"] == nxt["before"],
              f"{msg_prefix}: node {nxt['seq']} before-state == node "
              f"{prev['seq']} after-state")
    current = sorted(
        ({"sortie_id": r["sortie_id"], "actual_offset": r["actual_offset"],
          "revision": r["revision"]} for r in store.get_batch(bid)["reports"]),
        key=lambda r: r["sortie_id"])
    if events:
        check(events[-1]["after"] == current,
              f"{msg_prefix}: last node after-state == stored reports")
    return events


def test_node_creation_rules():
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        bid = _make_batch(store)

        outcome, _ = store.submit_report(bid, "A", 5, 2)
        check(outcome == "invalid_first_revision", "first revision must be 1")
        check(store.get_report_events(bid) == [], "rejected first report: no node")

        outcome, _ = store.submit_report(bid, "A", 5, 1)
        check(outcome == "inserted", "first report accepted")
        events = store.get_report_events(bid)
        check(len(events) == 1 and events[0]["seq"] == 1
              and events[0]["sortie_id"] == "A" and events[0]["revision"] == 1
              and events[0]["actual_offset"] == 5,
              f"one node for the accepted write: {events}")

        outcome, _ = store.submit_report(bid, "A", 5, 1)
        check(outcome == "idempotent", "same revision same value idempotent")
        check(len(store.get_report_events(bid)) == 1, "idempotent retry: no node")

        outcome, _ = store.submit_report(bid, "A", 6, 1)
        check(outcome == "revision_value_conflict", "same rev different value")
        check(len(store.get_report_events(bid)) == 1, "value conflict: no node")

        outcome, _ = store.submit_report(bid, "B", 2, 1)
        check(outcome == "inserted", "second sortie first report")
        outcome, _ = store.submit_report(bid, "A", 7, 2)
        check(outcome == "inserted", "higher revision accepted")
        outcome, _ = store.submit_report(bid, "A", 5, 1)
        check(outcome == "stale_revision", "older revision rejected")
        check(len(store.get_report_events(bid)) == 3, "stale revision: no node")

        events = _check_chain(store, bid, 3, "node rules")
        check(events[0]["before"] == [] and
              events[0]["after"] == [{"sortie_id": "A", "actual_offset": 5,
                                      "revision": 1}],
              f"node 1 snapshots empty -> A@5: {events[0]}")
        check(events[1]["after"] == sorted(
            [{"sortie_id": "A", "actual_offset": 5, "revision": 1},
             {"sortie_id": "B", "actual_offset": 2, "revision": 1}],
            key=lambda r: r["sortie_id"]), "node 2 after-state has A and B")
        check(events[2]["before"] == events[1]["after"]
              and events[2]["after"] == sorted(
                  [{"sortie_id": "A", "actual_offset": 7, "revision": 2},
                   {"sortie_id": "B", "actual_offset": 2, "revision": 1}],
                  key=lambda r: r["sortie_id"]),
              "node 3 reflects the revision-2 overwrite")


def test_legacy_batch_baseline():
    """Batches existing before replay nodes: no back-filled history, and the
    first post-upgrade accepted report takes the latest known state as its
    baseline (its before-snapshot)."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        store = Store(path)
        bid = _make_batch(store)
        store.submit_report(bid, "A", 5, 1)
        store.submit_report(bid, "B", 2, 1)
        # Simulate a pre-upgrade database: reports exist, nodes do not.
        with sqlite3.connect(path) as c:
            c.execute("DELETE FROM report_events")
        check(store.get_report_events(bid) == [],
              "legacy batch has no replay nodes")

        outcome, _ = store.submit_report(bid, "A", 7, 2)
        check(outcome == "inserted", "post-upgrade report accepted")
        events = _check_chain(store, bid, 1, "legacy baseline")
        check(events[0]["before"] == sorted(
            [{"sortie_id": "A", "actual_offset": 5, "revision": 1},
             {"sortie_id": "B", "actual_offset": 2, "revision": 1}],
            key=lambda r: r["sortie_id"]),
            f"baseline is the latest state known at upgrade: {events[0]['before']}")
        check(events[0]["seq"] == 1, "first node of a legacy batch has seq 1")


def test_threads_commit_order():
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        sid = store.create_scenario({"sorties": []}, 30, 10)
        for trial in range(10):
            bid = store.create_batch(sid, f"b{trial}", {"sorties": [{"id": "A"}]},
                                     30, 10, {"offsets": {"A": 0}})

            def put(off, rev):
                store.submit_report(bid, "A", off, rev)

            ts = [threading.Thread(target=put, args=(10 * r, r))
                  for r in (1, 2, 3, 4, 5, 6, 7, 8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            events = _check_chain(store, bid, len(store.get_report_events(bid)),
                                  f"trial {trial}")
            check(len(events) >= 1, f"trial {trial}: at least the winner left a node")
            revs = [e["revision"] for e in events]
            check(revs == sorted(revs) and len(set(revs)) == len(revs),
                  f"trial {trial}: accepted revisions increase in commit order: {revs}")
            check(events[-1]["revision"] == 8
                  and events[-1]["after"][0]["actual_offset"] == 80,
                  f"trial {trial}: chain ends at the highest revision")


def _mp_worker(path, bid, off, rev, barrier, queue):
    store = Store(path)
    barrier.wait()
    outcome, info = store.submit_report(bid, "A", off, rev)
    queue.put((rev, outcome, info))


def test_multiprocess_commit_order():
    """Two OS processes race revisions 2 and 3 on top of a stored revision 1.

    Whichever commits first, the node chain must follow the actual commit
    order: if revision 2 committed first it left a node that revision 3's
    node builds on; if revision 3 committed first, revision 2 was rejected
    and left no node.  The final state is always revision 3.
    """
    import multiprocessing as mp

    ctx = mp.get_context("fork")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        setup = Store(path)
        sid = setup.create_scenario({"sorties": []}, 30, 10)

        for trial in range(10):
            bid = setup.create_batch(sid, f"race{trial}", {"sorties": [{"id": "A"}]},
                                     30, 10, {"offsets": {"A": 0}})
            setup.submit_report(bid, "A", 10, 1)
            barrier = ctx.Barrier(2)
            queue = ctx.Queue()
            procs = [ctx.Process(target=_mp_worker,
                                 args=(path, bid, off, rev, barrier, queue))
                     for rev, off in ((2, 20), (3, 30))]
            for p in procs:
                p.start()
            results = {rev: o for rev, o, _ in (queue.get(timeout=30) for _ in procs)}
            for p in procs:
                p.join(10)
                check(p.exitcode == 0, "worker process finished cleanly")

            events = _check_chain(setup, bid, len(setup.get_report_events(bid)),
                                  f"trial {trial}")
            race_events = events[1:]  # node 1 is the revision-1 seed
            check(events[0]["before"] == [] and
                  events[0]["after"] == [{"sortie_id": "A", "actual_offset": 10,
                                          "revision": 1}],
                  f"trial {trial}: seed node snapshots")
            if results[2] == "inserted":
                # Revision 2 committed first: two race nodes in commit order.
                check(len(race_events) == 2
                      and race_events[0]["revision"] == 2
                      and race_events[1]["revision"] == 3,
                      f"trial {trial}: commit order 2 then 3 -> two nodes, "
                      f"got {[(e['revision']) for e in race_events]}")
            else:
                # Revision 3 committed first: revision 2 was rejected, no node.
                check(results[2] == "stale_revision" and len(race_events) == 1
                      and race_events[0]["revision"] == 3,
                      f"trial {trial}: revision 2 rejected -> single node, "
                      f"got {results} / {[e['revision'] for e in race_events]}")
            final = events[-1]["after"]
            check(final == [{"sortie_id": "A", "actual_offset": 30, "revision": 3}],
                  f"trial {trial}: final state is revision 3 / offset 30")


def test_diff_warnings_intervals():
    def warn(conflicts, overdue=(), complete=None):
        return {
            "conflicts": [
                {"sortie_a": a, "sortie_b": b, "time_start": s, "time_end": e}
                for a, b, s, e in conflicts
            ],
            "first_conflict_time":
                min((s for _, _, s, _ in conflicts), default=None),
            "overdue_sorties": sorted(overdue),
            "complete": (not overdue) if complete is None else complete,
        }

    # Identical warnings: no changes anywhere.
    w = warn([("A", "B", 10.0, 20.0)])
    d = diff_warnings(w, warn([("A", "B", 10.0, 20.0)]))
    check(d["pairs"] == [], "identical warnings: no pair changes")
    check(d["first_conflict_time_before"] == 10.0
          and d["first_conflict_time_after"] == 10.0, "first conflict times")
    check(d["overdue_added"] == [] and d["overdue_resolved"] == [],
          "no overdue change")

    # Intersecting intervals: only the changed parts are marked.
    d = diff_warnings(warn([("A", "B", 10.0, 20.0)]),
                      warn([("A", "B", 15.0, 25.0)]))
    check(d["pairs"] == [{"sortie_a": "A", "sortie_b": "B",
                          "added": [[20.0, 25.0]], "resolved": [[10.0, 15.0]]}],
          f"intersection contributes only changed parts: {d['pairs']}")

    # Shrinking interval: nothing added, two resolved strips.
    d = diff_warnings(warn([("A", "B", 10.0, 30.0)]),
                      warn([("A", "B", 15.0, 20.0)]))
    check(d["pairs"][0]["added"] == []
          and d["pairs"][0]["resolved"] == [[10.0, 15.0], [20.0, 30.0]],
          f"contained interval resolves both sides: {d['pairs'][0]}")

    # Appearing / disappearing pairs and point intervals.
    d = diff_warnings(warn([]), warn([("A", "B", 5.0, 5.0)]))
    check(d["pairs"] == [{"sortie_a": "A", "sortie_b": "B",
                          "added": [[5.0, 5.0]], "resolved": []}],
          "point conflict interval appears")
    d = diff_warnings(warn([("A", "B", 5.0, 6.0)]), warn([]))
    check(d["pairs"][0]["resolved"] == [[5.0, 6.0]]
          and d["first_conflict_time_after"] is None,
          "resolved pair and cleared first conflict time")

    # Endpoint-touching intervals do not subtract from each other.
    d = diff_warnings(warn([("A", "B", 10.0, 20.0)]),
                      warn([("A", "B", 20.0, 30.0)]))
    check(d["pairs"][0]["added"] == [[20.0, 30.0]]
          and d["pairs"][0]["resolved"] == [[10.0, 20.0]],
          "touching intervals are fully marked as changed")

    # Overdue changes and completeness flags.
    d = diff_warnings(warn([], overdue=["B"]), warn([]))
    check(d["overdue_resolved"] == ["B"] and d["overdue_added"] == []
          and d["complete_before"] is False and d["complete_after"] is True,
          f"overdue resolved: {d}")
    d = diff_warnings(warn([]), warn([], overdue=["A", "C"]))
    check(d["overdue_added"] == ["A", "C"], "newly overdue sorties listed")


def test_replay_evaluation():
    """End-to-end replay math on real geometry, straight from the stored
    node snapshots, evaluated at one shared t."""
    sorties = two_crossing_sorties()
    plan = {"A": 0, "B": 5}  # conflict-free plan
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        bid = _make_batch(store)
        store.submit_report(bid, "A", 0, 1)   # node 1
        store.submit_report(bid, "B", 0, 1)   # node 2: B deviates to 0
        store.submit_report(bid, "B", 3, 2)   # node 3: corrected to +3

        def replay(seq, t):
            ev = store.get_report_event(bid, seq)
            sides = []
            for snapshot in (ev["before"], ev["after"]):
                reports = {r["sortie_id"]: Report(r["actual_offset"], r["revision"])
                           for r in snapshot}
                sides.append(evaluate_warning(sorties, plan, reports, 30, 10, t))
            return ev, sides[0], sides[1], diff_warnings(sides[0], sides[1])

        # Node 2: B goes from planned 5 to actual 0 -> conflict appears.
        ev, before, after, diff = replay(2, 0)
        check(ev["sortie_id"] == "B" and ev["revision"] == 1,
              "node identifies the triggering sortie and revision")
        check(not before["conflicts"] and len(after["conflicts"]) == 1,
              "conflict appears after the deviation")
        c = after["conflicts"][0]
        check(diff["pairs"] == [{"sortie_a": "A", "sortie_b": "B",
                                 "added": [[c["time_start"], c["time_end"]]],
                                 "resolved": []}],
              f"added interval equals the new conflict: {diff['pairs']}")
        check(diff["first_conflict_time_before"] is None
              and diff["first_conflict_time_after"] == c["time_start"],
              "first conflict time appears")

        # Node 3: B corrected 0 -> 3; intervals [7.879,12.121] -> [10,13]
        # intersect, so only the changed parts are marked.  t=3 is the
        # earliest evaluation time consistent with the reported take-off.
        _, before, after, diff = replay(3, 3)
        check(len(before["conflicts"]) == 1 and len(after["conflicts"]) == 1,
              "conflict persists through the correction")
        pair = diff["pairs"][0]
        check(abs(pair["added"][0][0] - 12.121320) < 1e-3
              and abs(pair["added"][0][1] - 13.0) < 1e-3,
              f"added part only: {pair['added']}")
        check(abs(pair["resolved"][0][0] - 7.878680) < 1e-3
              and abs(pair["resolved"][0][1] - 10.0) < 1e-3,
              f"resolved part only: {pair['resolved']}")
        check(abs(diff["first_conflict_time_before"] - 7.878680) < 1e-3
              and abs(diff["first_conflict_time_after"] - 10.0) < 1e-3,
              "first conflict times before/after")

        # Overdue change at a shared t: node 1 at t=5 — before, A's planned
        # take-off 0 is already past (overdue); after, A is reported at 0.
        _, before, after, diff = replay(1, 5)
        check(before["overdue_sorties"] == ["A"] and after["overdue_sorties"] == [],
              "overdue before, reported after")
        check(diff["overdue_resolved"] == ["A"] and diff["overdue_added"] == []
              and diff["complete_before"] is False
              and diff["complete_after"] is True,
              f"overdue resolution reported: {diff}")


def test_reload_consistency():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        store = Store(path)
        bid = _make_batch(store)
        store.submit_report(bid, "A", 0, 1)
        store.submit_report(bid, "B", 3, 1)
        # A fresh Store on the same file sees the identical node chain.
        reopened = Store(path)
        for seq in (1, 2):
            check(store.get_report_event(bid, seq)
                  == reopened.get_report_event(bid, seq),
                  f"node {seq} identical after reload")
        ev = reopened.get_report_event(bid, 2)
        sorties = two_crossing_sorties()
        plan = {"A": 0, "B": 5}

        def evaluate(snapshot):
            reports = {r["sortie_id"]: Report(r["actual_offset"], r["revision"])
                       for r in snapshot}
            return evaluate_warning(sorties, plan, reports, 30, 10, 3)

        check(diff_warnings(evaluate(ev["before"]), evaluate(ev["after"]))
              == diff_warnings(evaluate(ev["before"]), evaluate(ev["after"])),
              "reloaded replay evaluates identically")


# ---------------------------------------------------------------------------
# HTTP-level checks (FastAPI TestClient on a temporary database)
# ---------------------------------------------------------------------------

PAYLOAD = {
    "safety_radius": 30,
    "safety_height": 10,
    "sorties": [
        {"id": "A", "weight": 1, "window_hi": 20, "waypoints": [
            {"x": 0, "y": 0, "z": 20, "t": 0},
            {"x": 100, "y": 0, "z": 20, "t": 10},
            {"x": 200, "y": 0, "z": 20, "t": 20}]},
        {"id": "B", "weight": 1, "window_hi": 20, "waypoints": [
            {"x": 100, "y": -100, "z": 20, "t": 0},
            {"x": 100, "y": 100, "z": 20, "t": 20}]},
    ],
}


def test_http_replay():
    tmp = tempfile.TemporaryDirectory()
    os.environ["DCC_DB_PATH"] = os.path.join(tmp.name, "http.db")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/import", json=PAYLOAD)
        check(r.status_code == 200, f"import: {r.status_code}")
        sid = r.json()["scenario_id"]
        r = client.post(f"/api/scenarios/{sid}/solve", json={"time_limit": 10})
        check(r.status_code == 200 and r.json()["solution"]["feasible"],
              "solve feasible")
        r = client.post(f"/api/scenarios/{sid}/batches", json={"name": "replay"})
        check(r.status_code == 201, f"create batch: {r.status_code}")
        bid = r.json()["batch_id"]
        check(r.json()["events"] == [], "fresh batch has no replay nodes")

        def put(sortie, offset, revision):
            return client.put(f"/api/batches/{bid}/reports",
                              json={"sortie_id": sortie, "actual_offset": offset,
                                    "revision": revision})

        # Existing report response stays compatible.
        r = put("A", 0, 1)
        check(r.status_code == 200, "first report accepted")
        body = r.json()
        check(set(body) == {"batch_id", "report", "accepted", "idempotent"}
              and body["report"] == {"sortie_id": "A", "actual_offset": 0,
                                     "revision": 1}
              and body["accepted"] is True and body["idempotent"] is False,
              f"report response shape unchanged: {body}")

        r = put("A", 0, 1)
        check(r.status_code == 200 and r.json()["idempotent"] is True,
              "idempotent retry")
        check(len(client.get(f"/api/batches/{bid}").json()["events"]) == 1,
              "idempotent retry created no node")

        r = put("A", 9, 1)
        check(r.status_code == 409
              and r.json()["detail"]["reason"] == "revision_value_conflict",
              "same revision different value conflicts")
        check(len(client.get(f"/api/batches/{bid}").json()["events"]) == 1,
              "conflicting report created no node")

        check(put("B", 0, 1).status_code == 200, "B first report")   # node 2
        check(put("B", 3, 2).status_code == 200, "B corrected to +3")  # node 3
        check(put("B", 9, 3).status_code == 200, "B corrected to +9")  # node 4
        r = put("B", 3, 2)
        check(r.status_code == 409
              and r.json()["detail"]["reason"] == "stale_revision",
              "stale revision rejected")
        events = client.get(f"/api/batches/{bid}").json()["events"]
        check([e["seq"] for e in events] == [1, 2, 3, 4]
              and [e["revision"] for e in events] == [1, 1, 2, 3]
              and [e["sortie_id"] for e in events] == ["A", "B", "B", "B"],
              f"nodes in commit order with trigger info: {events}")

        # --- replay of node 3 (B corrected 0 -> 3) at t=3 ------------------
        # t=3 is the earliest evaluation time consistent with the reported
        # take-off; both conflict intervals start later, so they are intact.
        r = client.get(f"/api/batches/{bid}/replay/3?t=3")
        check(r.status_code == 200, f"replay node 3: {r.status_code}")
        rp = r.json()
        check(rp["node"] == {"seq": 3, "sortie_id": "B", "actual_offset": 3,
                             "revision": 2, "created_at": rp["node"]["created_at"]},
              f"node identifies triggering sortie/revision: {rp['node']}")
        check(rp["t"] == 3 and rp["before"]["t"] == 3 and rp["after"]["t"] == 3,
              "both sides evaluated at the same t")
        check(rp["reports_before"] == [
            {"sortie_id": "A", "actual_offset": 0, "revision": 1},
            {"sortie_id": "B", "actual_offset": 0, "revision": 1}],
            "complete before-state restored")
        check(rp["reports_after"] == [
            {"sortie_id": "A", "actual_offset": 0, "revision": 1},
            {"sortie_id": "B", "actual_offset": 3, "revision": 2}],
            "complete after-state restored")
        pair = rp["diff"]["pairs"][0]
        check((pair["sortie_a"], pair["sortie_b"]) == ("A", "B"),
              "diff keyed by sortie pair")
        check(abs(pair["added"][0][0] - 12.121320) < 1e-3
              and abs(pair["added"][0][1] - 13.0) < 1e-3
              and abs(pair["resolved"][0][0] - 7.878680) < 1e-3
              and abs(pair["resolved"][0][1] - 10.0) < 1e-3,
              f"intersecting intervals: only changed parts: {pair}")
        check(abs(rp["diff"]["first_conflict_time_before"] - 7.878680) < 1e-3
              and abs(rp["diff"]["first_conflict_time_after"] - 10.0) < 1e-3,
              "first conflict times before/after")

        # --- replay of node 1 at t=5: overdue resolved by the report -------
        r = client.get(f"/api/batches/{bid}/replay/1?t=5")
        check(r.status_code == 200, "replay node 1 at t=5")
        diff = r.json()["diff"]
        check(diff["overdue_before"] == ["A"] and diff["overdue_after"] == []
              and diff["overdue_resolved"] == ["A"]
              and diff["complete_before"] is False
              and diff["complete_after"] is True,
              f"overdue change at shared t: {diff}")

        # --- rejections ----------------------------------------------------
        r = client.get(f"/api/batches/{bid}/replay/3?t=2")
        check(r.status_code == 400
              and r.json()["detail"]["reason"] == "t_before_reported_takeoff"
              and r.json()["detail"]["side"] == "after"
              and "diff" not in r.json(),
              "t earlier than an after-side take-off rejects the whole replay")
        r = client.get(f"/api/batches/{bid}/replay/4?t=2")
        check(r.status_code == 400
              and r.json()["detail"]["reason"] == "t_before_reported_takeoff"
              and r.json()["detail"]["side"] == "before",
              "t earlier than a before-side take-off rejects too")
        r = client.get(f"/api/batches/{bid}/replay/4?t=-1")
        check(r.status_code == 400, "negative t rejected")
        r = client.get(f"/api/batches/{bid}/replay/99?t=0")
        check(r.status_code == 404, "unknown node -> 404")
        r = client.get(f"/api/batches/9999/replay/1?t=0")
        check(r.status_code == 404, "unknown batch -> 404")

        # --- latest warning stays compatible -------------------------------
        r = client.get(f"/api/batches/{bid}/warning?t=9")
        check(r.status_code == 200 and "warning" in r.json()
              and r.json()["warning"]["t"] == 9
              and {row["id"] for row in r.json()["warning"]["sorties"]} == {"A", "B"},
              "warning endpoint unchanged")
        r = client.get(f"/api/batches/{bid}/warning?t=1")
        check(r.status_code == 400
              and r.json()["detail"]["reason"] == "t_before_reported_takeoff",
              "warning early-t rule unchanged")

        # --- reload gives identical replay ---------------------------------
        again = client.get(f"/api/batches/{bid}/replay/3?t=3").json()
        check(again == rp, "reloaded replay is identical")
    tmp.cleanup()


def main():
    test_node_creation_rules()
    test_legacy_batch_baseline()
    test_threads_commit_order()
    test_multiprocess_commit_order()
    test_diff_warnings_intervals()
    test_replay_evaluation()
    test_reload_consistency()
    test_http_replay()
    print("failures:", FAILS)
    return FAILS


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
