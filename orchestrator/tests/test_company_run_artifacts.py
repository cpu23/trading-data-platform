"""Company-benchmark artifact coverage: complete-run artifact lifecycle,
manifest validation, tamper detection, repair chains, executor and judge
identities, trusted request rebuilding, and numeric ledger artifact round-trip.
"""

import dataclasses
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import investment_service as service  # noqa: E402
from company_benchmark_support import (  # noqa: E402
    BAD_FIRST_PASS,
    EXCERPT,
    EXECUTOR_IDENTITY,
    OTHER_EXECUTOR_IDENTITY,
    REPAIR_PROMPT,
    _finalized_for,
    _judge_round_for,
    evaluator_raw,
    judge_payload,
    narrative_payload,
    narrative_payload_for_request,
    other_producer_raw,
    producer_raw,
    relationship_producer_raw,
    write_yaml,
)
from research_intelligence import company_artifacts as artifacts  # noqa: E402
from research_intelligence import company_benchmarks as cb  # noqa: E402
from research_intelligence import company_judging as judging  # noqa: E402
from research_intelligence import company_quality as cq  # noqa: E402
from research_intelligence.contracts import canonical_fingerprint  # noqa: E402


class CompanyRunArtifactTests(unittest.TestCase):
    """The clean complete-run contract, exercised through real seams.

    One coherent fixture walks the full benchmark lifecycle — producer case,
    dispatch request, a one-repair recorded attempt chain, finalization,
    three verbatim raw judge responses with independent execution
    identities, and the persisted blind salt. The writer rebuilds requests,
    hard gates, and panel aggregation from those trusted inputs alone, so a
    published run is reproducible from immutable bytes. Cross-component
    mixing, omission, tampering, rubric/output drift with reforged judge
    material, shared judge identities, fingerprint substitution, and any
    manifest identity deviation must all fail closed before the destination
    exists.
    """

    BLIND_SALT = "company-run-blind-salt"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)

    # -- fixture assembly through production seams ------------------------

    def _load_case(self, raw):
        return cb.load_producer_case(write_yaml(self.directory, "producer.yaml", raw))

    def _load_evaluator(self, producer, **overrides):
        # Every constructed run must be internally coherent: the evaluator
        # half names its OWN producer case, so the pairing case_id derives
        # from the loaded case (matching the lowercase fixture convention)
        # instead of leaking the first case's id into the second run.
        overrides.setdefault("case_id", producer.case_id.lower())
        return cb.load_evaluator_case(
            write_yaml(
                self.directory,
                "evaluator.yaml",
                evaluator_raw(producer.fingerprint, **overrides),
            ),
            producer=producer,
        )

    def _attempt(
        self, content, *, identity, accepted=True, repair_prompt=None, index=0
    ):
        provenance = dict(identity)
        if accepted:
            provenance["model"] = "recorded-model-final"
            provenance["tokens_total"] = 480
        else:
            provenance["model"] = "recorded-model-first-pass"
            provenance["tokens_total"] = 91
        return artifacts.RecordedAttempt(
            index=index,
            content=content,
            accepted=accepted,
            repair_prompt=repair_prompt,
            provenance=provenance,
        )

    def _judge_records(self, producer, evaluator, finalized, blind_salt=None):
        """Three raw judge responses with distinct independent identities."""
        requests = judging.build_blind_judge_requests(
            producer,
            evaluator,
            finalized,
            self.BLIND_SALT if blind_salt is None else blind_salt,
        )
        return [
            {
                "role": request.role,
                "token": request.token,
                "raw_json": json.dumps(judge_payload(request)),
                "execution_id": f"judge-exec-{index}",
                "session_id": f"judge-session-{index}",
                "provenance": {},
            }
            for index, request in enumerate(requests)
        ]

    def _run(
        self,
        raw=None,
        identity=EXECUTOR_IDENTITY,
        *,
        blind_salt=None,
        **evaluator_overrides,
    ):
        """Build every component of one coherent run from real seams."""
        salt = self.BLIND_SALT if blind_salt is None else blind_salt
        case = self._load_case(producer_raw() if raw is None else raw)
        request = cb.prepare_company_run(case)
        attempts = [
            self._attempt(
                BAD_FIRST_PASS,
                identity=identity,
                accepted=False,
                repair_prompt=REPAIR_PROMPT,
            ),
            self._attempt(
                json.dumps(
                    narrative_payload_for_request(
                        request,
                        alternate=raw is not None,
                    )
                ),
                identity=identity,
                accepted=True,
                index=1,
            ),
        ]
        accepted = attempts[-1]
        finalized = cb.finalize_recorded_company_run(
            cb.recorded_executor_output(accepted.content, {"model": "recorded-model"}),
            case,
        )
        evaluator = self._load_evaluator(case, **evaluator_overrides)
        stage_config = {
            "executor": dict(identity),
            "model": {"slug": "recorded-model", "temperature": 0},
            "retries": 1,
        }
        return {
            "producer": case,
            "request": request,
            "attempts": attempts,
            "finalized": finalized,
            "evaluator": evaluator,
            "blind_salt": salt,
            "judge_records": self._judge_records(
                case, evaluator, finalized, blind_salt=salt
            ),
            "git_commit": "0f1e2d3c4b5a",
            "git_dirty": False,
            "created_at": datetime(2026, 4, 1, tzinfo=UTC),
            "stage_config": stage_config,
        }

    SECOND_BLIND_SALT = "company-run-blind-salt-second-run"

    def _second_run(self):
        """A fully independent second case/run for cross-mixing checks.

        Distinct case, executor identity, AND salt: substituting any one
        of them — including ``blind_salt`` — into the first run must
        change the derived judge material and be rejected.
        """
        other_raw = other_producer_raw()
        return self._run(
            raw=other_raw,
            identity=OTHER_EXECUTOR_IDENTITY,
            blind_salt=self.SECOND_BLIND_SALT,
        )

    def _write(self, output_dir, components=None, **overrides):
        arguments = components if components is not None else self._run()
        arguments = dict(arguments)
        arguments.update(overrides)
        return artifacts.write_immutable_company_run(output_dir, **arguments)

    @staticmethod
    def _read(root, name):
        return json.loads((root / name).read_text(encoding="utf-8"))

    def _reforge_payload(self, root, name, payload):
        """Replace one payload while keeping every byte-level seal coherent."""
        blob = artifacts._canonical_json(payload)
        (root / name).write_bytes(blob)
        manifest = self._read(root, artifacts.MANIFEST_NAME)
        manifest["files"][name] = {
            "sha256": hashlib.sha256(blob).hexdigest(),
            "bytes": len(blob),
        }
        manifest["run_identity_digest"] = artifacts._run_identity_digest(
            {
                key: value
                for key, value in manifest.items()
                if key != "run_identity_digest"
            }
        )
        (root / artifacts.MANIFEST_NAME).write_bytes(
            artifacts._canonical_json(manifest)
        )
        published = self._read(root, artifacts.MANIFEST_NAME)
        self.assertEqual(
            published["files"][name],
            {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)},
        )
        self.assertEqual(
            published["run_identity_digest"],
            artifacts._run_identity_digest(
                {
                    key: value
                    for key, value in published.items()
                    if key != "run_identity_digest"
                }
            ),
        )

    # -- the coherent happy path ------------------------------------------

    def test_complete_run_writes_exact_files_and_self_verifies(self):
        root = self._write(self.directory / "run")
        names = {entry.name for entry in root.iterdir()}
        expected_payloads = {
            "manifest.json",
            "producer.json",
            "request.json",
            "attempts.json",
            "finalized_output.json",
            "hard_gates.json",
            "judge_requests.json",
            "judge_results.json",
            "evaluator.json",
            "panel_report.json",
            "defect_log.json",
            "stage_config.json",
            "blind_salt.json",
        }
        self.assertEqual(names, expected_payloads)
        self.assertEqual(names, artifacts._RUN_FILES)
        self.assertTrue(artifacts.is_complete_company_run(root))
        # Manifest-last semantics are observable: without the manifest the
        # directory is an incomplete run even though all payloads exist.
        (root / artifacts.MANIFEST_NAME).unlink()
        self.assertEqual(
            {entry.name for entry in root.iterdir()}, artifacts._PAYLOAD_FILES
        )
        self.assertFalse(artifacts.is_complete_company_run(root))

    def test_manifest_records_digests_identities_and_stage_versions(self):
        components = self._run()
        root = self._write(self.directory / "run", components)
        manifest = self._read(root, artifacts.MANIFEST_NAME)
        self.assertEqual(manifest["schema_version"], artifacts.ARTIFACT_SCHEMA_VERSION)
        self.assertEqual(manifest["artifact_kind"], artifacts.ARTIFACT_KIND)
        for name in sorted(artifacts._PAYLOAD_FILES):
            blob = (root / name).read_bytes()
            spec = manifest["files"][name]
            self.assertEqual(spec["sha256"], hashlib.sha256(blob).hexdigest())
            self.assertEqual(spec["bytes"], len(blob))
        self.assertEqual(manifest["case_id"], components["producer"].case_id)
        self.assertEqual(
            manifest["fixture_version"], components["producer"].fixture_version
        )
        self.assertEqual(
            manifest["producer_fingerprint"], components["producer"].fingerprint
        )
        # The blind salt itself never lands in the artifact; only its
        # commitment does — a DOMAIN-SEPARATED digest over the exact salt
        # bytes, not a bare sha256(salt). Byte-only replay must still be
        # possible: the post-judging salt disclosure file carries the salt,
        # and the verifier recomputes this commitment from it.
        self.assertNotIn(
            self.BLIND_SALT.encode("utf-8"), (root / "manifest.json").read_bytes()
        )
        self.assertEqual(
            manifest["blind_salt_commitment"],
            artifacts._salt_commitment(self.BLIND_SALT.encode("utf-8")),
        )
        self.assertEqual(
            manifest["blind_salt_commitment"],
            hashlib.sha256(
                b"research_intelligence/company_run/blind_salt\x00"
                + self.BLIND_SALT.encode("utf-8")
            ).hexdigest(),
        )
        disclosed = json.loads((root / "blind_salt.json").read_text(encoding="utf-8"))
        self.assertEqual(disclosed, {"salt_hex": self.BLIND_SALT.encode("utf-8").hex()})
        disclosed_salt = bytes.fromhex(disclosed["salt_hex"])
        self.assertEqual(
            artifacts._salt_commitment(disclosed_salt),
            manifest["blind_salt_commitment"],
        )
        # The raw salt string must not appear anywhere, even hex-encoded in
        # the disclosure file; the disclosure is for post-judging replay.
        self.assertNotIn(
            self.BLIND_SALT.encode("utf-8"), (root / "blind_salt.json").read_bytes()
        )
        rebuilt = judging.build_blind_judge_requests(
            components["producer"],
            components["evaluator"],
            components["finalized"],
            self.BLIND_SALT,
        )
        self.assertEqual(len(manifest["judge_request_fingerprints"]), 3)
        self.assertEqual(
            manifest["judge_request_fingerprints"],
            [request.fingerprint for request in rebuilt],
        )
        self.assertEqual(
            manifest["executor_recorded_provenance"],
            dict(components["attempts"][-1].provenance),
        )
        self.assertEqual(manifest["git_commit"], "0f1e2d3c4b5a")
        self.assertFalse(manifest["git_dirty"])
        self.assertEqual(
            manifest["created_at"], datetime(2026, 4, 1, tzinfo=UTC).isoformat()
        )
        self.assertEqual(manifest["executor"], EXECUTOR_IDENTITY)
        self.assertEqual(
            manifest["prompt_stage"]["prompt_version"], judging.PROMPT_VERSION
        )
        self.assertEqual(manifest["prompt_stage"]["schema_name"], judging.SCHEMA_NAME)

    def test_stored_components_match_their_sources_exactly(self):
        components = self._run()
        root = self._write(self.directory / "run", components)
        manifest = self._read(root, artifacts.MANIFEST_NAME)
        producer_blob = self._read(root, "producer.json")
        self.assertEqual(
            producer_blob["fingerprint"], components["producer"].fingerprint
        )
        self.assertEqual(producer_blob["excerpt"], EXCERPT)
        request_blob = self._read(root, "request.json")
        self.assertEqual(request_blob["fingerprint"], components["request"].fingerprint)
        self.assertEqual(
            request_blob["schema_name"],
            "investment_report_narrative_v7",
        )
        expected_request_blob = json.loads(
            artifacts._canonical_json(artifacts._request_payload(components["request"]))
        )
        self.assertEqual(
            request_blob["relationship_facts"],
            expected_request_blob["relationship_facts"],
        )
        self.assertEqual(
            request_blob["material_relationships"],
            expected_request_blob["material_relationships"],
        )
        self.assertIn(EXCERPT, request_blob["prompt"])
        finalized_blob = self._read(root, "finalized_output.json")
        self.assertEqual(
            finalized_blob["facts"]["relationship_facts"],
            request_blob["relationship_facts"],
        )
        self.assertEqual(
            finalized_blob["facts"]["material_relationships"],
            request_blob["material_relationships"],
        )
        self.assertEqual(
            finalized_blob["facts"]["relationship_reconciliations"],
            finalized_blob["analysis"]["relationship_reconciliations"],
        )
        attempts_blob = self._read(root, "attempts.json")["attempts"]
        self.assertEqual([entry["index"] for entry in attempts_blob], [0, 1])
        self.assertEqual([entry["accepted"] for entry in attempts_blob], [False, True])
        self.assertIsNotNone(attempts_blob[0]["repair_prompt"])
        self.assertIsNone(attempts_blob[1]["repair_prompt"])
        self.assertEqual(attempts_blob[1]["content"], json.dumps(narrative_payload()))
        self.assertEqual(
            attempts_blob[0]["provenance"]["execution_id"],
            EXECUTOR_IDENTITY["execution_id"],
        )
        gate_blob = self._read(root, "hard_gates.json")
        recomputed_gate = cq.run_company_hard_gates(
            components["producer"], components["evaluator"], components["finalized"]
        )
        self.assertTrue(gate_blob["passed"])
        self.assertTrue(recomputed_gate.passed)
        judge_requests_blob = self._read(root, "judge_requests.json")["requests"]
        rebuilt_requests = judging.build_blind_judge_requests(
            components["producer"],
            components["evaluator"],
            components["finalized"],
            self.BLIND_SALT,
        )
        self.assertEqual(
            [entry["role"] for entry in judge_requests_blob],
            list(judging.JUDGE_ROLES),
        )
        for request, stored in zip(rebuilt_requests, judge_requests_blob, strict=True):
            # The stored prompt is the exact recomputed dispatch text; the
            # canonical digest covers it and is never echoed inside it.
            self.assertEqual(stored["prompt"], request.prompt)
            self.assertEqual(stored["fingerprint"], request.fingerprint)
            self.assertNotIn(stored["fingerprint"], stored["prompt"])
            self.assertIn(stored["response_binding"], stored["prompt"])
        self.assertEqual(
            {entry["role"] for entry in judge_requests_blob}, set(judging.JUDGE_ROLES)
        )
        judge_results_blob = self._read(root, "judge_results.json")["results"]
        self.assertEqual(len(judge_results_blob), 3)
        for request, record, stored in zip(
            rebuilt_requests,
            components["judge_records"],
            judge_results_blob,
            strict=True,
        ):
            parsed = judging.parse_judge_result(request, record["raw_json"])
            self.assertIn(parsed.role, judging.JUDGE_ROLES)
            # The canonical request digest persists on the manifest; the
            # stored result carries its response binding instead.
            self.assertIn(request.fingerprint, manifest["judge_request_fingerprints"])
            self.assertEqual(stored["role"], parsed.role)
            self.assertEqual(stored["response_binding"], parsed.response_binding)
            self.assertNotIn("fingerprint", stored)
            # Verbatim raw bytes plus the per-judge execution identity are
            # stored beside the parsed verdict.
            self.assertEqual(stored["raw_response"], record["raw_json"])
            self.assertEqual(stored["execution_id"], record["execution_id"])
            self.assertEqual(stored["session_id"], record["session_id"])
            self.assertEqual(stored["judge_provenance"], record["provenance"])
        panel_blob = self._read(root, "panel_report.json")
        self.assertTrue(panel_blob["passed"])
        defects_blob = self._read(root, "defect_log.json")
        total_defects = sum(
            len(judging.parse_judge_result(r, rec["raw_json"]).concrete_defects)
            for r, rec in zip(
                rebuilt_requests, components["judge_records"], strict=True
            )
        )
        self.assertEqual(len(defects_blob["judge_defects"]), total_defects)
        stage_blob = self._read(root, "stage_config.json")
        self.assertEqual(stage_blob["executor"], EXECUTOR_IDENTITY)

    def test_existing_output_path_fails_without_touching_bytes(self):
        first = self._write(self.directory / "run")
        before = {path.name: path.read_bytes() for path in sorted(first.iterdir())}
        with self.assertRaises(FileExistsError):
            self._write(first)
        after = {path.name: path.read_bytes() for path in sorted(first.iterdir())}
        self.assertEqual(before, after)

    def test_tampering_breaks_completeness(self):
        root = self._write(self.directory / "run")
        payload_path = root / "stage_config.json"
        original_blob = payload_path.read_bytes()
        payload_path.write_bytes(
            original_blob.replace(b"recorded-model", b"other-model")
        )
        self.assertFalse(artifacts.is_complete_company_run(root))
        payload_path.write_bytes(original_blob)
        extra = root / "extra.json"
        extra.write_text("{}", encoding="utf-8")
        self.assertFalse(artifacts.is_complete_company_run(root))

    def test_every_required_component_omission_is_rejected_before_creation(self):
        components = self._run()
        destination = self.directory / "omitted"
        for component in (
            "producer",
            "request",
            "attempts",
            "finalized",
            "evaluator",
            "blind_salt",
            "stage_config",
        ):
            with self.subTest(component=component):
                incomplete = dict(components)
                del incomplete[component]
                with self.assertRaises(TypeError):
                    self._write(destination, incomplete)
                self.assertFalse(destination.exists())

    def test_cross_mixing_any_component_from_second_run_is_rejected(self):
        first = self._run()
        second = self._second_run()
        for component in (
            "producer",
            "request",
            "attempts",
            "finalized",
            "evaluator",
            "blind_salt",
            "stage_config",
        ):
            mixed = dict(first)
            mixed[component] = second[component]
            # The salt swap is only meaningful if the substituted value
            # genuinely differs; a same-value "swap" proves nothing.
            self.assertNotEqual(
                first[component],
                second[component],
                f"second-run {component} must differ for the mix to be tested",
            )
            # A unique destination per component keeps subtests independent:
            # a rejected write never leaves a directory behind, and one
            # accidental success cannot contaminate the remaining subtests.
            with self.subTest(component=component):
                destination = self.directory / f"mixed-{component}"
                with self.assertRaises(ValueError):
                    self._write(destination, mixed)
                self.assertFalse(destination.exists())

    def test_invalid_metadata_fails_before_destination_creation(self):
        rejections = [
            {"git_commit": "ZZZZ9999"},
            {"created_at": datetime(2026, 4, 1)},
            {"stage_config": [("model", "luna")]},
        ]
        destination = self.directory / "rejected"
        components = self._run()
        for overrides in rejections:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    self._write(destination, components, **overrides)
                self.assertFalse(destination.exists())

    def test_one_repair_chain_is_recorded_in_order(self):
        components = self._run()
        root = self._write(self.directory / "run", components)
        attempts_blob = self._read(root, "attempts.json")["attempts"]
        self.assertEqual(len(attempts_blob), 2)
        first, second = attempts_blob
        self.assertFalse(first["accepted"])
        self.assertEqual(first["content"], BAD_FIRST_PASS)
        self.assertTrue(first["repair_prompt"])
        self.assertTrue(second["accepted"])
        self.assertIsNone(second["repair_prompt"])
        # The stored chain ends at the accepted attempt that produced the
        # finalized output; the rejected first pass never feeds finalization.
        finalized_blob = self._read(root, "finalized_output.json")
        self.assertNotEqual(finalized_blob.get("analysis"), BAD_FIRST_PASS)
        self.assertEqual(
            finalized_blob["analysis"]["summary"], narrative_payload()["summary"]
        )

    def test_replay_rejects_reforged_noncontiguous_attempt_indices(self):
        root = self._write(self.directory / "noncontiguous-attempts")
        payload = self._read(root, "attempts.json")
        payload["attempts"][0]["index"] = 7
        self._reforge_payload(root, "attempts.json", payload)
        self.assertFalse(artifacts.is_complete_company_run(root))

    def test_replay_rejects_reforged_accepted_attempt_before_chain_end(self):
        root = self._write(self.directory / "accepted-before-end")
        payload = self._read(root, "attempts.json")
        payload["attempts"][0]["accepted"] = True
        payload["attempts"][1]["accepted"] = False
        self._reforge_payload(root, "attempts.json", payload)
        self.assertFalse(artifacts.is_complete_company_run(root))

    def test_replay_rejects_reforged_broken_repair_successor(self):
        root = self._write(self.directory / "broken-repair-successor")
        payload = self._read(root, "attempts.json")
        payload["attempts"][0]["repair_prompt"] = None
        self._reforge_payload(root, "attempts.json", payload)
        self.assertFalse(artifacts.is_complete_company_run(root))

    def test_replay_rejects_reforged_extra_attempt_entry_fields(self):
        root = self._write(self.directory / "extra-attempt-entry")
        payload = self._read(root, "attempts.json")
        payload["attempts"][0]["untrusted_annotation"] = "accepted out of band"
        self._reforge_payload(root, "attempts.json", payload)
        self.assertFalse(artifacts.is_complete_company_run(root))

    def test_replay_rejects_reforged_extra_attempt_envelope_fields(self):
        root = self._write(self.directory / "extra-attempt-envelope")
        payload = self._read(root, "attempts.json")
        payload["untrusted_annotation"] = "accepted out of band"
        self._reforge_payload(root, "attempts.json", payload)
        self.assertFalse(artifacts.is_complete_company_run(root))

    def test_replay_rejects_reforged_transplanted_attempt_executor(self):
        root = self._write(self.directory / "transplanted-attempt-executor")
        payload = self._read(root, "attempts.json")
        payload["attempts"][0]["provenance"]["execution_id"] = OTHER_EXECUTOR_IDENTITY[
            "execution_id"
        ]
        self._reforge_payload(root, "attempts.json", payload)
        self.assertFalse(artifacts.is_complete_company_run(root))

    def test_replay_accepts_legacy_attempts_without_session_identity(self):
        root = self._write(self.directory / "legacy-no-attempt-session")
        payload = self._read(root, "attempts.json")
        self.assertTrue(
            all(
                "session_id" not in entry["provenance"] for entry in payload["attempts"]
            )
        )
        self.assertTrue(artifacts.is_complete_company_run(root))

    def test_successful_completeness_after_full_lifecycle(self):
        root = self._write(self.directory / "run")
        panel_blob = self._read(root, "panel_report.json")
        self.assertTrue(panel_blob["passed"])
        self.assertEqual(panel_blob["overall_median"], 5.0)
        gate_blob = self._read(root, "hard_gates.json")
        self.assertTrue(gate_blob["passed"])
        self.assertTrue(artifacts.is_complete_company_run(root))

    def test_blank_or_wrong_executor_identity_is_rejected(self):
        components = self._run()
        destination = self.directory / "identity"
        rejections = [
            {"executor_kind": ""},
            {"executor_kind": None},
            {"executor_kind": "human_operator"},
            {"execution_id": ""},
            {"execution_id": "   "},
            {"execution_id": None},
            {"executor_name": ""},
            {"executor_version": ""},
            {"extra_key": "not allowed"},
        ]
        for overrides in rejections:
            with self.subTest(overrides=overrides):
                stage_config = {
                    "executor": dict(EXECUTOR_IDENTITY),
                    "model": "recorded-model",
                }
                stage_config["executor"].update(overrides)
                with self.assertRaises(ValueError):
                    self._write(destination, components, stage_config=stage_config)
                self.assertFalse(destination.exists())

    def test_missing_or_empty_stage_config_executor_block_is_rejected(self):
        components = self._run()
        destination = self.directory / "stage"
        for stage_config in (
            {},
            {"model": "luna", "retries": 1},
            {"executor": {}},
            {"executor": {"executor_kind": "agent_environment"}},
        ):
            with self.subTest(stage_config=stage_config):
                with self.assertRaises(ValueError):
                    self._write(destination, components, stage_config=stage_config)
                self.assertFalse(destination.exists())

    def test_evaluator_leakage_into_payloads_is_rejected(self):
        components = self._run()
        evaluator_packet = evaluator_raw(components["producer"].fingerprint)
        destination = self.directory / "leak-object"
        with self.assertRaises(ValueError) as ctx:
            self._write(destination, {**components, "producer": evaluator_packet})
        self.assertIn("must be a ProducerCase", str(ctx.exception))
        self.assertFalse(destination.exists())
        poisoned_document = dict(
            producer_raw()["document"],
            extraction={"nested": {"known_traps": []}},
        )
        poisoned_raw = dict(producer_raw(), document=poisoned_document)
        with self.assertRaises(ValueError) as ctx:
            cb.load_producer_case(
                write_yaml(self.directory, "poison.yaml", poisoned_raw)
            )
        self.assertIn("evaluator-only field", str(ctx.exception))

    def _manifest_identity_keys(self, root):
        """Identity keys exactly as published, minus the digest map itself."""
        manifest = self._read(root, artifacts.MANIFEST_NAME)
        identity = dict(manifest)
        identity.pop("files")
        return sorted(identity)

    def test_manifest_identity_schema_is_exact_and_cross_checked(self):
        components = self._run()
        root = self._write(self.directory / "run", components)
        expected = self._manifest_identity_keys(root)
        # The manifest binds its own integrity: the domain-separated
        # run_identity_digest covers every other published field (including
        # the per-file digest map), so ANY isolated manifest mutation — an
        # altered or deleted blind_salt_commitment included — fails closed.
        self.assertIn("blind_salt_commitment", expected)
        self.assertIn("run_identity_digest", expected)

        published = self._read(root, artifacts.MANIFEST_NAME)
        # The expected digest is recomputed with the exact published
        # run-identity helper over EVERY published field (including the
        # per-file digest map and the blind-salt commitment), never a
        # hand-built subset with divergent encoding.
        self.assertEqual(
            published["run_identity_digest"],
            artifacts._run_identity_digest(
                {
                    key: value
                    for key, value in published.items()
                    if key != "run_identity_digest"
                }
            ),
        )
        for field in expected:
            for mode in ("deleted", "altered"):
                with self.subTest(field=field, mode=mode):
                    manifest = self._read(root, artifacts.MANIFEST_NAME)
                    if mode == "deleted":
                        del manifest[field]
                    else:
                        manifest[field] = (
                            "0" * 64
                            if isinstance(manifest.get(field), str)
                            and len(str(manifest.get(field))) == 64
                            else "tampered"
                        )
                    scratch = self.directory / f"mtmp-{field}-{mode}"
                    shutil.rmtree(scratch, ignore_errors=True)
                    scratch.mkdir()
                    for name in artifacts._PAYLOAD_FILES:
                        shutil.copy2(root / name, scratch / name)
                    (scratch / artifacts.MANIFEST_NAME).write_text(
                        json.dumps(manifest), encoding="utf-8"
                    )
                    self.assertFalse(artifacts.is_complete_company_run(scratch))

    def test_manifest_cross_checks_every_payload_against_its_identity(self):
        root = self._write(self.directory / "run")
        producer_blob = json.loads((root / "producer.json").read_bytes())
        request_blob = self._read(root, "request.json")
        stage_blob = self._read(root, "stage_config.json")
        judge_requests_blob = self._read(root, "judge_requests.json")["requests"]
        attempts_blob = self._read(root, "attempts.json")["attempts"]
        manifest = self._read(root, artifacts.MANIFEST_NAME)
        self.assertEqual(manifest["case_id"], producer_blob["case_id"])
        self.assertEqual(manifest["fixture_version"], producer_blob["fixture_version"])
        self.assertEqual(manifest["producer_fingerprint"], producer_blob["fingerprint"])
        self.assertEqual(manifest["request_fingerprint"], request_blob["fingerprint"])
        self.assertEqual(
            manifest["judge_request_fingerprints"],
            [entry["fingerprint"] for entry in judge_requests_blob],
        )
        self.assertEqual(manifest["executor"], stage_blob["executor"])
        self.assertEqual(
            manifest["executor_recorded_provenance"],
            attempts_blob[-1]["provenance"],
        )
        evaluator_manifest = manifest["files"]["evaluator.json"]
        self.assertEqual(
            evaluator_manifest["sha256"],
            hashlib.sha256((root / "evaluator.json").read_bytes()).hexdigest(),
        )

    def test_byte_identical_artifact_remains_complete(self):
        first = self._write(self.directory / "first")
        second = self._write(self.directory / "second")
        for name in sorted(artifacts._RUN_FILES):
            self.assertEqual(
                (first / name).read_bytes(),
                (second / name).read_bytes(),
                f"{name} must be deterministic",
            )
        self.assertTrue(artifacts.is_complete_company_run(first))
        self.assertTrue(artifacts.is_complete_company_run(second))
        # A byte-for-byte copy elsewhere is still a complete run.
        copy_root = self.directory / "copied"
        copy_root.mkdir()
        for name in sorted(artifacts._RUN_FILES):
            shutil.copy2(first / name, copy_root / name)
        self.assertTrue(artifacts.is_complete_company_run(copy_root))

    def test_evaluator_half_is_stored_digested_and_gates_replay_from_bytes(self):
        components = self._run()
        root = self._write(self.directory / "run", components)
        self.assertIn(
            "evaluator.json",
            {entry.name for entry in root.iterdir()},
        )
        stored = self._read(root, "evaluator.json")
        # The stored packet is EXACTLY the canonical artifact evaluator
        # serialization: location-dependent ``source_path`` is omitted (the
        # manifest owns derived identities), and hindsight rows persist as
        # structured objects (the loader's own row schema), never as opaque
        # repr strings.
        expected_stored = artifacts._evaluator_payload(components["evaluator"])
        self.assertNotIn("source_path", stored)
        self.assertEqual(stored, expected_stored)
        manifest_files = self._read(root, artifacts.MANIFEST_NAME)["files"]
        self.assertEqual(
            manifest_files["evaluator.json"]["sha256"],
            hashlib.sha256((root / "evaluator.json").read_bytes()).hexdigest(),
        )
        # Gate replay from artifact bytes alone: reload both stored payload
        # mappings through the artifact trust boundary and recompute the
        # identical hard-gate report from those reloaded halves.
        producer_payload = self._read(root, "producer.json")
        # The persisted envelope carries the asserted derived fingerprint and
        # no location-dependent source_path; only the artifact envelope
        # accepts those bytes, re-deriving canonical identity from content
        # and rejecting any rebound fingerprint.
        self.assertNotIn("source_path", producer_payload)
        self.assertIn("fingerprint", producer_payload)
        replayed_producer = artifacts._producer_envelope(producer_payload)
        replayed_evaluator = artifacts._evaluator_envelope(stored, replayed_producer)

        finalized_blob = self._read(root, "finalized_output.json")
        # Reconstruct the finalized analysis from stored payload bytes; the
        # gates consume only facts/analysis/previous_facts, serialized
        # verbatim. The classification field is not gate-relevant and is
        # filled from the stored dispatch schema name.
        finalized = service.InvestmentFinalizedAnalysis(
            facts=finalized_blob["facts"],
            classified_industry=self._read(root, "request.json")["schema_name"],
            previous_facts=finalized_blob["previous_facts"] or None,
            analysis=finalized_blob["analysis"],
        )

        replayed_gates = cq.run_company_hard_gates(
            replayed_producer, replayed_evaluator, finalized
        )
        self.assertEqual(
            replayed_gates.producer_fingerprint, replayed_producer.fingerprint
        )
        self.assertTrue(replayed_gates.passed)
        self.assertEqual(
            artifacts._hard_gate_report_payload(replayed_gates),
            self._read(root, "hard_gates.json"),
        )
        self.assertTrue(artifacts.is_complete_company_run(root))

    def test_relationship_contract_replays_with_exact_request_and_response_parity(self):
        components = self._run(raw=relationship_producer_raw())
        self.assertEqual(len(components["request"].material_relationships), 1)
        self.assertEqual(
            len(components["finalized"].analysis["relationship_reconciliations"]),
            1,
        )
        root = self._write(self.directory / "relationship-run", components)

        stored_request = self._read(root, "request.json")
        expected_request = json.loads(
            artifacts._canonical_json(artifacts._request_payload(components["request"]))
        )
        self.assertEqual(
            stored_request["relationship_facts"],
            expected_request["relationship_facts"],
        )
        self.assertEqual(
            stored_request["material_relationships"],
            expected_request["material_relationships"],
        )
        accepted_payload = json.loads(components["attempts"][-1].content)
        relationship = components["request"].material_relationships[0]
        expected_numeric_claims = []
        for fact_index, ref in enumerate(relationship["required_facts"]):
            fact_key = ref["fact_path"].removeprefix(
                "deterministic_current.relationship_facts."
            )
            fact = (
                components["request"].relationship_facts.get(ref["fact_path"])
                or components["request"].relationship_facts[fact_key]
            )
            expected_numeric_claims.append(
                {
                    "claim_id": (f"relationship-0-fact-{fact_index}-observation"),
                    "path": "$.relationship_reconciliations[0].observation",
                    "value": fact["value"],
                    "metric": fact["metric_label"],
                    "period": fact["period"],
                    "unit": fact["unit"],
                    "currency": fact["currency"],
                    "source_kind": "fact",
                    "fact_path": ref["fact_path"],
                }
            )
        for fact_index, ref in enumerate(relationship["required_facts"][:1]):
            fact_key = ref["fact_path"].removeprefix(
                "deterministic_current.relationship_facts."
            )
            fact = (
                components["request"].relationship_facts.get(ref["fact_path"])
                or components["request"].relationship_facts[fact_key]
            )
            expected_numeric_claims.append(
                {
                    "claim_id": f"relationship-summary-{fact_index + 1}",
                    "path": "summary",
                    "value": fact["value"],
                    "metric": fact["metric_label"],
                    "period": fact["period"],
                    "unit": fact["unit"],
                    "currency": fact["currency"],
                    "source_kind": "fact",
                    "fact_path": ref["fact_path"],
                }
            )
        self.assertEqual(
            accepted_payload["numeric_claims"],
            expected_numeric_claims,
        )
        self.assertEqual(
            components["finalized"].facts["numeric_claims"],
            expected_numeric_claims,
        )
        self.assertEqual(
            self._read(root, "finalized_output.json")["analysis"][
                "relationship_reconciliations"
            ],
            accepted_payload["relationship_reconciliations"],
        )
        reconciliation = accepted_payload["relationship_reconciliations"][0]
        self.assertIn(reconciliation["summary_synthesis"], accepted_payload["summary"])
        self.assertIn(reconciliation["thesis_synthesis"], accepted_payload["thesis"])
        self.assertNotIn(reconciliation["observation"], accepted_payload["summary"])
        self.assertNotIn(reconciliation["uncertainty"], accepted_payload["summary"])
        self.assertEqual(
            reconciliation["summary_fact_paths"],
            [ref["fact_path"] for ref in relationship["required_facts"][:1]],
        )
        replayed_producer = artifacts._producer_envelope(
            self._read(root, "producer.json")
        )
        replayed_request = cb.prepare_company_run(replayed_producer)
        self.assertEqual(
            json.loads(
                artifacts._canonical_json(artifacts._request_payload(replayed_request))
            ),
            stored_request,
        )
        self.assertTrue(artifacts.is_complete_company_run(root))

        for label, changed_rows in (
            (
                "invalid_value",
                [
                    {**expected_numeric_claims[0], "value": "not-a-number"},
                ],
            ),
            (
                "duplicate_claim_id",
                [
                    expected_numeric_claims[0],
                    dict(expected_numeric_claims[0]),
                ],
            ),
        ):
            with self.subTest(replay_summary_rows=label):
                changed_facts = dict(components["finalized"].facts)
                changed_facts["numeric_claims"] = changed_rows
                replayed = cq.run_company_hard_gates(
                    components["producer"],
                    components["evaluator"],
                    components["finalized"]._replace(
                        facts=changed_facts,
                    ),
                )
                self.assertFalse(replayed.passed)
                self.assertTrue(
                    any(
                        failure.code
                        in {"numeric_claim_invalid_row", "numeric_claim_duplicate"}
                        for failure in replayed.failures
                    ),
                    replayed.failures,
                )

    def test_replay_rejects_reforged_stale_relationship_response(self):
        root = self._write(
            self.directory / "stale-relationship-response",
            self._run(raw=relationship_producer_raw()),
        )
        attempts = self._read(root, "attempts.json")
        accepted = json.loads(attempts["attempts"][-1]["content"])
        accepted["relationship_reconciliations"][0]["relationship_id"] = (
            "mr_stale_from_another_request"
        )
        attempts["attempts"][-1]["content"] = json.dumps(accepted)
        self._reforge_payload(root, "attempts.json", attempts)
        self.assertFalse(artifacts.is_complete_company_run(root))

    def test_replay_rejects_reforged_relationship_request_mutations(self):
        for field in ("relationship_facts", "material_relationships"):
            with self.subTest(field=field):
                root = self._write(
                    self.directory / f"mutated-{field}",
                    self._run(raw=relationship_producer_raw()),
                )
                request = self._read(root, "request.json")
                if field == "relationship_facts":
                    fact = next(iter(request[field].values()))
                    fact["value"] = fact["value"] + 1
                else:
                    request[field][0]["relationship_id"] = (
                        "mr_reforged_request_relationship"
                    )
                self._reforge_payload(root, "request.json", request)
                self.assertFalse(artifacts.is_complete_company_run(root))

    # -- priority-1: trusted request rebuild ---------------------------------

    def test_trusted_rebuild_rejects_one_byte_rubric_change_with_reforged_request(self):
        components = self._run()
        destination = self.directory / "rubric-drift"
        drifted_evaluator_raw = evaluator_raw(
            components["producer"].fingerprint,
            strongest_counter_thesis="Demand reverses strongly in H2.",
        )
        drifted_evaluator = cb.load_evaluator_case(
            write_yaml(
                self.directory,
                "rubric-drift.yaml",
                drifted_evaluator_raw,
            ),
            producer=components["producer"],
        )
        # A fully coherent different run is NOT an attack: swapping BOTH the
        # evaluator and judge records coherently yields a valid, self-
        # consistent run that must be ACCEPTED (content-derived identity).
        coherent_destination = self.directory / "rubric-coherent"
        self._write(
            coherent_destination,
            {
                **components,
                "evaluator": drifted_evaluator,
                "judge_records": _judge_round_for(
                    components["producer"],
                    drifted_evaluator,
                    components["finalized"],
                    components["blind_salt"],
                )[2],
            },
        )
        self.assertTrue(artifacts.is_complete_company_run(coherent_destination))

        # The actual drift attack: substitute ONLY the drifted evaluator
        # while retaining the original run's judge records. The records were
        # bound to the ORIGINAL rubric, so the trusted rebuild must fail.
        mixed = {**components, "evaluator": drifted_evaluator}
        with self.assertRaises(ValueError) as ctx:
            self._write(destination, mixed)
        self.assertIn(
            "does not reparse against its rebuilt request",
            str(ctx.exception),
        )
        self.assertFalse(destination.exists())

    def test_trusted_rebuild_rejects_one_byte_finalized_output_change(self):
        components = self._run()
        destination = self.directory / "output-drift"
        payload = narrative_payload()
        payload["summary"] = "Demand durable; supply tight."
        drifted_content = json.dumps(payload)
        recorded = cb.recorded_executor_output(
            drifted_content, {"model": "recorded-model"}
        )
        drifted_finalized = cb.finalize_recorded_company_run(
            recorded, components["producer"]
        )
        _forged_requests, _forged_results, forged_records = _judge_round_for(
            components["producer"],
            components["evaluator"],
            drifted_finalized,
            self.BLIND_SALT,
        )

        # Internally coherent for the DRIFTED output: only the writer's
        # rebuild from the supplied attempt chain can detect the mismatch.
        self.assertTrue(forged_records)
        mixed = {
            **components,
            "judge_records": forged_records,
        }
        with self.assertRaises(ValueError):
            self._write(destination, mixed)
        self.assertFalse(destination.exists())

    def test_trusted_rebuild_rejects_disclosed_or_swapped_blind_salt(self):
        components = self._run()
        destination = self.directory / "salt-drift"
        _other_requests, _other_results, other_records = _judge_round_for(
            components["producer"],
            components["evaluator"],
            components["finalized"],
            "a completely different blind salt",
        )
        # A whole coherent panel judged under a different salt: the supplied
        # blind_salt must reproduce these bindings, so this rejects.
        self.assertTrue(other_records)
        mixed = {
            **components,
            "blind_salt": "a completely different blind salt",
        }
        with self.assertRaises(ValueError):
            self._write(destination, mixed)
        self.assertFalse(destination.exists())

    def test_prompt_cannot_disclose_or_substitute_producer_identity(self):
        case = self._load_case(producer_raw())
        evaluator = self._load_evaluator(case)
        finalized = _finalized_for(case, narrative_payload())
        requests = judging.build_blind_judge_requests(
            case, evaluator, finalized, self.BLIND_SALT
        )
        producer_digest = case.fingerprint
        for request in requests:
            # The response binding is the ONLY identity echoed to judges;
            # the canonical producer/request digests never enter the prompt.
            self.assertIn(request.response_binding, request.prompt)
            self.assertIn(request.token, request.prompt)
            self.assertNotIn(f"fingerprint={producer_digest}", request.prompt)
            packet_json = request.prompt.split("<case_packet", 1)[1]
            self.assertNotIn("forbidden_hindsight", packet_json)
            self.assertNotIn("later_outcomes", packet_json)

    def test_stored_prompts_match_their_trusted_recomputation(self):
        components = self._run()
        root = self._write(self.directory / "run", components)
        rebuilt = judging.build_blind_judge_requests(
            components["producer"],
            components["evaluator"],
            components["finalized"],
            self.BLIND_SALT,
        )
        stored_requests = self._read(root, "judge_requests.json")["requests"]
        for fresh, entry in zip(rebuilt, stored_requests, strict=False):
            # Stored dispatch text is exactly the trusted recomputation; the
            # canonical digest covers it without appearing inside it.
            self.assertEqual(fresh.prompt, entry["prompt"])
            self.assertEqual(fresh.fingerprint, entry["fingerprint"])
            self.assertNotIn(entry["fingerprint"], entry["prompt"])

    def test_writer_reparses_verbatim_raw_response_with_out_of_range_score(self):
        components = self._run()
        destination = self.directory / "score-six"
        requests, _results, records = _judge_round_for(
            components["producer"],
            components["evaluator"],
            components["finalized"],
            self.BLIND_SALT,
        )
        poisoned = []
        for index, record in enumerate(records):
            if index == 0:
                payload = judge_payload(requests[index])
                payload["overall"] = 6
                poisoned.append({**record, "raw_json": json.dumps(payload)})
            else:
                poisoned.append(record)
        with self.assertRaises(ValueError):
            self._write(destination, {**components, "judge_records": poisoned})
        self.assertFalse(destination.exists())

    def test_writer_reparses_verbatim_raw_response_with_blank_rationale(self):
        components = self._run()
        destination = self.directory / "blank-rationale"
        requests, results, records = _judge_round_for(
            components["producer"],
            components["evaluator"],
            components["finalized"],
            self.BLIND_SALT,
        )
        poisoned = []
        for index, record in enumerate(records):
            if index == 1:
                payload = judge_payload(requests[index])
                payload["dimension_scores"][judging.JUDGE_DIMENSIONS[4]][
                    "rationale"
                ] = ""
                poisoned.append({**record, "raw_json": json.dumps(payload)})
            else:
                poisoned.append(record)
        with self.assertRaises(ValueError):
            self._write(destination, {**components, "judge_records": poisoned})
        self.assertFalse(destination.exists())

    def test_writer_reparses_binding_mismatch_inside_raw_response(self):
        components = self._run()
        destination = self.directory / "binding-mismatch"
        requests, results, records = _judge_round_for(
            components["producer"],
            components["evaluator"],
            components["finalized"],
            self.BLIND_SALT,
        )
        poisoned = []
        for index, record in enumerate(records):
            if index == 2:
                payload = judge_payload(requests[index])
                payload["response_binding"] = "e" * 64
                poisoned.append({**record, "raw_json": json.dumps(payload)})
            else:
                poisoned.append(record)
        with self.assertRaises(ValueError):
            self._write(destination, {**components, "judge_records": poisoned})
        self.assertFalse(destination.exists())

    def test_stored_judge_results_carry_verbatim_raw_responses(self):
        components = self._run()
        root = self._write(self.directory / "run", components)
        blob = self._read(root, "judge_results.json")["results"]
        self.assertEqual(len(blob), 3)
        for entry in blob:
            self.assertIn("raw_response", entry)
            self.assertIsInstance(entry["raw_response"], str)
            reparsed = json.loads(entry["raw_response"])
            self.assertEqual(reparsed["role"], entry["role"])
            self.assertEqual(reparsed["overall"], entry["overall"])

    # -- priority-1: independent judge execution identities ------------------

    def test_duplicate_judge_execution_id_is_rejected_before_publication(self):
        components = self._run()
        destination = self.directory / "dup-judge-execution"
        requests, results, records = _judge_round_for(
            components["producer"],
            components["evaluator"],
            components["finalized"],
            self.BLIND_SALT,
        )
        duplicated = [
            dict(record, execution_id="shared-judge-execution") for record in records
        ]
        with self.assertRaises(ValueError):
            self._write(destination, {**components, "judge_records": duplicated})
        self.assertFalse(destination.exists())

    def test_duplicate_judge_session_id_is_rejected_before_publication(self):
        components = self._run()
        destination = self.directory / "dup-judge-session"
        requests, results, records = _judge_round_for(
            components["producer"],
            components["evaluator"],
            components["finalized"],
            self.BLIND_SALT,
        )
        duplicated = [
            dict(record, session_id="shared-judge-session") for record in records
        ]
        with self.assertRaises(ValueError):
            self._write(destination, {**components, "judge_records": duplicated})
        self.assertFalse(destination.exists())

    def test_judge_execution_reusing_the_producer_execution_id_is_rejected(self):
        components = self._run()
        destination = self.directory / "producer-reuse"
        requests, results, records = _judge_round_for(
            components["producer"],
            components["evaluator"],
            components["finalized"],
            self.BLIND_SALT,
        )
        reused = [
            dict(record, execution_id=EXECUTOR_IDENTITY["execution_id"])
            for record in records
        ]
        with self.assertRaises(ValueError):
            self._write(destination, {**components, "judge_records": reused})
        self.assertFalse(destination.exists())

    def test_judge_execution_and_session_cross_field_reuse_is_rejected(self):
        variants = (
            ("execution-reuses-session", 1, "execution_id", 0, "session_id"),
            ("session-reuses-execution", 1, "session_id", 0, "execution_id"),
        )
        for label, target_index, target_field, source_index, source_field in variants:
            with self.subTest(label=label):
                components = self._run()
                root = self._write(self.directory / f"{label}-replay", components)
                payload = self._read(root, "judge_results.json")
                payload["results"][target_index][target_field] = payload["results"][
                    source_index
                ][source_field]
                self._reforge_payload(root, "judge_results.json", payload)
                self.assertFalse(artifacts.is_complete_company_run(root))

                poisoned_records = [
                    dict(record) for record in components["judge_records"]
                ]
                poisoned_records[target_index][target_field] = poisoned_records[
                    source_index
                ][source_field]
                destination = self.directory / f"{label}-publication"
                with self.assertRaises(ValueError):
                    self._write(
                        destination,
                        {**components, "judge_records": poisoned_records},
                    )
                self.assertFalse(destination.exists())

    def test_judge_reusing_producer_identity_is_rejected_on_replay(self):
        components = self._run()
        root = self._write(
            self.directory / "producer-execution-reuse-replay", components
        )
        payload = self._read(root, "judge_results.json")
        payload["results"][0]["session_id"] = EXECUTOR_IDENTITY["execution_id"]
        self._reforge_payload(root, "judge_results.json", payload)
        self.assertFalse(artifacts.is_complete_company_run(root))

        poisoned_records = [dict(record) for record in components["judge_records"]]
        poisoned_records[0]["session_id"] = EXECUTOR_IDENTITY["execution_id"]
        destination = self.directory / "producer-execution-reuse-publication"
        with self.assertRaises(ValueError):
            self._write(
                destination,
                {**components, "judge_records": poisoned_records},
            )
        self.assertFalse(destination.exists())

    def test_judge_reusing_producer_session_identity_is_rejected_on_replay(self):
        producer_session = "producer-session-2026-04-01"
        components = self._run()
        session_attempts = [
            dataclasses.replace(
                attempt,
                provenance={**attempt.provenance, "session_id": producer_session},
            )
            for attempt in components["attempts"]
        ]
        components = {**components, "attempts": session_attempts}

        root = self._write(self.directory / "producer-session-reuse-replay", components)
        self.assertTrue(artifacts.is_complete_company_run(root))
        payload = self._read(root, "judge_results.json")
        payload["results"][0]["execution_id"] = producer_session
        self._reforge_payload(root, "judge_results.json", payload)
        self.assertFalse(artifacts.is_complete_company_run(root))

        poisoned_records = [dict(record) for record in components["judge_records"]]
        poisoned_records[0]["execution_id"] = producer_session
        destination = self.directory / "producer-session-reuse-publication"
        with self.assertRaises(ValueError):
            self._write(
                destination,
                {**components, "judge_records": poisoned_records},
            )
        self.assertFalse(destination.exists())

    def test_blank_or_missing_judge_execution_fields_are_rejected(self):
        components = self._run()
        destination = self.directory / "blank-judge-fields"
        requests, results, records = _judge_round_for(
            components["producer"],
            components["evaluator"],
            components["finalized"],
            self.BLIND_SALT,
        )
        variants = [
            [{**record, "execution_id": ""} for record in records],
            [{**record, "execution_id": "   "} for record in records],
            [{**record, "session_id": ""} for record in records],
            [
                {k: v for k, v in record.items() if k != "execution_id"}
                for record in records
            ],
            [
                {k: v for k, v in record.items() if k != "session_id"}
                for record in records
            ],
        ]
        for variant in variants:
            with self.subTest(variant=repr(variant)[:80]):
                with self.assertRaises(ValueError):
                    self._write(
                        destination,
                        {**components, "judge_records": variant},
                    )
                self.assertFalse(destination.exists())

    def test_stored_per_judge_provenance_is_distinct_and_independent(self):
        components = self._run()
        root = self._write(self.directory / "run", components)
        blob = self._read(root, "judge_results.json")["results"]
        pairs = {(entry["execution_id"], entry["session_id"]) for entry in blob}
        self.assertEqual(len(pairs), len(blob))
        producer_execution = EXECUTOR_IDENTITY["execution_id"]
        for entry in blob:
            self.assertNotEqual(entry["execution_id"], producer_execution)
            self.assertNotEqual(entry["session_id"], producer_execution)
            self.assertEqual(set(entry["judge_provenance"]), set())

    def test_wrong_token_or_role_in_a_judge_record_is_rejected(self):
        components = self._run()
        destination = self.directory / "wrong-token"
        requests, results, records = _judge_round_for(
            components["producer"],
            components["evaluator"],
            components["finalized"],
            self.BLIND_SALT,
        )
        wrong_token = [dict(record, token="f" * 32) for record in records]
        with self.assertRaises(ValueError):
            self._write(destination, {**components, "judge_records": wrong_token})
        self.assertFalse(destination.exists())
        wrong_role = [dict(record, role=judging.JUDGE_ROLES[0]) for record in records]
        with self.assertRaises(ValueError):
            self._write(destination, {**components, "judge_records": wrong_role})
        self.assertFalse(destination.exists())

    def _with_fingerprint(self, case, fingerprint):
        """Rebind stored identity the way a tampering caller would."""
        return dataclasses.replace(case, fingerprint=fingerprint)

    def test_arbitrary_fingerprint_replacement_is_rejected_at_publication(self):
        components = self._run()
        destination = self.directory / "identity-swap"
        swapped_case = self._with_fingerprint(
            components["producer"],
            canonical_fingerprint({"arbitrary": "replacement"}),
        )
        with self.assertRaises(ValueError) as ctx:
            self._write(destination, {**components, "producer": swapped_case})
        message = str(ctx.exception)
        # Rejection happens at the derivation trust boundary — before any
        # downstream evaluator/gate/request/judge material is consumed —
        # with the writer's exact rebound-identity wording.
        self.assertIn("rebound producer case", message)
        self.assertIn(
            "canonical identity derived from the normalized producer fields",
            message,
        )
        self.assertFalse(destination.exists())

    def test_fingerprint_swap_of_same_shape_is_rejected_even_with_matching_fixture(
        self,
    ):
        components = self._run()
        destination = self.directory / "digest-shape-swap"
        swapped_case = self._with_fingerprint(components["producer"], "b" * 64)
        with self.assertRaises(ValueError):
            self._write(destination, {**components, "producer": swapped_case})
        self.assertFalse(destination.exists())

    def test_derived_producer_fingerprint_matches_canonical_content_digest(self):
        raw = producer_raw()
        case = self._load_case(raw)
        # The stored identity is the derived canonical digest over the
        # normalized fixture content — not an independent persisted value.
        self.assertEqual(case.fingerprint, cb.canonical_producer_fingerprint(case))
        self.assertEqual(
            case.fingerprint,
            canonical_fingerprint(cb.canonical_producer_fingerprint_payload(case)),
        )
        self.assertRegex(case.fingerprint, r"[a-f0-9]{64}")

    def test_loaded_case_fingerprint_survives_no_arbitrary_rebind(self):
        case = self._load_case(producer_raw())
        replaced = self._with_fingerprint(case, "c" * 64)
        # The writer recomputes identity from content, so an arbitrarily
        # rebound fingerprint can never reach publication.
        self.assertNotEqual(replaced.fingerprint, case.fingerprint)
        with self.assertRaises(ValueError):
            self._write(
                self.directory / "rebind", {**self._run(), "producer": replaced}
            )
        self.assertFalse((self.directory / "rebind").exists())

    def test_second_run_components_carry_their_own_derived_identity(self):
        second = self._second_run()
        self.assertNotEqual(
            second["producer"].fingerprint,
            self._run()["producer"].fingerprint,
        )
        root = self._write(self.directory / "second-run", second)
        self.assertTrue(artifacts.is_complete_company_run(root))

    # -- provenance-complete replay ------------------------------------------

    def test_completed_run_is_replayable_from_immutable_bytes_alone(self):
        components = self._run()
        root = self._write(self.directory / "run", components)
        manifest = self._read(root, artifacts.MANIFEST_NAME)
        # Reload BOTH halves from stored payload bytes through the artifact
        # trust boundary; identity must survive the round trip untouched.
        producer_payload = self._read(root, "producer.json")
        # The persisted envelope is exactly the artifact contract: canonical
        # identity fields plus the derived fingerprint — no source_path. The
        # envelope loader re-derives identity and rejects unexpected keys.
        self.assertNotIn("source_path", producer_payload)
        producer = artifacts._producer_envelope(producer_payload)
        evaluator = artifacts._evaluator_envelope(
            self._read(root, "evaluator.json"), producer
        )
        self.assertEqual(producer.fingerprint, manifest["producer_fingerprint"])
        self.assertEqual(
            evaluator.producer_fingerprint, manifest["producer_fingerprint"]
        )

        finalized_blob = self._read(root, "finalized_output.json")
        replayed_finalized = service.InvestmentFinalizedAnalysis(
            facts=finalized_blob["facts"],
            classified_industry=finalized_blob["analysis"]
            .get("classification", {})
            .get("industry", ""),
            previous_facts=finalized_blob["previous_facts"] or None,
            analysis=finalized_blob["analysis"],
        )
        # Byte-only replay uses the POST-JUDGING disclosed salt file: the
        # exact salt bytes are sufficient to recompute every judge request
        # without any session state.
        disclosed = json.loads((root / "blind_salt.json").read_text(encoding="utf-8"))
        replay_salt = bytes.fromhex(disclosed["salt_hex"])
        self.assertNotIn(replay_salt, (root / artifacts.MANIFEST_NAME).read_bytes())
        rebuilt_requests = judging.build_blind_judge_requests(
            producer,
            evaluator,
            replayed_finalized,
            replay_salt,
        )
        stored_requests = self._read(root, "judge_requests.json")["requests"]
        stored_results = self._read(root, "judge_results.json")["results"]
        for fresh, entry in zip(rebuilt_requests, stored_requests, strict=False):
            self.assertEqual(fresh.fingerprint, entry["fingerprint"])
        # Every stored verdict reparses strictly against the rebuilt
        # request and matches its verbatim raw response.
        for fresh, entry in zip(rebuilt_requests, stored_results, strict=False):
            reparsed = judging.parse_judge_result(fresh, entry["raw_response"])
            self.assertEqual(reparsed.overall, entry["overall"])
            self.assertEqual(reparsed.response_binding, entry["response_binding"])
        gate_blob = self._read(root, "hard_gates.json")
        self.assertTrue(gate_blob["passed"])
        panel_blob = self._read(root, "panel_report.json")
        self.assertTrue(panel_blob["passed"])
        self.assertEqual(panel_blob["overall_median"], 5.0)
        self.assertTrue(artifacts.is_complete_company_run(root))

    def test_mid_write_exception_cleans_new_directory(self):
        components = self._run()
        destination = self.directory / "aborted"
        real_exclusive_write = artifacts._exclusive_write
        calls = {"count": 0}

        def failing_write(path, blob):
            calls["count"] += 1
            if calls["count"] == 3:  # Fail partway through the payload files.
                raise OSError("simulated disk failure")
            return real_exclusive_write(path, blob)

        with patch.object(artifacts, "_exclusive_write", side_effect=failing_write):
            with self.assertRaises(OSError):
                self._write(destination, components)
        self.assertFalse(destination.exists())
        self.assertGreaterEqual(calls["count"], 3)


