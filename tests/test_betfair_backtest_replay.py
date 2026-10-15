"""Primary-member replay to a decision time (betangel_research.betfair.backtest.replay) on synthetic archives.

Every fixture is generated here and goes through the real task (b) ingest and index, so each archive, its manifest and
its index are built for exactly the bytes a test replays (a cut or mutated stream gets its own). These tests show the
point-in-time rules on these synthetic shapes: the view at t holds every message published at or before t, the
equal-pt batch at t included and nothing after it; rc.ltp follows the image rule; the view is the index's
decision_time at t; and an input that is not what the manifest and the index say refuses. They say nothing about
real Betfair files or any strategy's results. The helpers here are reused by tests/test_betfair_backtest_cohort.py.
"""

import bz2
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from betangel_research.betfair.backtest import market_index, replay
from betangel_research.betfair.backtest.archive import ingest_archive, safe_target
from betangel_research.betfair.backtest.market_index import index_archives, load_manifest
from betangel_research.betfair.backtest.replay import ReplayProblem, replay_view, usable_ltp

REPO = Path(__file__).resolve().parent.parent
DRAFT = REPO / "research" / "experiments" / "backtest-p0-bsp-v1.json"
OFF = datetime(2015, 6, 1, 14, 0, tzinfo=timezone.utc)
OFFSET_S = -300
T = OFF + timedelta(seconds=OFFSET_S)
EVENT = "27000001"


def frozen_contract(delay_s=0, max_trials=4, **changes):
    """The P0 draft frozen with operational values (as the contract tests do), changed as asked."""
    value = json.loads(DRAFT.read_text(encoding="utf-8"))
    value.update(status="frozen", frozen_on="2026-10-14")
    value["decision_timing"]["delay_s"] = delay_s
    value["stopping_rule"]["max_trials"] = max_trials
    for key, item in changes.items():
        value[key] = item
    return value


def ms(when):
    return int(when.timestamp() * 1000)


def iso(when):
    return when.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def runner(rid, status="ACTIVE", bsp=None):
    item = {"id": rid, "status": status, "sortPriority": rid, "name": f"Horse {rid}"}
    if bsp is not None:
        item["bsp"] = bsp
    return item


def definition(off, runners, status="OPEN", in_play=False, **extra):
    value = {"bspMarket": True, "turnInPlayEnabled": True, "persistenceEnabled": True, "marketBaseRate": 5.0,
             "eventId": EVENT, "eventTypeId": "7", "numberOfWinners": 1, "bettingType": "ODDS", "marketType": "WIN",
             "marketTime": iso(off), "suspendTime": iso(off), "bspReconciled": False, "complete": True,
             "inPlay": in_play, "crossMatching": True, "runnersVoidable": False, "betDelay": 0, "status": status,
             "numberOfActiveRunners": sum(1 for r in runners if r.get("status") == "ACTIVE"), "runners": runners,
             "regulators": ["MR_INT"], "venue": "Synthetic Park", "countryCode": "GB", "discountAllowed": True,
             "timezone": "Europe/London", "openDate": iso(off), "version": 1, "eventName": "Synthetic 1st Jun",
             "name": "1m Hcap"}
    value.update(extra)
    return {key: item for key, item in value.items() if item is not DROP}


DROP = object()


def mcm(when, changes):
    pt = when if isinstance(when, int) else ms(when)
    return {"op": "mcm", "clk": "1", "pt": pt, "mc": changes}


def md(market_id, when, value, **change):
    return mcm(when, [{"id": market_id, "marketDefinition": value, **change}])


def prices(market_id, when, ltps, **change):
    """An rc change: ltps maps runner id to a price (or to DROP for an rc entry without ltp)."""
    rc = [{"id": rid} if price is DROP else {"id": rid, "ltp": price} for rid, price in ltps.items()]
    return mcm(when, [{"id": market_id, "rc": rc, **change}])


def lines(messages):
    return b"".join(m if isinstance(m, bytes) else json.dumps(m).encode() + b"\n" for m in messages)


