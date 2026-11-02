"""Offline protocol, workload, reference and report tests. No external service required."""
import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mock_server import start
import benchmark as b

ROOT = Path(b.__file__).parent


def reference(name="Example"):
    return {"schema_version": 1, "name": name, "conditions": {"concurrency": 2, "min_samples": 2},
            "bands": [{"min_input_tokens": 1000, "max_input_tokens": 20000,
                       "baseline_ms": {"ttft_p90": 100, "tpot_avg": 10},
                       "limits_ms": {"ttft_p90": 150}}]}


def row(ok=True, target=16000):
    return {"ok": ok, "usage": {"prompt_tokens": target, "completion_tokens": 256},
            "cached_tokens": 0, "reasoning_seen": False, "validity_issues": [] if ok else ["request_failed"],
            "ttft_ms": 200 if ok else None, "tpot_ms": 20 if ok else None,
            "e2e_ms": 500 if ok else 300000, "generation_events": 2}


class ReferenceTests(unittest.TestCase):
    def load(self, refs):
        with tempfile.TemporaryDirectory() as td:
            paths = []
            for i, ref in enumerate(refs):
                path = Path(td) / f"{i}.json"
                b.write_json(path, ref)
                paths.append(path)
            return b.load_references(paths)

    def compare(self, refs, rows, target=16000, concurrency=2):
        return b.compare_references(refs, {}, target, rows,
                                    argparse.Namespace(concurrency=concurrency))

    def test_optional_and_multiple_references(self):
        self.assertEqual(self.load([]), [])
        refs = self.load([reference("One"), reference("Two")])
        compared = self.compare(refs, [row(), row()])
        self.assertEqual(list(compared), ["One", "Two"])
        self.assertEqual(compared["One"]["status"], "COMPARABLE")
        metric = compared["One"]["metrics"]["ttft_p90"]
        self.assertEqual(metric["ratio_to_baseline"], 2)
        self.assertAlmostEqual(metric["ratio_to_limit"], 4 / 3)

    def test_bands_are_lower_inclusive_upper_exclusive(self):
        ref = reference()
        self.assertIsNotNone(b.band_for(ref, 1000))
        self.assertIsNone(b.band_for(ref, 20000))
        self.assertIsNone(b.band_for(ref, None))
        result = self.compare([ref], [row()], target=20000)["Example"]
        self.assertEqual(result["status"], "NOT_APPLICABLE")
        self.assertEqual(result["metrics"], {})

    def test_partial_errors_keep_valid_subset_and_warning(self):
        result = self.compare([reference()], [row(), row(False)])["Example"]
        self.assertEqual(result["status"], "LIMITED")
        self.assertEqual(result["valid_samples"], 1)
        self.assertIn("request_failed", result["reasons"])
        self.assertIn("valid_samples_below_2", result["reasons"])
        self.assertEqual(result["metrics"]["ttft_p90"]["measured_ms"], 200)

    def test_all_errors_have_no_ratios(self):
        result = self.compare([reference()], [row(False), row(False)])["Example"]
        self.assertEqual(result["status"], "NO_VALID_SAMPLES")
        self.assertIsNone(result["metrics"]["ttft_p90"]["ratio_to_baseline"])
        self.assertNotIn("actual_input_in_different_reference_band", result["reasons"])

    def test_condition_mismatch_and_actual_band_mismatch(self):
        result = self.compare([reference()], [row(target=20000)], concurrency=1)["Example"]
        self.assertEqual(result["status"], "LIMITED")
        self.assertIn("actual_input_in_different_reference_band", result["reasons"])
        self.assertIn("concurrency_expected_2_actual_1", result["reasons"])

    def test_conditions_check_observed_usage(self):
        ref = reference()
        ref["conditions"].update(require_cold_cache=True, allow_reasoning=False,
                                 min_completion_tokens=512, max_tokens=512)
        result_row = row()
        result_row.update(cached_tokens=100, reasoning_seen=True)
        result = self.compare([ref], [result_row, result_row])["Example"]
        self.assertEqual(result["status"], "LIMITED")
        for reason in ("reference_requires_verified_cold_cache", "reference_excludes_reasoning",
                       "completion_tokens_below_512"):
            self.assertIn(reason, result["reasons"])

    def test_invalid_reference_files_rejected(self):
        cases = []
        for value in (0, -1, True, float("nan"), float("inf"), "100"):
            ref = reference()
            ref["bands"][0]["baseline_ms"]["ttft_p90"] = value
            cases.append([ref])
        ref = reference()
        ref["bands"].append(copy.deepcopy(ref["bands"][0]))
        cases.append([ref])
        ref = reference()
        ref["bands"][0]["baseline_ms"] = {"typo_metric": 10}
        cases.append([ref])
        ref = reference()
        ref["conditions"]["typo"] = 1
        cases.append([ref])
        cases.append([reference(), reference()])
        for refs in cases:
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                self.load(refs)

    def test_read_demo_files(self):
        refs = b.load_references([ROOT / "references/demo-a.json", ROOT / "references/demo-b.json"])
        self.assertEqual(len(refs), 2)

    def test_report_dynamic_columns_and_snapshot(self):
        for refs in ([], [reference("One")], [reference("One"), reference("Two"), reference("Three")]):
            with self.subTest(count=len(refs)), tempfile.TemporaryDirectory() as td:
                rows = [row(), row(False)]
                summary = b.summarize(rows, 301)
                result = {"mode": "baseline", "status": "COMPLETED", "load_model": "test",
                          "references": refs, "scenarios": [{
                              "target_tokens": 16000, "summary": summary,
                              "valid_summary": b.summarize(rows[:1], 301), "valid_samples": 1,
                              "invalid_reasons": ["request_failed"],
                              "reference_comparisons": self.compare(refs, rows)}]}
                b.render_report(result, {"model": "fixture"}, td)
                text = (Path(td) / "report.md").read_text(encoding="utf-8")
                self.assertEqual(text.count("## Input"), 1)
                self.assertIn("completed: 1; errors: 1; valid samples: 1; error rate: 50.00%", text)
                self.assertIn("Partial sample warning", text)
                for ref in refs:
                    self.assertIn(ref["name"] + " baseline", text)
                self.assertEqual(b.load_json(Path(td) / "report.json")["references"], refs)
                self.assertNotRegex(text, r"\b(?:PA" + r"SS|FA" + r"IL)\b")


