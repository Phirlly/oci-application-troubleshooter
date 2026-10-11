from base64 import b64decode
from dataclasses import replace
from io import BytesIO
import json
import unittest
from zipfile import ZipFile

from troubleshooter_deployment.cleanup import prepare_purge
from troubleshooter_deployment.codec import RecordError, decode, encode, encode_receipt, validate_identity
from troubleshooter_deployment.journal import Journal
from troubleshooter_deployment.policy import bootstrap, begin_generation, reserve
from troubleshooter_deployment.records import Record, StackIdentity, Resource, Generation
from troubleshooter_deployment.stack_journal_config import declaration_zip
from troubleshooter_deployment.stack_journal_variables import (
    KEYS, PREFIX, MAX_JOURNAL_BYTES, StackJournalVariablesError, merge_journal, read_journal,
)
from tests.deployment.fixtures import ATTEMPT, NEXT_ATTEMPT, TERMINAL, IDENTITY, GENERATION, OPERATION, REMOVAL, SHA, MemoryStore

STACK_IDENTITY = StackIdentity(IDENTITY.scope, "synthetic-stack", "6" * 32)


def large_record():
    resources = tuple(Resource(f"resource{i:04d}" + "x" * 244, "secret", "deleted") for i in range(256))
    retired = Generation(GENERATION, SHA, "workload_removed", REMOVAL, True, resources)
    return replace(bootstrap(STACK_IDENTITY, STACK_IDENTITY, ATTEMPT), generations=(retired,))


class StackJournalTests(unittest.TestCase):
    def test_explicit_schema_roundtrip_takeover_and_uncertain_history(self):
        store = MemoryStore()
        first = Journal(store, STACK_IDENTITY, ATTEMPT)
        saved = first.initialize(STACK_IDENTITY)
        self.assertEqual(saved.record.schema, 2)
        saved = first.commit(saved, begin_generation(saved.record, GENERATION, SHA))
        saved, decision = first.reserve(saved, GENERATION, OPERATION, "provision")
        self.assertEqual(decision, "reserved")
        next_runner = Journal(store, STACK_IDENTITY, NEXT_ATTEMPT)
        taken = next_runner.takeover(next_runner.load(), TERMINAL)
        self.assertEqual(taken.record.operations, saved.record.operations)
        self.assertEqual(taken.record.owner, NEXT_ATTEMPT)
        self.assertEqual(decode(encode(taken.record)), taken.record)

    def test_schema_types_and_bucket_authority_stay_separate(self):
        legacy = bootstrap(IDENTITY, IDENTITY, ATTEMPT)
        self.assertEqual(legacy.schema, 1)
        self.assertIn(b'"bucket_id":', encode(legacy))
        for record in (Record(STACK_IDENTITY, ATTEMPT), replace(legacy, schema=2)):
            with self.assertRaises(RecordError):
                encode(record)
        for identity in (STACK_IDENTITY, replace(STACK_IDENTITY, epoch="bad")):
            with self.assertRaises(RecordError):
                validate_identity(identity)
        good = bootstrap(STACK_IDENTITY, STACK_IDENTITY, ATTEMPT)
        for identity in (replace(STACK_IDENTITY, stack_id="bad/id"), replace(STACK_IDENTITY, epoch="bad")):
            with self.assertRaises(RecordError):
                encode(replace(good, identity=identity))
        closed = prepare_purge(good, REMOVAL)
        self.assertEqual(decode(encode(closed)), closed)
        with self.assertRaises(RecordError):
            encode_receipt(closed.receipt)

    def test_carrier_preserves_unrelated_values_and_all_chunks(self):
        record = bootstrap(STACK_IDENTITY, STACK_IDENTITY, ATTEMPT)
        original = {"region": "us-ashburn-1", "label": "日本語"}
        self.assertIsNone(read_journal(original))
        merged = merge_journal(original, encode(record))
        self.assertEqual(read_journal(merged), encode(record))
        self.assertEqual({key: merged[key] for key in original}, original)
        self.assertEqual(set(merged) - set(original), KEYS)
        self.assertEqual(merged[PREFIX + "21"], "")
        updated = encode(begin_generation(record, GENERATION, SHA))
        self.assertEqual(read_journal(merge_journal(merged, updated)), updated)

    def test_partial_corrupt_unknown_and_noncanonical_carriers_fail(self):
        body = encode(bootstrap(STACK_IDENTITY, STACK_IDENTITY, ATTEMPT))
        good = merge_journal({}, body)
        cases = [{k: v for k, v in good.items() if k != missing} for missing in good]
        cases += [{**good, PREFIX + "unexpected": ""}, {**good, PREFIX + "21": "x"},
                  {**good, PREFIX + "manifest": "{}"}, {**good, PREFIX + "0": "é"}]
        for values in cases:
            with self.assertRaises(StackJournalVariablesError):
                read_journal(values)
            with self.assertRaises(StackJournalVariablesError):
                merge_journal(values, body)
        for invalid in (b"{}", body + b" ", b"x" * (MAX_JOURNAL_BYTES + 1), "text",
                        encode(bootstrap(IDENTITY, IDENTITY, ATTEMPT))):
            with self.assertRaises(StackJournalVariablesError):
                merge_journal({}, invalid)

    def test_large_payload_and_provider_limits(self):
        record = large_record()
        body = encode(record)
        self.assertGreater(len(body), 80000)
        self.assertEqual(read_journal(merge_journal({}, body)), body)
        too_large = replace(record, generations=(record.generations[0], replace(
            record.generations[0], generation_id="7" * 32,
            resources=tuple(replace(r, resource_id="z" + r.resource_id[1:]) for r in record.generations[0].resources))))
        self.assertGreater(len(encode(too_large)), MAX_JOURNAL_BYTES)
        with self.assertRaises(StackJournalVariablesError):
            merge_journal({}, encode(too_large))
        allowed = {f"v{i}": "" for i in range(250 - len(KEYS))}
        self.assertEqual(len(merge_journal(allowed, body)), 250)
        for values in ({**allowed, "overflow": ""}, {"x": "é" * 4096}, {"x": "\ud800"},
                       None, [], {"": ""}, {1: ""}, {"v": True}):
            with self.assertRaises(StackJournalVariablesError):
                merge_journal(values, body)

    def test_declarations_are_deterministic_resource_free_root_zip(self):
        body = declaration_zip()
        self.assertEqual(body, declaration_zip())
        with ZipFile(BytesIO(b64decode(body))) as archive:
            self.assertEqual(archive.namelist(), ["main.tf"])
            source = archive.read("main.tf").decode()
        for key in KEYS | {"troubleshooter_initial_stack_v1"}:
            self.assertIn(f'variable "{key}"', source)
        self.assertNotIn("resource ", source)
        self.assertNotIn("provider ", source)
