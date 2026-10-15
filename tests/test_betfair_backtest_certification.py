"""Engine certification fixtures and cases (betangel_research.betfair.backtest.certification_fixtures and certify;
task c2b3b2a).

These show: the bytes-only ustar writer gives what tarfile reads back, byte for byte what tarfile itself writes for
the same fields, and refuses what it does not write; every synthetic world goes through the real ingest and index;
the limit world holds exactly the prefix asked for, and the bytes, lines and digests the limit cases expect follow
from its layout alone; the settlement witness's answers follow from decimal arithmetic and tell the two roundings
apart; the case registry is the protocol's literal, and its runners, counters and expected constants correspond one
to one with no value left to derive; every case is as expected on this engine; an engine changed where a case looks
makes that case not as expected; an exception is an error, never a pass; a case whose counts or kind invariant do
not hold is not as expected; and the seeded draws, decision ids and decision blocks in the constants re-derive from
their documented recipes with hashlib alone. They say nothing about real files or results.
"""

import bz2
import contextlib
import dataclasses
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from betangel_research.betfair.backtest import (accounting, archive, certification_fixtures as cf, certify, cohort,
                                              engineering, runner)
from betangel_research.betfair.backtest.archive import ingest_archive
from betangel_research.betfair.backtest.market_index import index_archives

C, N, I = certify.CHECK, certify.NEGATIVE, certify.INCOMPLETE
# The protocol's registry, written out: every case id, its kind and the exact counts its projection reports.
REGISTRY = (
    ("kat", C, {"kat_cases": 13}),
    ("scenario_full", C, {"tally_records": 22, "tally_compared": 19, "markets": 22}),
    ("scenario_cut", C, {"compared_markets": 20, "changed_outcomes": 19}),
    ("scenario_deleted", C, {"compared_markets": 20, "changed_outcomes": 17}),
    ("scenario_revised", C, {"compared_markets": 20, "changed_outcomes": 16}),
    ("second_point", C, {"markets": 22}),
    ("settlement_witness", C, {"points": 2}),
    ("market_conditions", C, {"markets": 28}),
    ("sources_named_file_decides", C, {}),
    ("sources_primary_rules", C, {"worlds": 6}),
    ("membership_gates", C, {"refused_members": 2}),
    ("duplicate_across_archives", N, {}),
    ("limit_small_exact", C, {}),
    ("limit_small_over", C, {}),
    ("limit_batch_exact", C, {}),
    ("limit_batch_inside", C, {}),
    ("limit_64mib_exact", C, {}),
    ("limit_64mib_over", C, {}),
    ("read_limits_exact", C, {}),
    ("read_limits_unexercised", C, {}),
    ("abstaining_incomplete", I, {}),
    ("unexercised_incomplete", I, {}),
    ("nc_future_leak", N, {}),
    ("nc_pre_t_change", N, {}),
    ("nc_borrowed_rows", N, {}),
    ("nc_ledger_byte", N, {}),
    ("nc_swapped_bets", N, {}),
    ("nc_dropped_decision", N, {}),
    ("nc_short_settlement", N, {}),
    ("nc_decision_without_record", N, {}),
    ("nc_duplicate_record", N, {}),
    ("nc_other_variant_row", N, {}),
    ("nc_settlement_other_variants", N, {}),
    ("nc_wrong_decision_time", N, {}),
    ("nc_member_tampered", N, {}),
    ("nc_markets_changed", N, {}),
    ("nc_same_key_substitution", N, {}),
    ("nc_rejected_tally_addition", N, {}),
)
RECONCILIATION_CONTROLS = ("nc_borrowed_rows", "nc_ledger_byte", "nc_swapped_bets", "nc_dropped_decision",
                           "nc_short_settlement", "nc_decision_without_record", "nc_duplicate_record",
                           "nc_other_variant_row", "nc_settlement_other_variants")