_LEDGER_EXCERPT = (
    "This quarter, revenue was $64.7 billion, up 15% and 16% in constant "
    "currency. Azure and other cloud services revenue grew 29% and 30% in "
    "constant currency. Capital expenditures including finance leases were "
    "$19 billion in FY2024 Q4, in line with expectations, and cash paid "
    "for P, P, and E was $13.9 billion. Free cash flow was $23.3 billion, "
    "up 18% year-over-year."
)


class NumericLedgerArtifactRoundTripTests(unittest.TestCase):
    """The settled numeric-claim ledger through the full benchmark lifecycle:
    replay finalization, gate recomputation, and immutable-artifact bytes.
    """

    BLIND_SALT = "company-run-blind-salt"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)

    @classmethod
    def _claim_row(cls, **overrides):
        # Text rows always carry the verbatim producer quote: structural
        # validation rejects any text row without one, so the default is
        # the real transcript sentence — adversarial rewrites of ``value``
        # then fail tuple verification, not schema.
        row = {
            "claim_id": "capex_fy24q4",
            "path": "summary",
            "value": "$19B",
            "metric": "capital expenditures including finance leases",
            "period": "FY2024 Q4",
            "unit": "usd_billions",
            "currency": "USD",
            "source_kind": "text",
            "quote": (
                "Capital expenditures including finance leases were $19 "
                "billion in FY2024 Q4, in line with expectations"
            ),
        }
        row.update(overrides)
        return row

    @classmethod
    def _ledger_payload(cls):
        """Valid narrative whose capex claim binds to the real transcript.

        The fixture excerpt carries the exact capex/cash-PP&E/FCF sentences,
        so the verbatim quote and every supported rendering resolve against
        the frozen producer case without touching shipped fixtures.
        """
        payload = narrative_payload()
        payload["summary"] = (
            "Capital expenditures including finance leases were $19 billion "
            "in FY2024 Q4."
        )
        # Keep every other gate green by design: the qualitative evidence
        # quote sits inside this fixture excerpt too.
        payload["qualitative"]["ai_demand"]["evidence"] = "revenue was $64.7 billion"
        payload["numeric_claims"] = [cls._claim_row()]
        return payload

    def _load_case(self):
        raw = producer_raw()
        raw["excerpt"] = _LEDGER_EXCERPT
        return cb.load_producer_case(write_yaml(self.directory, "producer.yaml", raw))

    def _evaluator_for(self, case):
        return cb.load_evaluator_case(
            write_yaml(
                self.directory,
                "evaluator.yaml",
                evaluator_raw(case.fingerprint),
            ),
            producer=case,
        )

    def _run(self):
        case = self._load_case()
        evaluator = self._evaluator_for(case)
        payload = self._ledger_payload()
        recorded = cb.recorded_executor_output(
            json.dumps(payload), {"model": "recorded-model"}
        )
        finalized = cb.finalize_recorded_company_run(recorded, case)
        return case, evaluator, finalized

    def test_replayed_finalization_preserves_ledger_rows(self):
        case, _evaluator, finalized = self._run()
        rows = finalized.facts.get("numeric_claims")
        self.assertIsInstance(rows, list)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["claim_id"], "capex_fy24q4")
        self.assertEqual(rows[0]["value"], "$19B")
        # Replay determinism: a second identical run yields the same rows.
        _case2, _evaluator2, finalized2 = self._run()
        self.assertEqual(finalized2.facts["numeric_claims"], rows)

    def test_supported_ledger_passes_recomputed_gates(self):
        case, evaluator, finalized = self._run()
        report = cq.run_company_hard_gates(case, evaluator, finalized)
        binding_codes = {
            failure.code
            for failure in report.failures
            if failure.code.startswith("numeric_claim_")
        }
        self.assertEqual(binding_codes, set())

    def test_unsupported_capex_number_fails_gates_and_stays_out_of_artifact(self):
        case = self._load_case()
        payload = self._ledger_payload()
        # The adversarial rewrite: $20B amid unrelated 20s. The old
        # token-presence gate passed exactly this shape.
        payload["summary"] = (
            "Capital expenditures including finance leases were $20 billion "
            "in FY2024 Q4."
        )
        payload["numeric_claims"] = [
            self._claim_row(value="not-a-number", claim_id="capex_wrong"),
        ]
        recorded = cb.recorded_executor_output(
            json.dumps(payload), {"model": "recorded-model"}
        )

        with self.assertRaises(service.InvestmentValidationError) as raised:
            cb.finalize_recorded_company_run(recorded, case)

        self.assertEqual(raised.exception.category, service.VALIDATION_JSON_SCHEMA)
        self.assertTrue(
            any(
                "value: must be a finite number" in p for p in raised.exception.problems
            ),
            raised.exception.problems,
        )

    def test_artifact_round_trip_preserves_ledger_rows_in_bytes(self):
        case, evaluator, finalized = self._run()
        requests = judging.build_blind_judge_requests(
            case, evaluator, finalized, self.BLIND_SALT
        )
        judge_records = [
            {
                "role": request.role,
                "token": request.token,
                "raw_json": json.dumps(judge_payload(request)),
                "execution_id": f"judge-exec-{index}",
                "session_id": f"judge-session-{index}",
                "provenance": {},
            }
            for index, request in enumerate(requests)
        ]
        stage_config = {
            "executor": dict(EXECUTOR_IDENTITY),
            "model": {"slug": "recorded-model", "temperature": 0},
            "retries": 1,
        }
        output_dir = self.directory / "run"
        artifacts.write_immutable_company_run(
            output_dir,
            producer=case,
            request=cb.prepare_company_run(case),
            attempts=[
                artifacts.RecordedAttempt(
                    index=0,
                    content=json.dumps(self._ledger_payload()),
                    accepted=True,
                    repair_prompt=None,
                    provenance={
                        **EXECUTOR_IDENTITY,
                        "model": "recorded-model",
                    },
                )
            ],
            finalized=finalized,
            evaluator=evaluator,
            blind_salt=self.BLIND_SALT,
            judge_records=judge_records,
            git_commit="0f1e2d3c4b5a",
            git_dirty=False,
            created_at=datetime(2026, 4, 1, tzinfo=UTC),
            stage_config=stage_config,
        )
        stored_finalized = json.loads(
            (output_dir / "finalized_output.json").read_text(encoding="utf-8")
        )
        stored_rows = stored_finalized["facts"]["numeric_claims"]
        self.assertEqual(stored_rows, cb.plain_copy(finalized.facts["numeric_claims"]))
        analysis_rows = stored_finalized["analysis"]["numeric_claims"]
        self.assertEqual(
            analysis_rows, cb.plain_copy(finalized.analysis["numeric_claims"])
        )
        # The manifest digest covers the ledger-bearing payload: any row
        # tampering is detectable from the artifact alone.
        manifest = json.loads(
            (output_dir / artifacts.MANIFEST_NAME).read_text(encoding="utf-8")
        )
        blob = (output_dir / "finalized_output.json").read_bytes()
        self.assertEqual(
            manifest["files"]["finalized_output.json"]["sha256"],
            hashlib.sha256(blob).hexdigest(),
        )
        self.assertTrue(artifacts.is_complete_company_run(output_dir))