class WorkloadTests(unittest.TestCase):
    def fake_module(self, mapping=False):
        class Tokenizer:
            def apply_chat_template(self, messages, **kwargs):
                ids = [ord(c) for c in "".join(
                    "<" + m["role"] + ">" + m["content"] + "</>" for m in messages) + "<assistant>"]
                return {"input_ids": ids} if mapping else ids
        return types.SimpleNamespace(AutoTokenizer=types.SimpleNamespace(
            from_pretrained=lambda *args, **kwargs: Tokenizer()))

    def test_prepare_length_precedence_and_manifest(self):
        cases = [
            ({"lengths": [1000, 2000]}, [], [1000, 2000]),
            ({"lengths": [1000, 2000]}, ["--lengths", "3000"], [3000]),
            ({}, [], [4000, 8000, 16000]),
            (None, [], [4000, 8000, 16000]),
            (None, ["--lengths", "2000", "1000"], [2000, 1000]),
        ]
        for settings, extra, expected in cases:
            with self.subTest(settings=settings, extra=extra), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                out = root / "workload"
                argv = ["prepare", "--tokenizer", "fixture", "--samples", "1", "--out", str(out)]
                if settings is not None:
                    config = {"base_url": "http://127.0.0.1:1/v1", "model": "fixture",
                              "require_api_key": False, "max_context_tokens": 32768, **settings}
                    b.write_json(root / "config.json", config)
                    argv += ["--config", str(root / "config.json")]
                with patch.dict(sys.modules, {"transformers": self.fake_module()}):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(b.main(argv + extra), 0)
                self.assertEqual(b.load_json(out / "workload_manifest.json")["lengths"], expected)
                prompts = b.load_prompts(out / "prompts.jsonl")
                self.assertEqual([r["target_tokens"] for r in prompts], expected)
                self.assertEqual([r["local_prompt_tokens"] for r in prompts], expected)

    def test_prepare_invalid_config_lengths_fail_before_tokenizer_or_output(self):
        for lengths in ([], None, "16000", [True], [1000.0], [999],
                        [1000, 1000], [[1000]], [1000, "2000"]):
            with self.subTest(lengths=lengths), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                b.write_json(root / "config.json", {
                    "base_url": "http://127.0.0.1:1/v1", "model": "fixture",
                    "require_api_key": False, "lengths": lengths})
                with patch.dict(sys.modules, {"transformers": None}):
                    with contextlib.redirect_stderr(io.StringIO()) as errors:
                        code = b.main(["prepare", "--config", str(root / "config.json"),
                                       "--tokenizer", "fixture", "--out", str(root / "workload")])
                self.assertEqual(code, 2)
                self.assertIn("lengths must be", errors.getvalue())
                self.assertFalse((root / "workload").exists())

    def test_prepare_resolved_lengths_respect_context_limit(self):
        for extra in ([], ["--lengths", "1800"]):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                b.write_json(root / "config.json", {
                    "base_url": "http://127.0.0.1:1/v1", "model": "fixture",
                    "require_api_key": False, "max_context_tokens": 2000, "lengths": [1600]})
                with patch.dict(sys.modules, {"transformers": None}):
                    with contextlib.redirect_stderr(io.StringIO()) as errors:
                        code = b.main(["prepare", "--config", str(root / "config.json"),
                                       "--tokenizer", "fixture", "--out", str(root / "workload")] + extra)
                self.assertEqual(code, 2)
                self.assertIn("exceeds deployed context limit", errors.getvalue())
                self.assertFalse((root / "workload").exists())

    def test_prepare_cache_modes_and_mapping_tokenizer(self):
        for cache_type in b.CACHE_TYPES:
            for mapping, cache_tokens in ((False, 1000), (False, 16000), (True, 16000)):
                with self.subTest(cache_type=cache_type, mapping=mapping, cache_tokens=cache_tokens), tempfile.TemporaryDirectory() as td:
                    out = Path(td) / "workload"
                    args = argparse.Namespace(tokenizer="offline-fixture", config=None, seed=42,
                                              template_options=None, lengths=[1000], samples=2,
                                              run_id="test", cache_type=cache_type, cache_tokens=cache_tokens, out=out)
                    with patch.dict(sys.modules, {"transformers": self.fake_module(mapping)}):
                        with contextlib.redirect_stdout(io.StringIO()):
                            b.prepare(args)
                    data = b.load_prompts(out / "prompts.jsonl")
                    self.assertEqual([r["local_prompt_tokens"] for r in data], [1000, 1000])
                    if cache_type == "none":
                        self.assertFalse((out / "cache_pair.json").exists())
                    else:
                        pair = b.load_json(out / "cache_pair.json")
                        b.validate_cache_pair(pair, {"max_context_tokens": 20000})
                        self.assertTrue(.88 <= pair["local_shared_prefix_ratio"] <= .92)
                        if cache_type == "message-append":
                            self.assertEqual(pair["probe"][:len(pair["warm"])], pair["warm"])

    def test_prepare_needs_no_key_and_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = {"base_url": "http://127.0.0.1:1/v1", "model": "fixture",
                      "api_key_env": "SERVICE_CHECK_UNUSED_TEST_KEY", "require_api_key": True,
                      "max_context_tokens": 2000}
            b.write_json(root / "config.json", config)
            args = argparse.Namespace(tokenizer="fixture", config=root / "config.json", seed=42,
                                      template_options=None, lengths=[1000], samples=1, run_id="test",
                                      cache_type="none", cache_tokens=1000, out=root / "workloads")
            with patch.dict(sys.modules, {"transformers": self.fake_module()}):
                with contextlib.redirect_stdout(io.StringIO()):
                    b.prepare(args)
                before = (args.out / "prompts.jsonl").read_bytes()
                with self.assertRaises(FileExistsError):
                    b.prepare(args)
                self.assertEqual((args.out / "prompts.jsonl").read_bytes(), before)

    def test_context_guard_and_duplicate_workloads(self):
        with self.assertRaises(ValueError):
            b.context_guard({"max_context_tokens": 2000, "max_tokens": 512}, 1600)
        with tempfile.TemporaryDirectory() as td:
            record = {"target_tokens": 1000, "messages": [{"role": "user", "content": "same"}]}
            path = Path(td) / "prompts.jsonl"
            path.write_text((json.dumps(record) + "\n") * 2)
            with self.assertRaises(ValueError):
                b.load_prompts(path)

    def test_nearest_rank_sse_and_cache_usage(self):
        self.assertEqual(b.percentile(list(range(1, 11)), .9), 9)
        self.assertEqual(b.cached_tokens({"cached_tokens": 0}), 0)
        self.assertIsNone(b.cached_tokens({"cached_tokens": True}))
        stream = io.BytesIO(': comment\r\ndata: {"name":\r\ndata: "caf\u00e9"}\r\n\r\ndata: [DONE]\n\n'.encode())
        events = list(b.parse_sse(stream))
        self.assertEqual(json.loads(events[0]), {"name": "caf\u00e9"})
        self.assertEqual(events[1], "[DONE]")

    def test_summary_denominator_includes_errors(self):
        measured = b.summarize([row(), row(False)], 2)
        self.assertEqual(measured["output_tokens_s"], 128)
        self.assertEqual(measured["error_rate"], .5)


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = start()
        cls.config = {"base_url": f"http://127.0.0.1:{cls.server.server_port}/v1",
                      "model": "mock", "require_api_key": False, "max_tokens": 512, "timeout_s": 5}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, text, **kwargs):
        return b.Client(self.config).request([{"role": "user", "content": text}], **kwargs)

    def test_stream_usage_and_timing(self):
        result = self.request("MOCK_TOKENS=16000")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["done"])
        self.assertEqual(result["generation_events"], 2)
        self.assertGreater(result["ttft_ms"], 0)
        self.assertGreater(result["tpot_ms"], 0)
        self.assertLess(result["ttft_ms"], result["e2e_ms"])
        self.assertEqual(b.metric_valid(result, self.config, 16000), [])

    def test_incomplete_and_error_streams(self):
        for text in ("NO_DONE", "STREAM_ERROR"):
            with self.subTest(text=text):
                self.assertFalse(self.request(text)["ok"])

    def test_validity_and_optional_reasoning_cache_policy(self):
        for text, issue in (
            ("MISSING_USAGE", "missing_prompt_tokens"),
            ("MOCK_TOKENS=16000 CACHE_HIT", "cache_hit_in_cold_baseline"),
            ("MOCK_TOKENS=16000 SHORT_OUTPUT", "output_too_short_or_missing_usage"),
            ("MOCK_TOKENS=16000 REASONING", "reasoning_makes_tpot_incomparable"),
        ):
            with self.subTest(text=text):
                result = self.request(text)
                self.assertIn(issue, b.metric_valid(result, self.config, 16000))
        result = self.request("MOCK_TOKENS=16000 REASONING CACHE_HIT")
        self.assertEqual(b.metric_valid(result, {**self.config, "allow_reasoning": True,
                                               "require_cold_cache": False}, 16000), [])

    def test_total_timeout_including_heartbeats(self):
        client = b.Client({**self.config, "timeout_s": .1, "read_timeout_s": 1})
        started = time.perf_counter()
        result = client.request([{"role": "user", "content": "STALL"}])
        self.assertFalse(result["ok"])
        self.assertLess(time.perf_counter() - started, 1)

    def test_read_timeout_after_headers_before_first_token(self):
        client = b.Client({**self.config, "timeout_s": 2, "read_timeout_s": .05})
        started = time.perf_counter()
        result = client.request([{"role": "user", "content": "SILENCE"}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["generation_events"], 0)
        self.assertIsNone(result["ttft_ms"])
        self.assertLess(time.perf_counter() - started, 1)

    def test_error_semantics(self):
        result = self.request("test", extra={"max_tokens": -1}, stream=False)
        self.assertEqual(result["http_status"], 400)
        self.assertEqual(result["error_type"], "invalid_request_error")
        result = self.request("test", key="deliberately-invalid-key", stream=False)
        self.assertEqual(result["http_status"], 401)

    def test_malformed_usage_does_not_break_summary(self):
        result = self.request("MALFORMED_USAGE", stream=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["usage"], {})
        self.assertIn("request_failed", b.metric_valid(result, self.config, 16000))
        self.assertEqual(b.summarize([result], 1)["failures"], 1)

    def test_compatibility_results_and_report(self):
        with tempfile.TemporaryDirectory() as td:
            recorder = b.Recorder(Path(td) / "run")
            result = b.compatibility(b.Client(self.config), self.config, recorder,
                                     argparse.Namespace(negative_auth=True))
            self.assertEqual(result["status"], "COMPLETED")
            self.assertFalse([t for t in result["tests"] if t["status"] == "FAIL"], result)
            tests = {t["name"]: t for t in result["tests"]}
            self.assertEqual(tests["invalid_auth"]["status"], "PASS")
            self.assertEqual(tests["invalid_temperature"]["status"], "PASS")
            self.assertEqual(tests["thinking_control"]["status"], "NOT_TESTED")
            self.assertEqual(tests["sampling_and_thinking_semantics"]["status"], "MANUAL")
            self.assertTrue(all(t.get("criteria") for t in result["tests"]))
            self.assertTrue(all(t["status"] in {"PASS", "FAIL", "NOT_TESTED", "MANUAL"}
                                for t in result["tests"]))
            b.render_report(result, self.config, recorder.out)
            report = (recorder.out / "report.md").read_text(encoding="utf-8")
            self.assertIn("| Compatibility check | Result | Criteria |", report)
            self.assertIn("| tool_single_stream | PASS |", report)
            self.assertIn("| invalid_temperature | PASS | Invalid parameter returns HTTP 400.", report)
            self.assertNotIn("OBSERVED", report)
            self.assertEqual(b.load_json(recorder.out / "report.json")["tests"], result["tests"])

    def test_compatibility_failures_and_unexecuted_checks(self):
        client = b.Client(self.config)
        request = client.request
        def missing_basic_fields(*args, **kwargs):
            result = request(*args, **kwargs)
            if kwargs.get("label") == "compat-basic_nonstream":
                result.update(finish_reason=None, request_id=None, usage={})
            return result
        with tempfile.TemporaryDirectory() as td:
            recorder = b.Recorder(Path(td) / "run")
            with patch.object(client, "request", side_effect=missing_basic_fields):
                result = b.compatibility(client, {"thinking_variants": {"off": {}}}, recorder,
                                         argparse.Namespace(negative_auth=False))
            tests = {t["name"]: t for t in result["tests"]}
            for name in ("basic_nonstream", "usage_fields", "request_id"):
                self.assertEqual(tests[name]["status"], "FAIL")
            self.assertEqual(tests["stream_sse"]["status"], "PASS")
            self.assertEqual(tests["thinking_off"]["status"], "PASS")
            self.assertEqual(tests["invalid_auth"]["status"], "NOT_TESTED")
            self.assertNotIn("thinking_control", tests)
            self.assertEqual(result["status"], "COMPLETED")
            b.render_report(result, self.config, recorder.out)
            report = (recorder.out / "report.md").read_text(encoding="utf-8")
            self.assertIn("| request_id | FAIL |", report)
            self.assertIn("| invalid_auth | NOT_TESTED |", report)

    def test_cli_with_multiple_references_and_error_exit_semantics(self):
        for errors in (False, True):
            with self.subTest(errors=errors), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                b.write_json(root / "config.json", self.config)
                b.write_json(root / "ref-a.json", reference("One"))
                b.write_json(root / "ref-b.json", reference("Two"))
                rows = [{"target_tokens": 16000, "messages": [{
                    "role": "user", "content": f"MOCK_TOKENS=16000 sample={i}" +
                    (" STREAM_ERROR" if errors else "")}]} for i in range(2)]
                path = root / "prompts.jsonl"
                path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
                args = [sys.executable, str(ROOT / "benchmark.py"), "baseline",
                        "--config", str(root / "config.json"), "--prompts", str(path),
                        "--requests", "2", "--concurrency", "2",
                        "--reference", str(root / "ref-a.json"), "--reference", str(root / "ref-b.json"),
                        "--out", str(root / "run")]
                completed = subprocess.run(args, capture_output=True, text=True, timeout=30,
                                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                self.assertEqual(completed.returncode, 0, completed.stderr)
                report = b.load_json(root / "run/report.json")
                scenario = report["scenarios"][0]
                self.assertEqual(scenario["summary"]["failures"], 2 if errors else 0)
                self.assertEqual(list(scenario["reference_comparisons"]), ["One", "Two"])
                text = (root / "run/report.md").read_text()
                self.assertNotRegex(completed.stdout + text, r"\b(?:PA" + r"SS|FA" + r"IL)\b")
                evidence = [json.loads(line) for line in (root / "run/requests.jsonl").read_text().splitlines()]
                self.assertEqual(len(evidence), 2)
                self.assertNotIn("text", evidence[0])
                self.assertNotIn("tool_calls", evidence[0])
                repeated = subprocess.run(args, capture_output=True, text=True, timeout=30)
                self.assertEqual(repeated.returncode, 2)

    def test_capacity_exhaustion(self):
        data = [{"target_tokens": 16000, "messages": [{"role": "user", "content": "MOCK_TOKENS=16000"}]}]
        with tempfile.TemporaryDirectory() as td:
            recorder = b.Recorder(Path(td) / "run")
            result = b.capacity(b.Client(self.config), self.config, data, recorder,
                                argparse.Namespace(length=16000, concurrency=[1], duration=60, allow_reuse=False))
            self.assertTrue(result["scenarios"][0]["exhausted_unique_prompts"])
            b.render_report(result, self.config, recorder.out)

    def test_cache_probe_and_invalid_pair(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pair = {"schema_version": 1, "cache_type": "message-append",
                    "warm": [{"role": "user", "content": "MOCK_TOKENS=16000"}],
                    "probe": [{"role": "user", "content": "MOCK_TOKENS=16000"},
                              {"role": "assistant", "content": "OK"},
                              {"role": "user", "content": "CACHE_HIT"}],
                    "local_warm_tokens": 14400, "local_probe_tokens": 16000,
                    "local_shared_prefix_tokens": 14400, "local_shared_prefix_ratio": .9}
            b.write_json(root / "pair.json", pair)
            recorder = b.Recorder(root / "run")
            result = b.cache_test(b.Client(self.config), self.config, recorder,
                                  argparse.Namespace(pair=root / "pair.json", wait_seconds=.02))
            self.assertEqual(result["hit_ratio"], .9)
            self.assertGreaterEqual(result["observed_wait_seconds"], .02)
            self.assertEqual(result["comparison_status"], "LIMITED")
            self.assertIn("warm_tokenizer_server_length_mismatch", result["reasons"])
            b.render_report(result, self.config, recorder.out)
            pair["probe"][0]["content"] = "different"
            with self.assertRaises(ValueError):
                b.validate_cache_pair(pair, {})

    def test_cache_warm_error_does_not_send_probe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            warm = [{"role": "user", "content": "STREAM_ERROR"}]
            pair = {"schema_version": 1, "cache_type": "message-append",
                    "warm": warm, "probe": warm + [{"role": "user", "content": "probe"}],
                    "local_warm_tokens": 900, "local_probe_tokens": 1000,
                    "local_shared_prefix_tokens": 900, "local_shared_prefix_ratio": .9}
            b.write_json(root / "pair.json", pair)
            recorder = b.Recorder(root / "run")
            result = b.cache_test(b.Client(self.config), self.config, recorder,
                                  argparse.Namespace(pair=root / "pair.json", wait_seconds=0))
            self.assertFalse(result["probe_success"])
            self.assertEqual(result["comparison_status"], "NO_VALID_SAMPLES")
            self.assertEqual(len(recorder.path.read_text().splitlines()), 1)

    def test_invalid_config(self):
        for extra in ({"timeout_s": float("nan")}, {"length_tolerance": 2},
                      {"api_key": "literal"}, {"max_tokens": True},
                      {"request_extra": {"model": "other"}}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                b.validate_config({**self.config, **extra})


if __name__ == "__main__":
    unittest.main()