def settle(market_id, off, statuses, bsps):
    """Post-off messages: BSP reconciled, then CLOSED with the given final statuses."""
    final = [runner(rid, statuses[rid], bsps[rid]) for rid in sorted(statuses)]
    return [md(market_id, off + timedelta(minutes=2), definition(off, final, in_play=True, bspReconciled=True)),
            md(market_id, off + timedelta(minutes=8),
               definition(off, final, status="CLOSED", in_play=True, bspReconciled=True,
                          settledTime=iso(off + timedelta(minutes=8))))]


def standard_market(market_id="1.100000001", off=OFF, ltps=None, winner=1):
    """Three runners published at off-2h, prices at off-30m, off-10m and off-4m, settled after the off."""
    opening = [runner(1), runner(2), runner(3)]
    ltps = ltps or {1: 3.0, 2: 2.5, 3: 6.0}
    return [md(market_id, off - timedelta(hours=2), definition(off, opening)),
            prices(market_id, off - timedelta(minutes=30), {rid: price + 1 for rid, price in ltps.items()}),
            prices(market_id, off - timedelta(minutes=10), ltps),
            prices(market_id, off - timedelta(minutes=4), {1: 1.01, 2: 1.01, 3: 1.01}),
            *settle(market_id, off, {1: "LOSER", 2: "LOSER", 3: "LOSER", winner: "WINNER"}, {1: 3.1, 2: 2.4, 3: 7.0})]


def path(market, when=OFF, plan="BASIC", event=EVENT):
    return f"{plan}/{when.year}/{when.strftime('%b')}/{when.day}/{event}/{market}.bz2"


def make_tar(target, members):
    with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return target


def cut(messages, t):
    """The messages a stream holds when it ends after its last batch published at or before t (raw byte lines
    before that point are kept, in order)."""
    kept = []
    for message in messages:
        if isinstance(message, dict) and message["pt"] > t:
            break
        kept.append(message)
    return kept