def draw(seed, label, n):
    """The hash-stream draw (metrics module docstring), written again with hashlib: the first 64-bit word, read
    big-endian from sha256(domain, 0x00, [seed, label] as compact JSON, 0x00, counter), below the largest multiple
    of n, taken modulo n."""
    prefix = (b"betangel-research/backtest/hash-stream/v1\x00"
              + json.dumps([seed, label], separators=(",", ":")).encode() + b"\x00")
    limit, counter = 2 ** 64 - 2 ** 64 % n, 0
    while True:
        digest = hashlib.sha256(prefix + counter.to_bytes(8, "big")).digest()
        counter += 1
        for start in (0, 8, 16, 24):
            value = int.from_bytes(digest[start:start + 8], "big")
            if value < limit:
                return value % n


def decision_id(contract_sha256, axis, market_id, selection_id):
    """A row's decision id (cohort docstring), written again: the sha256 of the compact JSON of [domain, contract
    sha256, market id, selection id, decision axis]."""
    text = json.dumps(["betangel-research/backtest/decision-id/v1", contract_sha256, market_id, selection_id, axis],
                      separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class Workspace(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()


class UstarTests(Workspace):
    def test_tarfile_reads_back_every_member(self):
        members = [("BASIC/2015/Jun/1/27000001/1.400000001.bz2", b"abc"), ("x", b""), ("y" * 100, b"z" * 1000),
                   ("w", b"q" * 512), ("v", b"after a full block")]
        data = cf.ustar(members)
        self.assertEqual(len(data), sum(512 + -(-len(body) // 512) * 512 for _, body in members) + 1024)
        self.assertEqual(data[-1024:], bytes(1024))
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|") as tar:
            read = [(info.name, info.size, info.mode, info.type, info.uid, info.gid, info.mtime,
                     tar.extractfile(info).read()) for info in tar]
        self.assertEqual(read, [(name, len(body), 0o644, tarfile.REGTYPE, 0, 0, 0, body) for name, body in members])

    def test_it_is_what_tarfile_writes_for_the_same_fields(self):
        members = [("a/b.bz2", b"payload" * 100), ("c.bz2", b"q")]
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for name, body in members:
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime, info.uname, info.gname = len(body), 0o644, 0, "", ""
                tar.addfile(info, io.BytesIO(body))
        ours, theirs = cf.ustar(members), out.getvalue()
        self.assertEqual(theirs[:len(ours)], ours)
        self.assertEqual(theirs[len(ours):], bytes(len(theirs) - len(ours)))     # tarfile pads to its record size

    def test_what_it_does_not_write_is_refused(self):
        for name, members in (("an empty name", [("", b"")]), ("a long name", [("x" * 101, b"")]),
                              ("a name outside ASCII", [("é.bz2", b"")]), ("a space", [("a b", b"")]),
                              ("a control character", [("a\tb", b"")]), ("an absolute name", [("/a", b"")]),
                              ("a trailing slash", [("a/", b"")]), ("an empty segment", [("a//b", b"")]),
                              ("a dot segment", [("a/./b", b"")]), ("a dot-dot segment", [("../a", b"")]),
                              ("a repeated name", [("a", b""), ("a", b"")]),
                              ("data that is not bytes", [("a", "text")])):
            with self.subTest(case=name), self.assertRaises(ValueError):
                cf.ustar(members)
        self.assertEqual(cf._octal(8 ** 11 - 1, 12), b"77777777777\x00")
        with self.assertRaisesRegex(ValueError, "field_overflow"):
            cf._octal(8 ** 11, 12)


class WorldTests(Workspace):
    def build(self, world, name):
        root = self.tmp / name
        root.mkdir()
        for index, tar in enumerate(world):
            target = self.tmp / f"{name}-{index}.tar"
            target.write_bytes(cf.ustar(tar))
            self.assertEqual(ingest_archive(target, root)["status"], "ingested")
        indexed = index_archives(root)
        self.assertEqual(indexed["status"], "indexed")
        return [archive["markets"] for archive in indexed["archives"]]

    def test_every_world_is_ingested_and_indexed(self):
        for name, world, markets in (
                *((form, cf.scenario_world(form), [20 if form == "cut" else 22]) for form in cf.FORMS),
                *((f"sources-{change}", cf.sources_world(change), [1])
                  for change in ("none", "event_file", "market_file")),
                *((f"event-alone-{change}", cf.event_alone_world(change), [1]) for change in ("none", "event_file")),
                ("membership", cf.membership_world(), [1]), ("duplicate", cf.duplicate_world(), [1, 1]),
                ("limit", cf.limit_world(4096), [1]), ("limit-batch", cf.limit_world(4096, batch_at_t=True), [1]),
                ("abstaining", cf.abstaining_world(), [2]), ("unexercised", cf.unexercised_world(), [3]),
                ("conditions", cf.conditions_world(), [28]),
                *((f"rule-{rule}", world, None) for rule, world in cf.source_rules_worlds().items())):
            with self.subTest(world=name):
                found = self.build(world, name)
                if markets is None:
                    self.assertTrue(found and all(found))
                else:
                    self.assertEqual(found, markets)

    def test_the_limit_world_holds_exactly_the_prefix_asked_for(self):
        for retained, batch in ((4096, False), (4097, False), (4096 + 18, False), (1 << 21, False), (4096, True)):
            with self.subTest(retained=retained, batch=batch):
                (_, data), = cf.limit_world(retained, batch_at_t=batch)[0]
                raw = bz2.decompress(data)
                after = cf.lines(cf.post_full(cf.LIMIT_MARKET))
                self.assertEqual(raw[len(raw) - len(after):], after)
                self.assertEqual(len(raw) - len(after), retained)
                self.assertEqual(b"".join(cf.limit_lines(retained, batch_at_t=batch)), raw[:retained])
                if batch:                              # the prefix ends with two price messages at exactly t
                    tail = [json.loads(line) for line in raw[:retained].splitlines()[-2:]]
                    self.assertEqual([message["pt"] for message in tail], [cf.ms(cf.T)] * 2)
        with self.assertRaisesRegex(ValueError, "retained_too_small"):
            cf.limit_world(10)
        self.assertNotEqual(cf._filler_text(0, 64), cf._filler_text(1, 64))
        self.assertGreater(len(bz2.compress(cf._filler_text(0, 1 << 20))), (1 << 20) // 200)   # the ratio bound

    def test_the_limit_cases_keep_what_the_layout_gives(self):
        """LIMIT_KEPT re-derives from the fixture's lines alone: a passed prefix keeps every line before the first
        message after t, with its tagged digest; an unexercised one keeps the whole lines before the first that
        would pass the limit (the prefix limit, or the read limit for read_limits_unexercised), with no digest."""
        tag = b"betangel-research/backtest/primary-prefix/v1\x00"
        self.assertEqual(tag, engineering.PREFIX_DIGEST_TAG)
        mib64 = 64 * 1024 * 1024
        for case_id, (retained, limit, batch, filler, line_limit) in {
                "limit_small_exact": (4096, 4096, False, 1 << 20, None),
                "limit_small_over": (4097, 4096, False, 1 << 20, None),
                "limit_batch_exact": (4096, 4096, True, 1 << 20, None),
                "limit_batch_inside": (4096, 4095, True, 1 << 20, None),
                "limit_64mib_exact": (mib64, mib64, True, 1 << 20, None),
                "limit_64mib_over": (mib64 + 1, mib64, True, 1 << 20, None),
                "read_limits_exact": (4096, mib64, False, 1024, 1024),
                "read_limits_unexercised": (4096, mib64, False, 1025, 1024)}.items():
            with self.subTest(case=case_id):
                lines = cf.limit_lines(retained, filler, batch)
                kept = count = 0
                for line in lines:
                    if kept + len(line) > limit or line_limit is not None and len(line) > line_limit:
                        break
                    kept, count = kept + len(line), count + 1
                whole = count == len(lines)
                digest = hashlib.sha256(tag + b"".join(lines)).hexdigest() if whole else None
                self.assertEqual(cf.LIMIT_KEPT[case_id], [kept, count, digest])

    def test_the_two_parameter_points(self):
        p0, second = cf.p0_rules(), cf.second_point_rules()
        self.assertEqual((p0[0].offset_s, p0[0].stake_pence, p0[0].selection_seed, p0[1].commission_rate,
                          p0[1].rounding), (-300, 100, 1, accounting.Decimal("0.05"), "per_market_ceil_penny"))
        self.assertEqual((second[0].offset_s, second[0].stake_pence, second[0].selection_seed,
                          second[1].commission_rate, second[1].rounding),
                         (-600, 250, 7, accounting.Decimal("0.02"), "per_market_half_up_penny"))
        self.assertNotEqual(p0[0].contract_sha256, second[0].contract_sha256)

    def test_the_settlement_witness_tells_the_points_apart(self):
        """Worked again with decimal alone: each point's gross, commission and net; the other rounding mode would
        give another commission."""
        bsp = Decimal(cf.WITNESS_BSP)
        for point, stake, rate, rounding, other in (("p0", 100, "0.05", ROUND_CEILING, ROUND_HALF_UP),
                                                    ("second_point", 250, "0.02", ROUND_HALF_UP, ROUND_CEILING)):
            with self.subTest(point=point):
                gross = int((stake * (bsp - 1)).to_integral_value(rounding=ROUND_FLOOR))
                commission = int((Decimal(rate) * gross).to_integral_value(rounding=rounding))
                self.assertEqual(cf.EXPECTED["settlement_witness"][point]["market"],
                                 [gross, commission, gross - commission])
                self.assertNotEqual(int((Decimal(rate) * gross).to_integral_value(rounding=other)), commission)


class RegistryTests(unittest.TestCase):
    def test_the_registry_is_the_protocols(self):
        self.assertEqual(certify.CASES, REGISTRY)
        self.assertEqual(set(certify.RUNNERS), set(certify.CASE_IDS))
        self.assertEqual(set(cf.EXPECTED), set(certify.CASE_IDS))
        self.assertTrue({name for _, _, counts in REGISTRY for name in counts} <= set(certify.COUNTERS))

    def test_every_count_is_set(self):
        self.assertTrue(all(type(value) is int for _, _, counts in certify.CASES for value in counts.values()))

    def test_a_registry_mismatch_refuses_before_any_case_runs(self):
        for name, target, value in (
                ("an extra runner", "RUNNERS", dict(certify.RUNNERS, extra=certify.RUNNERS["kat"])),
                ("a missing runner", "RUNNERS", {k: v for k, v in certify.RUNNERS.items() if k != "kat"}),
                ("a repeated id", "CASE_IDS", certify.CASE_IDS + ("kat",)),
                ("an extra constant", "EXPECTED", dict(cf.EXPECTED, extra={}))):
            with self.subTest(case=name), mock.patch.object(certify, target, value), \
                    self.assertRaisesRegex(certify.CaseProblem, "registry_mismatch"):
                certify.run_cases("/nonexistent-never-written")

    def test_the_kat_case_set_is_the_runners(self):
        self.assertEqual(cf.KAT_CASE_IDS, list(runner.KAT_CASE_IDS))
        self.assertEqual(cf.EXPECTED["kat"]["case_ids"], [case["id"] for case in engineering.KAT_CASES])

    def test_the_answer_tables_are_consistent(self):
        full, second = cf.EXPECTED["scenario_full"]["markets"], cf.EXPECTED["second_point"]["markets"]
        self.assertEqual(set(full), set(second))
        self.assertEqual(len(full), cf.EXPECTED["scenario_full"]["tally"]["records"])
        for market_id, market in full.items():
            with self.subTest(market=market_id):
                drawn = [row[0] for row in market["rows"] if row[1]["seeded_random_runner"][3] is True]
                issued = [row[0] for row in market["rows"] if row[1]["seeded_random_runner"][0] == "issued"]
                self.assertLessEqual(len(drawn), 1)
                self.assertTrue(set(issued) <= set(drawn))           # the seeded variant issues only its draw
                self.assertEqual(market["rows"] == [], market["population"] != "eligible")
        issued = sum(1 for market in full.values() for row in market["rows"]
                     for outcome in row[1].values() if outcome[0] == "issued")
        self.assertEqual(len(cf.EXPECTED["scenario_full"]["settlement"]), issued)

    def test_the_decision_ids_blocks_and_ledger_order_re_derive(self):
        for name, rules in (("scenario_full", cf.p0_rules()[0]), ("second_point", cf.second_point_rules()[0]),
                            ("market_conditions", cf.p0_rules()[0])):
            markets = cf.EXPECTED[name]["markets"]
            for market_id, market in markets.items():
                anchor_pt, anchor_ms, decision_ms = market["timing"]
                for row in market["rows"]:
                    with self.subTest(point=name, market=market_id, selection=row[0]):
                        self.assertEqual(row[3], decision_id(rules.contract_sha256, rules.axis, market_id, row[0]))
                        self.assertEqual(row[4], [rules.axis, decision_ms, anchor_pt, anchor_ms])
                        self.assertEqual(row[2] is None, row[0] is None)
                        # each variant's intent, on its own row: BACK at BSP, the point's stake, at this market's t
                        self.assertEqual(row[5], {variant: {"side": "BACK", "price": "BSP",
                                                            "stake_pence": rules.stake_pence,
                                                            "effective_ms": decision_ms + 1000 * rules.delay_s}
                                                  if outcome[0] == "issued" else None
                                                  for variant, outcome in row[1].items()})
            if "ledger" in cf.EXPECTED[name]:
                # every market with rows has one anchor (OFF), so the ledger is market id order, then selection order
                self.assertEqual(len({tuple(m["timing"][:2]) for m in markets.values() if m["rows"]}), 1)
                self.assertEqual(cf.EXPECTED[name]["ledger"], [[market_id, row[0]] for market_id in sorted(markets)
                                                               for row in markets[market_id]["rows"]])

    def test_the_seeded_draws_re_derive_from_the_recipe(self):
        for label, seed, draws in (("p0", 1, cf.SEEDED_DRAWS), ("second point", 7, cf.SEEDED_DRAWS_SECOND_POINT),
                                   ("conditions", 1, cf.SEEDED_DRAWS_CONDITIONS)):
            for market_id, (runners, drawn) in draws.items():
                with self.subTest(point=label, market=market_id):
                    self.assertEqual(sorted(runners)[draw(seed, f"selection/v1/{market_id}", len(runners))], drawn)


def _shifted_intents(real, effective_ms):
    """cohort._row with the issued intents of the markets in effective_ms {market id: ms} given that time instead."""
    def row(rules, market_id, plan, timing, selection_id, ltp, outcomes):
        made = real(rules, market_id, plan, timing, selection_id, ltp, outcomes)
        if market_id not in effective_ms:
            return made
        return dict(made, variants={variant: dict(value, intent=dict(value["intent"],
                                                                     effective_ms=effective_ms[market_id]))
                                    if value["intent"] is not None else dict(value)
                                    for variant, value in made["variants"].items()})
    return row


def _leaky_replay(real):
    """The run's view read three minutes past t and relabelled t (1.400000008's post-t segment is left alone: the
    replay's own integrity check would refuse it), so the futures the forms change now decide."""
    def leaky(root, manifest, record, offset_s, limits):
        if record["market_id"] == "1.400000008":
            return real(root, manifest, record, offset_s, limits)
        view = real(root, manifest, record, offset_s + 180, limits)
        return dataclasses.replace(view, decision_ms=view.decision_ms - 180000)
    return leaky


class CaseTests(Workspace):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        work = Path(tmp.name).resolve() / "work"
        work.mkdir()
        cls.results = {result["id"]: result for result in certify.run_cases(work)}

    def test_every_case_is_as_expected(self):
        self.assertEqual(list(self.results), list(certify.CASE_IDS))
        for case_id, result in self.results.items():
            with self.subTest(case=case_id):
                self.assertEqual(result["result"], certify.AS_EXPECTED, json.dumps(result, default=str)[:2000])

    def run_case(self, case_id):
        return certify.run_case(case_id, self.tmp / f"{case_id}-{len(list(self.tmp.iterdir()))}")

    def test_an_engine_changed_where_a_case_looks_is_caught(self):
        real_capture, real_add = engineering._capture, engineering.PrefixTally.add
        real_condition, not_open = cohort._market_condition, (cohort.ABSTAINED, "market_not_open_pre_off", None)

        def lax_add(tally, result, decision):            # a refused addition still records its key
            try:
                real_add(tally, result, decision)
            except ValueError:
                tally._digests[(result["plan"], result["market_id"])] = b"refused"
                raise

        def leaky_add(tally, result, decision):          # a refused addition still changes a count
            try:
                real_add(tally, result, decision)
            except ValueError:
                tally.counts["candidates"] += 1
                raise

        def never_failed(material, record, result, decision):
            return {"status": "passed", "failures": [], "counts_reconcile": True, "counts": {}}

        leak = _leaky_replay(runner.replay_view)
        changes = (
            ("kat", "no commission", [mock.patch.object(accounting, "commission_pence", return_value=0)]),
            ("scenario_full", "the favourite variant never bets", [mock.patch.object(
                cohort, "_favourite", lambda rules, t, runners, unsupported: {
                    item.selection_id: cohort._outcome(cohort.NOT_SELECTED) for item in runners})]),
            ("scenario_cut", "the view reads past t", [mock.patch.object(runner, "replay_view", leak)]),
            ("second_point", "one market's intents a minute late", [mock.patch.object(
                cohort, "_row", _shifted_intents(cohort._row, {"1.400000001": cf.ms(cf.OFF - timedelta(minutes=9))}))]),
            ("scenario_revised", "the view reads past t", [mock.patch.object(runner, "replay_view", leak)]),
            ("settlement_witness", "commission always rounded up", [mock.patch.dict(
                accounting.COMMISSION_ROUNDING, {"per_market_half_up_penny": ROUND_CEILING})]),
            ("market_conditions", "a market not open is never abstained", [mock.patch.object(
                cohort, "_market_condition", lambda view: None if real_condition(view) == not_open
                else real_condition(view))]),
            ("membership_gates", "the month gate refuses nothing", [mock.patch.object(
                archive, "month_gate", lambda month: None)]),
            ("limit_small_over", "the prefix limit is never reached", [mock.patch.object(
                engineering, "_capture", lambda raw, member, t, limits, limit:
                real_capture(raw, member, t, limits, 1 << 30))]),
            ("limit_batch_inside", "a line may pass the limit", [mock.patch.object(
                engineering, "_capture", lambda raw, member, t, limits, limit:
                real_capture(raw, member, t, limits, limit + 96))]),
            ("nc_pre_t_change", "a prefix tally that never fails", [mock.patch.object(
                certify, "_tallied", never_failed)]),
            ("nc_wrong_decision_time", "a prefix tally that never fails", [mock.patch.object(
                certify, "_tallied", never_failed)]),
            ("nc_same_key_substitution", "the result binding tells no decisions apart", [mock.patch.object(
                engineering, "decision_sha256", lambda decision: "0" * 64)]),
            ("nc_same_key_substitution", "a refused addition records its key", [mock.patch.object(
                engineering.PrefixTally, "add", lax_add)]),
            ("nc_rejected_tally_addition", "a refused addition changes a count", [mock.patch.object(
                engineering.PrefixTally, "add", leaky_add)]),
        )
        for case_id, change, patches in changes:
            with self.subTest(case=case_id, change=change), contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                result = self.run_case(case_id)
                self.assertEqual(result["result"], certify.NOT_AS_EXPECTED, json.dumps(result, default=str)[:1500])
                if case_id in ("scenario_cut", "scenario_revised"):
                    self.assertEqual((result["observed"]["same_decisions"], result["observed"]["same_ledger"]),
                                     (False, False))
                if case_id == "second_point":                # the reconciliation sees the late intents too
                    self.assertEqual(result["observed"]["reconciliation_status"], "failed")

    def test_a_seeded_row_without_its_drawn_flag_is_an_error_never_a_pass(self):
        """The cohort docstring gives every seeded outcome a drawn flag (null when no draw is made): an engine that
        leaves it out, where a null would have matched, must not pass."""
        real = runner.decide_market

        def without_null_drawn(rules, record, replay):
            decision = real(rules, record, replay)
            rows = tuple(dict(row, variants=dict(row["variants"], **{cohort.SEEDED: {
                key: value for key, value in row["variants"][cohort.SEEDED].items()
                if not (key == "drawn" and value is None)}})) for row in decision.rows)
            return dataclasses.replace(decision, rows=rows)

        with mock.patch.object(runner, "decide_market", without_null_drawn):
            result = self.run_case("market_conditions")
        self.assertEqual(result["result"], certify.ERROR, json.dumps(result, default=str)[:500])
        self.assertTrue(result["observed"].startswith("KeyError:"))

    def test_intents_swapped_between_two_markets_are_caught(self):
        """Astra's review probe (report_004c9dfa): the two issuing conditions markets' intent times exchanged. The set
        of all intents is unchanged, so only the per-row intents and the reconciliation can see it; a case that
        never reaches those markets is untouched."""
        swap = _shifted_intents(cohort._row, {"1.470000020": cf.ms(cf.T),
                                              "1.470000023": cf.ms(cf.APRIL_LAST - timedelta(minutes=5))})
        with mock.patch.object(cohort, "_row", swap):
            swapped, untouched = self.run_case("market_conditions"), self.run_case("scenario_full")
        self.assertEqual(swapped["result"], certify.NOT_AS_EXPECTED)
        self.assertEqual(swapped["observed"]["intents"], cf.EXPECTED["market_conditions"]["intents"])
        self.assertEqual(swapped["observed"]["reconciliation_status"], "failed")
        self.assertEqual(untouched["result"], certify.AS_EXPECTED)

    def test_a_reconciliation_that_never_fails_is_caught_by_every_reconciliation_control(self):
        def always_passed(rules, records, decisions, ledger_bytes, bets, settlement):
            return {"status": "passed", "problems": [], "unexercised_variants": [], "by_variant": {}}
        with mock.patch.object(certify, "issuance_exposure_reconciliation", always_passed):
            for case_id in RECONCILIATION_CONTROLS:
                with self.subTest(case=case_id):
                    self.assertEqual(self.run_case(case_id)["result"], certify.NOT_AS_EXPECTED)

    def test_an_exception_is_an_error_never_a_pass(self):
        for case_id in ("kat", "nc_markets_changed", "unexercised_incomplete"):
            with self.subTest(case=case_id), mock.patch.dict(certify.RUNNERS, {case_id: mock.Mock(
                    side_effect=RuntimeError("boom"))}):
                result = self.run_case(case_id)
                self.assertEqual((result["result"], result["observed"]), (certify.ERROR, "RuntimeError:boom"))

    def test_a_case_whose_counts_differ_is_not_as_expected(self):
        observed = dict(cf.EXPECTED["kat"])
        with mock.patch.dict(certify.RUNNERS, {"kat": lambda directory: observed}):
            self.assertEqual(self.run_case("kat")["result"], certify.AS_EXPECTED)
            for count in (12, 14):
                with self.subTest(count=count), mock.patch.object(certify, "CASES", tuple(
                        (i, k, {"kat_cases": count} if i == "kat" else c) for i, k, c in certify.CASES)):
                    self.assertEqual(self.run_case("kat")["result"], certify.NOT_AS_EXPECTED)

    def test_a_control_that_shows_no_failure_or_no_incompleteness_is_not_as_expected(self):
        for case_id, quiet in (("nc_markets_changed", {"refused": None}),
                               ("nc_ledger_byte", {"status": "passed", "problems": []}),
                               ("unexercised_incomplete", {"tally_status": "passed",
                                                           "reconciliation_status": "incomplete"})):
            with self.subTest(case=case_id), mock.patch.dict(certify.RUNNERS, {case_id: lambda directory: quiet}), \
                    mock.patch.dict(cf.EXPECTED, {case_id: quiet}):
                self.assertEqual(self.run_case(case_id)["result"], certify.NOT_AS_EXPECTED)

    def test_a_case_directory_is_never_reused(self):
        (self.tmp / "kat-reused").mkdir()
        with self.assertRaises(FileExistsError):
            certify.run_case("kat", self.tmp / "kat-reused")


if __name__ == "__main__":
    unittest.main()