class Workspace(unittest.TestCase):
    """Temporary data roots outside the repository; build() ingests and indexes one archive per call."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.builds = 0

    def build(self, members):
        """(data root, manifest, {market id: record}) of a fresh archive of members [(path, messages)]."""
        self.builds += 1
        root = self.tmp / f"data{self.builds}"
        root.mkdir()
        tar = make_tar(self.tmp / f"archive{self.builds}.tar",
                       [(name, bz2.compress(lines(messages))) for name, messages in members])
        result = ingest_archive(tar, root)
        self.assertEqual(result["status"], "ingested", result)
        indexed = index_archives(root)
        self.assertEqual(indexed["status"], "indexed", indexed)
        sha = result["archive_sha256"]
        manifest, _ = load_manifest(root, sha)
        data = (root / "index" / sha / market_index.MARKETS_FILE).read_bytes()
        records = {record["market_id"]: record for record in map(json.loads, data.splitlines())}
        return root, manifest, records

    def view(self, members, market_id="1.100000001", offset_s=OFFSET_S):
        root, manifest, records = self.build(members)
        return replay_view(root, manifest, records[market_id], offset_s)


class ViewTests(Workspace):
    def test_the_view_holds_the_last_price_and_definition_at_or_before_t(self):
        view = self.view([(path("1.100000001"), standard_market())])
        self.assertEqual((view.decision_ms, view.anchor_ms, view.anchor_value), (ms(T), ms(OFF), iso(OFF)))
        self.assertEqual(view.anchor_pt, ms(OFF - timedelta(hours=2)))
        self.assertEqual([(r.selection_id, r.status, r.listed, r.ltp) for r in view.runners],
                         [(1, "ACTIVE", True, 3.0), (2, "ACTIVE", True, 2.5), (3, "ACTIVE", True, 6.0)])
        self.assertEqual({r.ltp_pt for r in view.runners}, {ms(OFF - timedelta(minutes=10))})
        self.assertEqual((view.field("status"), view.field("inPlay"), view.field("bspMarket")), ("OPEN", False, True))
        self.assertIsNone(view.field("bspReconciled"))             # an outcome field never reaches the view
        self.assertEqual(view.removals, ())
        self.assertEqual(view.malformed_price_changes, 0)

    def test_the_equal_pt_batch_at_t_is_whole_and_the_next_millisecond_is_out(self):
        market = "1.100000001"
        messages = standard_market(market)[:1] + [
            prices(market, ms(T), {1: 4.0}), prices(market, ms(T), {2: 5.0}),
            prices(market, ms(T) + 1, {1: 9.0, 3: 9.0}), *standard_market(market)[4:]]
        view = self.view([(path(market), messages)])
        self.assertEqual([(r.selection_id, r.ltp, r.ltp_pt) for r in view.runners],
                         [(1, 4.0, ms(T)), (2, 5.0, ms(T)), (3, None, None)])

    def test_an_image_resets_prices_it_does_not_carry_and_null_resets_one(self):
        market = "1.100000001"
        opening = [runner(1), runner(2), runner(3)]
        early = OFF - timedelta(minutes=20)
        for later, expected in (
                ([prices(market, early, {1: 4.0}, img=True)], [4.0, None, None]),
                ([prices(market, early, {1: DROP, 2: 7.0}, img=True)], [None, 7.0, None]),
                ([md(market, early, definition(OFF, opening), img=True)], [None, None, None]),
                ([prices(market, early, {2: None})], [3.0, None, 6.0]),
                ([prices(market, early, {2: DROP})], [3.0, 2.5, 6.0])):
            with self.subTest(later=later):
                messages = [md(market, OFF - timedelta(hours=2), definition(OFF, opening)),
                            prices(market, OFF - timedelta(minutes=30), {1: 3.0, 2: 2.5, 3: 6.0}), *later]
                view = self.view([(path(market), messages)])
                self.assertEqual([r.ltp for r in view.runners], expected)

    def test_an_image_definition_that_omits_a_runner_unlists_it(self):
        market = "1.100000001"
        messages = [md(market, OFF - timedelta(hours=2), definition(OFF, [runner(1), runner(2)])),
                    md(market, OFF - timedelta(minutes=20), definition(OFF, [runner(1)]), img=True)]
        view = self.view([(path(market), messages)])
        self.assertEqual([(r.selection_id, r.status, r.listed) for r in view.runners],
                         [(1, "ACTIVE", True), (2, None, False)])

    def test_removals_and_revisions_at_or_before_t_are_in_and_later_ones_are_not(self):
        market = "1.100000001"
        opening = [runner(1), runner(2), runner(3)]
        removed = [runner(1), runner(2, "REMOVED"), runner(3)]
        later_off = OFF + timedelta(minutes=10)
        messages = [md(market, OFF - timedelta(hours=2), definition(OFF, opening)),
                    md(market, OFF - timedelta(minutes=20), definition(later_off, removed)),
                    md(market, OFF - timedelta(minutes=2), definition(later_off, [runner(1), removed[1],
                                                                                   runner(3, "REMOVED")]))]
        view = self.view([(path(market), messages)])
        self.assertEqual(view.removals, ((2, ms(OFF - timedelta(minutes=20)), "REMOVED"),))
        self.assertEqual([r.status for r in view.runners], ["ACTIVE", "REMOVED", "ACTIVE"])
        self.assertEqual(view.field("marketTime"), iso(later_off))
        self.assertEqual(view.anchor_value, iso(OFF))               # the anchor never moves with a revision

    def test_only_the_named_market_of_an_event_file_is_folded(self):
        first, second = "1.100000001", "1.100000002"
        messages = standard_market(first)[:3] + [prices(second, OFF - timedelta(minutes=9), {1: 1.5})]
        root, manifest, records = self.build([(f"BASIC/2015/Jun/1/{EVENT}/{EVENT}.bz2", messages)])
        self.assertEqual(records[second]["decision_time"], market_index.no_view())   # a price holds no definition
        view = replay_view(root, manifest, records[first], OFFSET_S)
        self.assertEqual([r.ltp for r in view.runners], [3.0, 2.5, 6.0])

    def test_runner_changes_without_a_parseable_id_are_counted_not_guessed(self):
        market = "1.100000001"
        messages = standard_market(market)[:3] + [mcm(OFF - timedelta(minutes=8),
                                                       [{"id": market, "rc": [{"id": "x", "ltp": 1.2}]}])]
        self.assertEqual(self.view([(path(market), messages)]).malformed_price_changes, 1)

    def test_a_failure_after_t_does_not_cancel_the_view_at_t(self):
        market = "1.100000001"
        messages = standard_market(market)[:4] + [b"{not json\n"] + standard_market(market)[4:]
        root, manifest, records = self.build([(path(market), messages)])
        outcome = records[market]["evaluator_only_outcome"]
        self.assertFalse(outcome["read_complete"])
        self.assertGreater(records[market]["decision_time"]["view_ends"]["pt"], ms(T))
        view = replay_view(root, manifest, records[market], OFFSET_S)
        self.assertEqual([r.ltp for r in view.runners], [3.0, 2.5, 6.0])

    def test_a_failure_with_a_known_pt_after_t_does_not_cancel_the_view_at_t(self):
        market = "1.100000001"
        segmented = {"op": "mcm", "clk": "1", "pt": ms(OFF - timedelta(minutes=4)), "segmentType": "SEG_START",
                     "mc": []}
        messages = standard_market(market)[:3] + [segmented] + standard_market(market)[3:]
        root, manifest, records = self.build([(path(market), messages)])
        ends = records[market]["decision_time"]["view_ends"]
        self.assertEqual(ends, {"pt": segmented["pt"], "reason": "member_unreadable_from_pt"})
        view = replay_view(root, manifest, records[market], OFFSET_S)
        self.assertEqual([r.ltp for r in view.runners], [3.0, 2.5, 6.0])

    def test_the_view_is_immutable_all_the_way_down(self):
        view = self.view([(path("1.100000001"), standard_market())])
        self.assertEqual(view.field("regulators"), ("MR_INT",))
        with self.assertRaises(AttributeError):
            view.field("regulators").append("SYNTHETIC_MUTATION")
        with self.assertRaises(Exception):
            view.runners[0].ltp = 1.5
        self.assertEqual(replay.frozen({"b": [1, {"c": [2]}], "a": None}), (("a", None), ("b", (1, (("c", (2,)),)))))
        odd = standard_market()[:3] + [prices("1.100000001", OFF - timedelta(minutes=6), {1: [2.0, {"x": 1}]})]
        runner_1 = self.view([(path("1.100000001"), odd + standard_market()[3:])]).runners[0]
        self.assertEqual(runner_1.ltp, (2.0, (("x", 1),)))     # a raw value that is not a price is frozen too

    def test_usable_ltp(self):
        for value, usable in ((1.01, True), (2, True), (0, False), (-1.5, False), (None, False), ("2.0", False),
                              (True, False)):
            with self.subTest(value=value):
                self.assertIs(usable_ltp(value), usable)


class PreconditionAndIntegrityTests(Workspace):
    def setUp(self):
        super().setUp()
        self.market = "1.100000001"
        self.root, self.manifest, self.records = self.build([(path(self.market), standard_market(self.market))])
        self.record = self.records[self.market]

    def assertProblem(self, reason, record=None, manifest=None):
        with self.assertRaises(ReplayProblem) as caught:
            replay_view(self.root, manifest or self.manifest, record or self.record, OFFSET_S)
        self.assertEqual(str(caught.exception), f"replay:{self.market}:{reason}")

    def test_preconditions_are_the_callers(self):
        no_view = json.loads(json.dumps(self.record))
        no_view["decision_time"] = market_index.no_view()
        for record, offset, reason in ((no_view, OFFSET_S, "replay:no_view"),
                                       (self.record, -3 * 3600, "replay:anchor_after_decision"),
                                       (self.record, 0, "replay:decision_at_or_after_view_ends")):
            with self.subTest(reason=reason), self.assertRaises(ValueError) as caught:
                replay_view(self.root, self.manifest, record, offset)
            self.assertEqual(str(caught.exception), reason)

    def test_a_changed_member_refuses(self):
        raw = safe_target(self.root, self.manifest["archive"]["sha256"], self.record["provenance"]["member_path"])
        raw.chmod(0o600)
        raw.write_bytes(bz2.compress(lines(standard_market(self.market, ltps={1: 9.0, 2: 9.5, 3: 9.9}))))
        self.assertProblem("member_hash_mismatch")
        raw.unlink()
        self.assertProblem("member_missing")

    def test_a_record_that_names_another_member_or_archive_refuses(self):
        for key, value, reason in (("member_sha256", "0" * 64, "member_sha256_mismatch"),
                                   ("member_path", "BASIC/2015/Jun/1/27000001/1.100000009.bz2",
                                    "member_not_in_manifest"),
                                   ("archive_sha256", "1" * 64, "archive_mismatch")):
            with self.subTest(key=key):
                record = json.loads(json.dumps(self.record))
                record["provenance"][key] = value
                self.assertProblem(reason, record)

    def test_a_record_whose_anchor_view_end_or_path_facts_disagree_refuses(self):
        moved = json.loads(json.dumps(self.record))
        moved["decision_time"]["first_published_market_time"]["value"] = iso(OFF + timedelta(seconds=200))
        self.assertProblem("anchor_mismatch", moved)
        early = json.loads(json.dumps(self.record))
        early["decision_time"]["first_published_market_time"]["pt"] += 1
        self.assertProblem("anchor_mismatch", early)
        for key, value in (("plan", "ADVANCED"), ("path_month", "2015-07"), ("path_date", "2015-06-02"),
                           ("event_id_from_path", "27000002"), ("file_type", "event"),
                           ("file_market_id", "1.100000009")):
            with self.subTest(key=key):
                record = json.loads(json.dumps(self.record))
                record["provenance"][key] = value
                self.assertProblem("provenance_mismatch", record)

    def test_an_outcome_before_t_never_reaches_a_view(self):
        three = [runner(1), runner(2), runner(3)]
        closed = [runner(1, "WINNER", 2.0), runner(2, "LOSER", 3.0), runner(3, "LOSER", 4.0)]
        market = "1.100000002"
        root, manifest, records = self.build([(path(market), [
            md(market, OFF - timedelta(hours=2), definition(OFF, three)),
            md(market, OFF - timedelta(minutes=10), definition(OFF, closed, status="CLOSED"))])])
        record = records[market]
        self.assertEqual(record["decision_time"]["view_ends"]["reason"], "first_outcome_bearing_definition")
        record["decision_time"]["view_ends"] = {"pt": ms(OFF), "reason": "first_published_scheduled_off"}
        with self.assertRaises(ReplayProblem) as caught:
            replay_view(root, manifest, record, OFFSET_S)
        self.assertEqual(str(caught.exception), f"replay:{market}:outcome_before_decision")

    def test_a_record_that_disagrees_with_the_replay_refuses(self):
        for change in ("status", "runner", "removal", "first_pt"):
            with self.subTest(change=change):
                record = json.loads(json.dumps(self.record))
                decision = record["decision_time"]
                if change == "status":
                    decision["definition"]["status"][0][1] = "SUSPENDED"
                elif change == "runner":
                    decision["runners"].pop()
                elif change == "removal":
                    decision["removals"].append({"id": 1, "pt": ms(T) - 1, "status": "REMOVED",
                                                 "removal_date": None, "adjustment_factor": None})
                else:
                    decision["runners"][0]["first_pt"] += 1
                self.assertProblem("index_mismatch", record)

    def test_a_disagreement_after_t_is_not_compared(self):
        record = json.loads(json.dumps(self.record))
        record["decision_time"]["definition"]["version"].append([ms(T) + 1, 99])
        view = replay_view(self.root, self.manifest, record, OFFSET_S)
        self.assertEqual(view.field("version"), 1)


if __name__ == "__main__":
    unittest.main()
