#!/usr/bin/env python3
"""LLMServiceCheck: pre-release checks for self-hosted language model services."""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import copy
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import random
import socket
import ssl
import statistics
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

VERSION = "0.1.0"

METRICS = {
    f"{metric}_{stat}": (metric + "_ms", stat)
    for metric in ("ttft", "tpot", "e2e")
    for stat in ("avg", "p50", "p90", "p95", "p99", "max")
}
CACHE_TYPES = ("none", "prefix-replace", "message-append")


def positive_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def validate_lengths(lengths):
    if (not isinstance(lengths, list) or not lengths
            or any(type(n) is not int or n < 1000 for n in lengths)
            or len(set(lengths)) != len(lengths)):
        raise ValueError("lengths must be a nonempty list of distinct integers >= 1000")


def validate_config(config, *, need_key=True):
    if not isinstance(config, dict):
        raise ValueError("Service config must be an object")
    obsolete = {"profile", "reference_profiles", "evaluation_mode", "service_slo", "capacity_slo"}
    if obsolete.intersection(config):
        raise ValueError("Legacy evaluation settings are unsupported; use --reference files")
    if "lengths" in config:
        validate_lengths(config["lengths"])
    if "api_key" in config or "headers" in config:
        raise ValueError("Store credentials in api_key_env, not in the config")
    if not isinstance(config.get("model"), str) or not config["model"].strip():
        raise ValueError("model must be a nonempty service model name")
    if not isinstance(config.get("base_url"), str):
        raise ValueError("base_url is required; include the API prefix, for example /v1")
    for field in ("timeout_s", "read_timeout_s"):
        if not positive_number(config.get(field, 600)):
            raise ValueError(f"{field} must be a finite positive number")
    for field, default in (("max_tokens", 512), ("min_completion_tokens", 128),
                           ("max_context_tokens", 32768)):
        if not valid_count(config.get(field, default)) or config.get(field, default) < 1:
            raise ValueError(f"{field} must be a positive integer")
    if config.get("min_completion_tokens", 128) > config.get("max_tokens", 512):
        raise ValueError("min_completion_tokens cannot exceed max_tokens")
    tolerance = config.get("length_tolerance", .02)
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not 0 <= tolerance < 1:
        raise ValueError("length_tolerance must be in [0, 1)")
    for field in ("require_api_key", "require_cold_cache", "allow_reasoning"):
        if field in config and not isinstance(config[field], bool):
            raise ValueError(f"{field} must be boolean")
    for field in ("request_extra", "template_options", "thinking_variants"):
        if not isinstance(config.get(field, {}), dict):
            raise ValueError(f"{field} must be an object")
    reserved = {"model", "messages", "stream", "stream_options", "max_tokens", "n"}
    if reserved.intersection(config.get("request_extra", {})):
        raise ValueError("request_extra cannot override model/messages/stream/stream_options/max_tokens/n")
    for variant in config.get("thinking_variants", {}).values():
        if not isinstance(variant, dict) or reserved.intersection(variant):
            raise ValueError("thinking_variants must contain request extension objects")
    # Workload preparation validates configuration without requiring a credential.
    checked = dict(config)
    if not need_key:
        checked["require_api_key"] = False
    Client(checked)


def load_references(paths):
    """Load explicit reference data; no implicit references or numeric thresholds."""
    references = []
    for path in paths:
        ref = load_json(path)
        if not isinstance(ref, dict) or set(ref) - {"schema_version", "name", "description", "conditions", "bands"}:
            raise ValueError(f"{path}: invalid reference object or unknown fields")
        if type(ref.get("schema_version")) is not int or ref["schema_version"] != 1:
            raise ValueError(f"{path}: schema_version must be 1")
        if not isinstance(ref.get("name"), str) or not ref["name"].strip():
            raise ValueError(f"{path}: reference name is required")
        if any(r["name"] == ref["name"] for r in references):
            raise ValueError(f"Duplicate reference name: {ref['name']}")
        conditions = ref.get("conditions", {})
        allowed = {"concurrency", "max_tokens", "min_samples", "min_completion_tokens",
                   "require_cold_cache", "allow_reasoning"}
        if not isinstance(conditions, dict) or set(conditions) - allowed:
            raise ValueError(f"{path}: unknown reference conditions")
        for key, value in conditions.items():
            if key in ("require_cold_cache", "allow_reasoning"):
                if not isinstance(value, bool):
                    raise ValueError(f"{path}: {key} must be boolean")
            elif not valid_count(value) or value < 1:
                raise ValueError(f"{path}: {key} must be a positive integer")
        bands = ref.get("bands")
        if not isinstance(bands, list) or not bands:
            raise ValueError(f"{path}: bands must be a nonempty list")
        previous_upper = 0
        for band in bands:
            if not isinstance(band, dict) or set(band) - {"min_input_tokens", "max_input_tokens", "baseline_ms", "limits_ms"}:
                raise ValueError(f"{path}: invalid band or unknown fields")
            lower, upper = band.get("min_input_tokens"), band.get("max_input_tokens")
            band.setdefault("max_input_tokens", None)
            if (not valid_count(lower) or lower < 1 or
                    (upper is not None and (not valid_count(upper) or upper <= lower))):
                raise ValueError(f"{path}: bands use positive lower-inclusive, upper-exclusive bounds")
            if previous_upper is None or lower < previous_upper:
                raise ValueError(f"{path}: bands must be sorted and nonoverlapping")
            previous_upper = upper
            if not band.get("baseline_ms") and not band.get("limits_ms"):
                raise ValueError(f"{path}: band needs baseline_ms or limits_ms")
            for field in ("baseline_ms", "limits_ms"):
                metrics = band.get(field, {})
                if not isinstance(metrics, dict) or set(metrics) - METRICS.keys():
                    raise ValueError(f"{path}: unknown metrics in {field}")
                if any(not positive_number(v) for v in metrics.values()):
                    raise ValueError(f"{path}: reference timings must be finite and positive")
        references.append(copy.deepcopy(ref))
    return references


def band_for(reference, tokens):
    if not valid_count(tokens):
        return None
    return next((b for b in reference["bands"] if tokens >= b["min_input_tokens"] and
                 (b["max_input_tokens"] is None or tokens < b["max_input_tokens"])), None)


def compare_references(references, config, target, rows, args):
    valid = [r for r in rows if not r["validity_issues"]]
    measured = summarize(valid, 0)
    comparisons = {}
    for ref in references:
        band = band_for(ref, target)
        if band is None:
            comparisons[ref["name"]] = {"status": "NOT_APPLICABLE",
                                       "reasons": ["no_reference_band"], "metrics": {}}
            continue
        reasons = sorted({issue for row in rows for issue in row["validity_issues"]})
        if any(band_for(ref, r["usage"].get("prompt_tokens")) != band for r in valid):
            reasons.append("actual_input_in_different_reference_band")
        for key, expected in ref.get("conditions", {}).items():
            if key == "min_samples":
                if len(valid) < expected:
                    reasons.append(f"valid_samples_below_{expected}")
            elif key == "min_completion_tokens":
                if any(r["usage"]["completion_tokens"] < expected for r in valid):
                    reasons.append(f"completion_tokens_below_{expected}")
            elif key == "require_cold_cache":
                if expected and any(r["cached_tokens"] != 0 for r in valid):
                    reasons.append("reference_requires_verified_cold_cache")
            elif key == "allow_reasoning":
                if not expected and any(r["reasoning_seen"] or
                        (r["usage"].get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
                        for r in valid):
                    reasons.append("reference_excludes_reasoning")
            else:
                actual = args.concurrency if key == "concurrency" else config.get("max_tokens", 512)
                if actual != expected:
                    reasons.append(f"{key}_expected_{expected}_actual_{actual}")
        metrics = {}
        for metric in sorted(set(band.get("baseline_ms", {})) | set(band.get("limits_ms", {}))):
            group, statistic = METRICS[metric]
            value = measured[group][statistic]
            base = band.get("baseline_ms", {}).get(metric)
            limit = band.get("limits_ms", {}).get(metric)
            metrics[metric] = {
                "measured_ms": value, "baseline_ms": base, "limit_ms": limit,
                "ratio_to_baseline": value / base if value is not None and base is not None else None,
                "ratio_to_limit": value / limit if value is not None and limit is not None else None,
            }
        comparisons[ref["name"]] = {
            "status": "NO_VALID_SAMPLES" if not valid else ("LIMITED" if reasons else "COMPARABLE"),
            "reasons": sorted(set(reasons)), "valid_samples": len(valid), "metrics": metrics,
        }
    return comparisons


def baseline(client, config, data, recorder, args):
    groups = {}
    for row in data:
        groups.setdefault(row["target_tokens"], []).append(row)
    # Check every group before sending any requests.
    for target, records in groups.items():
        context_guard(config, target)
        if len(records) < args.requests:
            raise ValueError(f"{target}: need {args.requests} distinct prepared prompts")
        for record in records[:args.requests]:
            context_guard(config, record.get("local_prompt_tokens", target))
    scenarios = []
    for target, records in sorted(groups.items()):
        selected = records[:args.requests]
        barrier = threading.Barrier(min(args.concurrency, len(selected)))

        def run(item):
            index, record = item
            if index < barrier.parties:
                barrier.wait(timeout=30)
            row = client.request(messages_for(record), label=f"baseline-{target}", target=target)
            row["validity_issues"] = metric_valid(row, config, target)
            recorder.add(row)
            return row

        started = time.perf_counter()
        with futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            rows = list(pool.map(run, enumerate(selected)))
        summary = summarize(rows, time.perf_counter() - started)
        valid = [r for r in rows if not r["validity_issues"]]
        scenarios.append({
            "target_tokens": target, "valid_samples": len(valid), "summary": summary,
            "valid_summary": summarize(valid, summary["wall_seconds"]),
            "invalid_reasons": sorted({i for r in rows for i in r["validity_issues"]}),
            "reference_comparisons": compare_references(args.references, config, target, rows, args),
        })
        print(f"Input {target} tokens: completed {summary['success']}/{len(rows)}, "
              f"valid {len(valid)}/{len(rows)}, errors {summary['failures']}/{len(rows)}", flush=True)
    return {"mode": "baseline", "status": "COMPLETED", "concurrency": args.concurrency,
            "scenarios": scenarios, "references": args.references,
            "load_model": "closed-loop, bounded concurrency, first-wave barrier, no retries"}


def make_cache_pair(args, fit, token_ids, rng):
    target = args.cache_tokens
    if args.cache_type == "prefix-replace":
        common, _ = fit(round(target * .9), f"cache-run={args.run_id};\n", "\nReference material.\n")
        def replaced_tail(label):
            source = "".join(f"{rng.getrandbits(64):016x} " for _ in range(target))

            def messages(n):
                return [{"role": "system", "content": SYSTEM},
                        {"role": "user", "content": common[1]["content"] + label + source[:n] + "\nSay OK."}]

            if len(token_ids(messages(len(source)))) < target:
                raise ValueError("Insufficient cache tail source for tokenizer")
            lo, hi = 0, len(source)
            while lo < hi:
                mid = (lo + hi) // 2
                if len(token_ids(messages(mid))) < target:
                    lo = mid + 1
                else:
                    hi = mid
            return messages(lo)

        warm, probe = replaced_tail("tail-A\n"), replaced_tail("tail-B\n")
    else:
        prefix, _ = fit(round(target * .9), f"cache-run={args.run_id};\n", "\nReference material.")
        warm = [{"role": "user", "content": prefix[0]["content"] + "\n" + prefix[1]["content"]}]
        # Keep the stable user message byte-for-byte. Fit appended turns to ~10%.
        source = "".join(f"{rng.getrandbits(64):016x} " for _ in range(target))

        def append(n):
            return warm + [{"role": "assistant", "content": "Reference received."},
                           {"role": "user", "content": source[:n] + "\nSay OK."}]

        desired = math.ceil(len(token_ids(warm)) / .9)
        if len(token_ids(append(len(source)))) < desired:
            raise ValueError("Insufficient cache append source for tokenizer")
        lo, hi = 0, len(source)
        while lo < hi:
            mid = (lo + hi) // 2
            if len(token_ids(append(mid))) < desired:
                lo = mid + 1
            else:
                hi = mid
        probe = append(lo)
    warm_ids, probe_ids = token_ids(warm), token_ids(probe)
    shared = 0
    for a, b in zip(warm_ids, probe_ids):
        if a != b:
            break
        shared += 1
    fraction = shared / len(probe_ids)
    if not .88 <= fraction <= .92:
        raise ValueError(f"Tokenizer-verified shared prefix must be near 90%, observed {fraction:.3f}")
    return {"schema_version": 1, "cache_type": args.cache_type, "warm": warm, "probe": probe,
            "local_warm_tokens": len(warm_ids), "local_probe_tokens": len(probe_ids),
            "local_shared_prefix_tokens": shared, "local_shared_prefix_ratio": fraction,
            "run_id": args.run_id}


def validate_cache_pair(pair, config):
    if pair.get("schema_version") != 1 or pair.get("cache_type") not in CACHE_TYPES[1:]:
        raise ValueError("Use a cache pair generated by prepare --cache-type")
    for name in ("warm", "probe"):
        messages = pair.get(name)
        if not isinstance(messages, list) or not messages or any(
                not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant")
                or not isinstance(m.get("content"), str) for m in messages):
            raise ValueError(f"Invalid cache {name} messages")
        local = pair.get(f"local_{name}_tokens")
        if not valid_count(local) or local < 1:
            raise ValueError(f"Missing tokenizer-verified local_{name}_tokens")
        context_guard(config, local)
    if pair["warm"] == pair["probe"]:
        raise ValueError("Warm and probe must differ")
    if pair["cache_type"] == "message-append" and pair["probe"][:len(pair["warm"])] != pair["warm"]:
        raise ValueError("message-append must preserve all warm messages")
    shared = pair.get("local_shared_prefix_tokens")
    if not valid_count(shared) or shared > min(pair["local_warm_tokens"], pair["local_probe_tokens"]):
        raise ValueError("Invalid shared prefix token count")
    fraction = shared / pair["local_probe_tokens"]
    if not .88 <= fraction <= .92 or abs(fraction - pair.get("local_shared_prefix_ratio", -1)) > 1e-9:
        raise ValueError("Invalid shared prefix ratio; regenerate cache workload")


def cache_test(client, config, recorder, args):
    pair = load_json(args.pair)
    validate_cache_pair(pair, config)
    warm = client.request(pair["warm"], label="cache-warm")
    recorder.add(warm)
    result = {"mode": "cache", "status": "COMPLETED", "cache_type": pair["cache_type"],
              "warm_success": warm["ok"], "probe_success": False, "hit_ratio": None,
              "requested_wait_seconds": args.wait_seconds, "observed_wait_seconds": None,
              "local_shared_prefix_ratio": pair["local_shared_prefix_ratio"], "reasons": []}
    if not warm["ok"]:
        result["comparison_status"] = "NO_VALID_SAMPLES"
        result["reasons"].append("warm_request_failed; probe_not_sent")
        return result
    started = time.perf_counter()
    # Short waits keep cancellation responsive.
    while time.perf_counter() - started < args.wait_seconds:
        time.sleep(max(0, min(1, args.wait_seconds - (time.perf_counter() - started))))
    result["observed_wait_seconds"] = time.perf_counter() - started
    probe = client.request(pair["probe"], label="cache-probe")
    recorder.add(probe)
    p, hit = probe["usage"].get("prompt_tokens"), probe["cached_tokens"]
    if valid_count(p) and p > 0 and valid_count(hit) and hit <= p:
        result["hit_ratio"] = hit / p
    else:
        result["reasons"].append("missing_or_invalid_cache_usage")
    for name, row in (("warm", warm), ("probe", probe)):
        actual = row["usage"].get("prompt_tokens")
        local = pair[f"local_{name}_tokens"]
        if not valid_count(actual) or abs(actual - local) / local > config.get("length_tolerance", .02):
            result["reasons"].append(f"{name}_tokenizer_server_length_mismatch")
    if warm["cached_tokens"] is None:
        result["reasons"].append("warm_cache_state_unknown")
    elif warm["cached_tokens"] > 0:
        result["reasons"].append("warm_request_already_cached")
    if not probe["ok"]:
        result["reasons"].append("probe_request_failed")
    result.update(probe_success=probe["ok"], warm_prompt_tokens=warm["usage"].get("prompt_tokens"),
                  probe_prompt_tokens=p, cached_tokens=hit)
    result["comparison_status"] = "LIMITED" if result["reasons"] else "MEASURED"
    return result


def capacity(client, config, data, recorder, args):
    context_guard(config, args.length)
    selected = [r for r in data if r["target_tokens"] == args.length]
    if not selected:
        raise ValueError("No prepared prompts for --length")
    for record in selected:
        context_guard(config, record.get("local_prompt_tokens", args.length))
    scenarios = []
    for concurrency in args.concurrency:
        cursor = 0
        exhausted = False
        lock = threading.Lock()
        started = time.perf_counter()
        deadline = started + args.duration

        def worker():
            nonlocal cursor, exhausted
            rows = []
            while time.perf_counter() < deadline:
                with lock:
                    index = cursor
                    cursor += 1
                if index >= len(selected) and not args.allow_reuse:
                    exhausted = True
                    break
                record = selected[index % len(selected)]
                row = client.request(record["messages"], label=f"capacity-c{concurrency}", target=args.length)
                row["validity_issues"] = metric_valid(row, config, args.length)
                recorder.add(row)
                rows.append(row)
            return rows

        with futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            rows = [row for batch in pool.map(lambda _: worker(), range(concurrency)) for row in batch]
        summary = summarize(rows, time.perf_counter() - started)
        valid = [r for r in rows if not r["validity_issues"]]
        scenarios.append({"concurrency": concurrency, "summary": summary,
                          "valid_samples": len(valid), "valid_summary": summarize(valid, summary["wall_seconds"]),
                          "exhausted_unique_prompts": exhausted,
                          "invalid_reasons": sorted({i for r in rows for i in r["validity_issues"]}),
                          "cache_reuse_allowed": args.allow_reuse})
        print(f"Concurrency {concurrency}: completed {summary['success']}/{len(rows)}, "
              f"errors {summary['failures']}, output {summary['output_tokens_s']:.2f} tokens/s", flush=True)
    return {"mode": "capacity", "status": "COMPLETED", "target_tokens": args.length,
            "load_model": "closed-loop, bounded concurrency, no retries",
            "note": "Later concurrency levels reuse earlier prompts and may be cache-warm. "
                    "Duration stops new arrivals; in-flight requests may finish after it.",
            "scenarios": scenarios}

def context_guard(config, target):
    limit = config.get("max_context_tokens")
    if limit and target + config.get("max_tokens", 512) > limit:
        raise ValueError(f"Input {target} + output budget exceeds deployed context limit {limit}")


SYSTEM = "You are a helpful assistant."
OUTPUT_TASK = (
    "\nUse the preceding material as reference. Produce a detailed numbered analysis "
    "of at least 1500 words. Keep generating until the output limit. "
    "Do not answer with a short summary."
)


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def percentile(values, p):
    """Nearest rank percentile; no interpolation."""
    if not values:
        return None
    v = sorted(values)
    return v[max(0, math.ceil(len(v) * p) - 1)]


def stats(values):
    return {"n": len(values), "avg": statistics.mean(values) if values else None,
            "p50": percentile(values, .5), "p90": percentile(values, .9),
            "p95": percentile(values, .95), "p99": percentile(values, .99),
            "max": max(values) if values else None}


def cached_tokens(usage):
    if not isinstance(usage, dict):
        return None
    val = usage.get("cached_tokens")
    if val is None:
        val = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    return val if isinstance(val, int) and not isinstance(val, bool) and val >= 0 else None


def valid_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_sse(response):
    """Yield decoded SSE events, supporting comments, CRLF, multiline data and UTF-8."""
    data = []
    while True:
        line = response.readline(8 * 1024 * 1024 + 1)
        if len(line) > 8 * 1024 * 1024:
            raise ValueError("SSE line exceeds 8 MiB")
        if not line:
            if data:
                yield "\n".join(data)
            return
        line = line.decode("utf-8").rstrip("\r\n")
        if line == "":
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))


class Client:
    def __init__(self, config):
        self.config = config
        u = urlsplit(config["base_url"])
        if u.scheme not in ("http", "https") or not u.hostname or u.query or u.fragment:
            raise ValueError("base_url must be an http(s) URL without query or fragment")
        if u.username or u.password:
            raise ValueError("Do not put credentials in base_url; use api_key_env")
        self.url = u
        self.path = u.path.rstrip("/") + "/chat/completions"
        self.key = os.environ.get(config.get("api_key_env", "LLM_API_KEY"), "")
        if not self.key and config.get("require_api_key", True):
            raise ValueError("API key environment variable is empty")

    def request(self, messages, *, extra=None, stream=True, key=None, label="", target=None):
        c = self.config
        body = {"model": c["model"], "messages": messages,
                "max_tokens": c.get("max_tokens", 512), "stream": stream}
        body.update(copy.deepcopy(c.get("request_extra", {})))
        if extra:
            body.update(copy.deepcopy(extra))
        # Tests own these fields; explicitly prevent accidentally testing another model.
        body["model"], body["messages"], body["stream"] = c["model"], messages, stream
        if stream:
            body["stream_options"] = {"include_usage": True}
        else:
            body.pop("stream_options", None)
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"}
        actual_key = self.key if key is None else key
        if actual_key:
            headers["Authorization"] = "Bearer " + actual_key
        start = time.perf_counter()
        result = {"label": label, "target_tokens": target, "ok": False, "http_status": None,
                  "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  "request_body_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                  "ttft_ms": None, "first_content_ms": None, "e2e_ms": None, "tpot_ms": None,
                  "stream_span_tpot_ms": None,
                  "usage": {}, "cached_tokens": None, "generation_events": 0,
                  "reasoning_seen": False, "finish_reason": None, "request_id": None,
                  "done": False, "error": None, "sse_errors": [], "text": "", "tool_calls": [],
                  "chunk_objects_valid": True, "content_type": None}
        conn = None
        timer = None
        expired = threading.Event()
        try:
            timeout = float(c.get("timeout_s", 600))
            cls = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
            kwargs = {"timeout": min(timeout, float(c.get("read_timeout_s", 120)))}
            if self.url.scheme == "https":
                kwargs["context"] = ssl.create_default_context(cafile=c.get("ca_file"))
            conn = cls(self.url.hostname, self.url.port, **kwargs)
            conn.connect()
            wire_socket = conn.sock
            def abort_at_deadline():
                expired.set()
                try:
                    wire_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(max(.001, timeout - (time.perf_counter() - start)), abort_at_deadline)
            timer.daemon = True
            timer.start()
            conn.request("POST", self.path, body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                         headers=headers)
            response = conn.getresponse()
            result["http_status"] = response.status
            result["request_id"] = response.getheader("x-request-id") or response.getheader("request-id")
            result["content_type"] = response.getheader("content-type", "")
            if response.status != 200:
                # Preserve error structure without persisting potential credentials or prompt echoes.
                raw = response.read(65536)
                try:
                    err = json.loads(raw).get("error", {})
                    result["error_type"] = err.get("type") if isinstance(err, dict) else None
                    result["error_code"] = err.get("code") if isinstance(err, dict) else None
                except (ValueError, AttributeError):
                    pass
                raise ValueError(f"HTTP {response.status}")
            first, last = None, None
            calls = {}
            if stream:
                if "text/event-stream" not in result["content_type"].lower():
                    raise ValueError("stream response Content-Type is not text/event-stream")
                for data in parse_sse(response):
                    now = time.perf_counter()
                    if now - start > timeout:
                        raise TimeoutError("request exceeded total timeout")
                    if data.strip() == "[DONE]":
                        result["done"] = True
                        break
                    event = json.loads(data)
                    if event.get("error"):
                        raise ValueError("API returned an error inside SSE stream")
                    if event.get("object") != "chat.completion.chunk":
                        result["chunk_objects_valid"] = False
                    if event.get("request_id"):
                        result["request_id"] = event["request_id"]
                    if isinstance(event.get("usage"), dict):
                        result["usage"] = event["usage"]
                    for choice in event.get("choices", []):
                        if choice.get("index", 0) != 0:
                            continue
                        delta = choice.get("delta") or {}
                        content = delta.get("content") or ""
                        reason = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        tools = delta.get("tool_calls") or []
                        if reason:
                            result["reasoning_seen"] = True
                        if content or reason or tools:
                            first = now if first is None else first
                            last = now
                            result["generation_events"] += 1
                        if content:
                            if result["first_content_ms"] is None:
                                result["first_content_ms"] = (now - start) * 1000
                            result["text"] += content
                        for t in tools:
                            idx = t.get("index", 0)
                            merged = calls.setdefault(idx, {"id": "", "type": "function",
                                                             "function": {"name": "", "arguments": ""}})
                            if t.get("id"):
                                merged["id"] = t["id"]
                            for field in ("name", "arguments"):
                                merged["function"][field] += t.get("function", {}).get(field, "")
                        if choice.get("finish_reason") is not None:
                            result["finish_reason"] = choice["finish_reason"]
                result["tool_calls"] = list(calls.values())
                if first is not None:
                    result["ttft_ms"] = (first - start) * 1000
                if not result["done"]:
                    result["sse_errors"].append("missing [DONE]")
                if result["finish_reason"] is None:
                    result["sse_errors"].append("missing finish_reason")
                if first is None:
                    result["sse_errors"].append("no generated content/reasoning/tool delta")
            else:
                raw = response.read(int(c.get("max_response_bytes", 16 * 1024 * 1024)) + 1)
                if len(raw) > c.get("max_response_bytes", 16 * 1024 * 1024):
                    raise ValueError("response too large")
                event = json.loads(raw)
                if event.get("error"):
                    raise ValueError("API error in HTTP 200 response")
                result["usage"] = event.get("usage") or {}
                result["request_id"] = result["request_id"] or event.get("request_id")
                choices = event.get("choices") or []
                if choices:
                    message = choices[0].get("message") or {}
                    result["text"] = message.get("content") or ""
                    result["tool_calls"] = message.get("tool_calls") or []
                    result["reasoning_seen"] = bool(message.get("reasoning_content") or message.get("reasoning"))
                    result["finish_reason"] = choices[0].get("finish_reason")
                else:
                    raise ValueError("missing choices")
            usage = result["usage"]
            if not isinstance(usage, dict) or any(
                    usage.get(field) is not None and not isinstance(usage[field], dict)
                    for field in ("prompt_tokens_details", "completion_tokens_details")):
                result["usage"] = {}
                raise ValueError("Malformed usage object")
            result["cached_tokens"] = cached_tokens(result["usage"])
            n = result["usage"].get("completion_tokens")
            if stream and valid_count(n) and n > 1 and first is not None and last is not None:
                # Service-side tokens + client-side event times. Not per-token ITL.
                result["stream_span_tpot_ms"] = (last - first) * 1000 / (n - 1)
                result["tpot_ms"] = (time.perf_counter() - first) * 1000 / (n - 1)
            if expired.is_set() or time.perf_counter() - start > timeout:
                raise TimeoutError("total request deadline exceeded")
            result["ok"] = not result["sse_errors"]
        except Exception as exc:
            # Do not include arbitrary exception strings which may echo secrets.
            result["error"] = f"{type(exc).__name__}: request failed"
        finally:
            if timer is not None:
                timer.cancel()
            if conn is not None:
                conn.close()
            result["e2e_ms"] = (time.perf_counter() - start) * 1000
        return result


def messages_for(record):
    return record["messages"]


def public_config(config):
    # Configuration intentionally contains only names of key env vars, never actual keys.
    return {k: v for k, v in config.items() if k not in ("api_key", "headers")}


class Recorder:
    def __init__(self, out):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=False)
        self.lock = threading.Lock()
        self.path = self.out / "requests.jsonl"

    def add(self, result):
        row = copy.deepcopy(result)
        # Content is only needed transiently for compatibility validation.
        row.pop("text", None)
        row.pop("tool_calls", None)
        with self.lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")


def metric_valid(r, config, target):
    issues = []
    usage = r["usage"]
    actual = usage.get("prompt_tokens")
    if not r["ok"]:
        issues.append("request_failed")
    if not valid_count(actual) or actual < 1:
        issues.append("missing_prompt_tokens")
    elif abs(actual - target) / target > config.get("length_tolerance", .02):
        issues.append("input_length_outside_tolerance")
    if not valid_count(usage.get("completion_tokens")) or usage.get("completion_tokens", 0) < config.get("min_completion_tokens", 128):
        issues.append("output_too_short_or_missing_usage")
    if config.get("require_cold_cache", True) and r["cached_tokens"] is None:
        issues.append("missing_cached_tokens")
    elif config.get("require_cold_cache", True) and r["cached_tokens"] > 0:
        issues.append("cache_hit_in_cold_baseline")
    if not r.get("chunk_objects_valid", True):
        issues.append("invalid_chunk_object")
    if not config.get("allow_reasoning", False) and (r["reasoning_seen"] or (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)):
        issues.append("reasoning_makes_tpot_incomparable")
    if r["tpot_ms"] is None or r["generation_events"] < 2:
        issues.append("insufficient_stream_events")
    return issues


def summarize(rows, elapsed):
    good = [r for r in rows if r["ok"]]
    tokens = sum(r["usage"].get("completion_tokens", 0) for r in good
                 if valid_count(r["usage"].get("completion_tokens")))
    inputs = sum(r["usage"].get("prompt_tokens", 0) for r in good
                 if valid_count(r["usage"].get("prompt_tokens")))
    return {"requests": len(rows), "success": len(good), "failures": len(rows) - len(good),
            "error_rate": (len(rows) - len(good)) / len(rows) if rows else None,
            "wall_seconds": elapsed, "request_rps": len(good) / elapsed if elapsed else 0,
            "output_tokens_s": tokens / elapsed if elapsed else 0,
            "input_tokens_s": inputs / elapsed if elapsed else 0,
            "combined_tpm_observed": (tokens + inputs) * 60 / elapsed if elapsed else 0,
            "ttft_ms": stats([r["ttft_ms"] for r in good if r["ttft_ms"] is not None]),
            "tpot_ms": stats([r["tpot_ms"] for r in good if r["tpot_ms"] is not None]),
            "e2e_ms": stats([r["e2e_ms"] for r in good])}


def load_prompts(path):
    data = [json.loads(line) for line in Path(path).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if not data or any(not isinstance(r, dict) or not valid_count(r.get("target_tokens")) or r["target_tokens"] <= 0
                       or not isinstance(r.get("messages"), list) or not r["messages"] for r in data):
        raise ValueError("Each prompt needs positive target_tokens and nonempty messages")
    if any(not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant")
           or not isinstance(m.get("content"), str) for r in data for m in r["messages"]):
        raise ValueError("Prepared workloads require text messages with system/user/assistant roles")
    if any("local_prompt_tokens" in r and (
            not valid_count(r["local_prompt_tokens"]) or r["local_prompt_tokens"] < 1) for r in data):
        raise ValueError("local_prompt_tokens must be a positive integer when present")
    hashes = [hashlib.sha256(json.dumps(r["messages"], sort_keys=True).encode()).hexdigest() for r in data]
    if len(hashes) != len(set(hashes)):
        raise ValueError("Duplicate prompts would contaminate cold baseline/cache capacity testing")
    return data


def prepare(args):
    config = load_json(args.config) if getattr(args, "config", None) else {}
    if config:
        validate_config(config, need_key=False)
    if args.lengths is None:
        args.lengths = config.get("lengths", [4000, 8000, 16000])
    validate_lengths(args.lengths)
    for target in args.lengths:
        context_guard(config, target)
    # Imported only for workload construction. Never trust_remote_code.
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True, trust_remote_code=False)
    rng = random.Random(args.seed)
    template_options = (load_json(args.template_options) if args.template_options
                        else config.get("template_options", {}))
    if set(template_options) & {"tokenize", "add_generation_prompt", "return_dict", "return_tensors"}:
        raise ValueError("template_options cannot override tokenizer output format or generation prompt")
    def token_ids(messages):
        # Explicit output format works across Transformers versions. Some versions
        # default to a mapping; len(mapping) counts fields, not tokens.
        encoded = tok.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True,
            return_dict=False, **template_options)
        if hasattr(encoded, "keys"):
            encoded = encoded["input_ids"]
        if not isinstance(encoded, (list, tuple)) or any(
                not isinstance(x, int) for x in encoded):
            raise ValueError("Expected one flat token-ID list from chat template")
        return encoded
    def count(messages):
        return len(token_ids(messages))
    def fit(target, prefix, suffix):
        # Varied numeric records prevent a single repeated sentence from dominating the workload.
        lines = []
        chars = 0
        source = ""
        def msg(n):
            return [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prefix + source[:n] + suffix}]
        rough = target * 5
        for attempt in range(7):
            while chars < rough:
                line = f"{rng.getrandbits(64):016x} record={rng.randrange(10**9)} value={rng.random():.8f}\n"
                lines.append(line)
                chars += len(line)
            source = "".join(lines)
            if count(msg(len(source))) >= target:
                break
            rough *= 2
        else:
            raise ValueError("Cannot reach target token length after source expansion; check tokenizer/chat template for truncation")
        lo, hi = 0, len(source)
        while lo < hi:
            mid = (lo + hi) // 2
            if count(msg(mid)) < target:
                lo = mid + 1
            else:
                hi = mid
        messages = msg(lo)
        actual = count(messages)
        if abs(actual - target) > max(16, target * .005):
            raise ValueError("Tokenizer length fitting did not converge")
        return messages, actual
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    with (out / "prompts.jsonl").open("w", encoding="utf-8") as f:
        for target in args.lengths:
            for i in range(args.samples):
                nonce = f"cold-run={args.run_id}; length={target}; sample={i}; nonce={rng.getrandbits(128):032x}\n"
                messages, actual = fit(target, nonce, OUTPUT_TASK)
                context_guard(config, actual)
                f.write(json.dumps({"target_tokens": target, "local_prompt_tokens": actual,
                                    "messages": messages}, ensure_ascii=False) + "\n")
            print(f"prepared {target} x {args.samples}", flush=True)
    if args.cache_type != "none":
        pair = make_cache_pair(args, fit, token_ids, rng)
        context_guard(config, pair["local_warm_tokens"])
        context_guard(config, pair["local_probe_tokens"])
        write_json(out / "cache_pair.json", pair)
    write_json(out / "workload_manifest.json", {
        "tokenizer": str(Path(args.tokenizer).resolve()), "template_options": template_options,
        "lengths": args.lengths, "samples": args.samples, "seed": args.seed,
        "run_id": args.run_id, "version": VERSION, "cache_type": args.cache_type,
        "cache_tokens": args.cache_tokens,
        "warning": "Regenerate with a new run-id before each cold run. Confirm lengths against server usage."})


def compatibility(client, config, recorder, args):
    tests = []
    simple = [{"role": "user", "content": "Reply with exactly OK."}]
    def check(name, messages, extra, validate, *, criteria, stream=False, key=None):
        r = client.request(messages, extra=extra, stream=stream, key=key, label="compat-" + name)
        recorder.add(r)
        try:
            passed = bool(validate(r))
        except (KeyError, ValueError, TypeError, IndexError):
            passed = False
        tests.append({"name": name, "status": "PASS" if passed else "FAIL", "criteria": criteria,
                      "http_status": r["http_status"], "error_type": r.get("error_type"),
                      "error_code": r.get("error_code")})
        return r
    def usage_ok(r):
        return all(valid_count(r["usage"].get(k)) for k in ("prompt_tokens", "completion_tokens")) and r["cached_tokens"] is not None
    r = check("basic_nonstream", simple, {"max_tokens": 128}, lambda r: r["ok"] and r["finish_reason"] is not None,
              criteria="Request succeeds and returns a finish_reason.")
    tests.append({"name": "usage_fields", "status": "PASS" if usage_ok(r) else "FAIL",
                  "criteria": "Basic nonstream response reports valid prompt/completion token counts and cached input tokens."})
    check("stream_sse", simple, {"max_tokens": 128},
          lambda r: r["ok"] and r["done"] and r["chunk_objects_valid"] and usage_ok(r),
          criteria="Stream succeeds with a DONE marker, valid chunk objects and token usage.", stream=True)
    tests.append({"name": "request_id", "status": "PASS" if r["request_id"] else "FAIL",
                  "criteria": "Basic nonstream response includes a request identifier recognized by the client."})
    variants = config.get("thinking_variants")
    for mode, extra in (variants or {}).items():
        check("thinking_" + mode, simple, {"max_tokens": 128, **extra}, lambda r: r["ok"],
              criteria="Request with this thinking variant succeeds; its effect is not verified.")
    if not variants:
        tests.append({"name": "thinking_control", "status": "NOT_TESTED",
                      "criteria": "No thinking_variants configured; parameter acceptance was not tested."})
    check("sampling_parameters_accepted", simple, {"max_tokens": 32, "temperature": .6, "top_p": .8},
          lambda r: r["ok"],
          criteria="Request with temperature and top_p succeeds; their effects are not verified.")
    def json_ok(r):
        value = json.loads(r["text"])
        return r["ok"] and isinstance(value, dict) and value.get("ok") is True
    check("json_mode", [{"role": "user", "content": 'Return the JSON object {"ok":true} and no other text.'}],
          {"max_tokens": 128, "response_format": {"type": "json_object"}}, json_ok,
          criteria="Request succeeds and returns a parseable JSON object with ok=true.")
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "Get city weather",
              "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                             "required": ["city"], "additionalProperties": False}}}]
    def tool_ok(r, minimum):
        calls = r["tool_calls"]
        return r["ok"] and r["finish_reason"] == "tool_calls" and len(calls) >= minimum and all(
            t.get("id") and t.get("type") == "function" and t["function"]["name"] == "get_weather"
            and isinstance(json.loads(t["function"]["arguments"]).get("city"), str) for t in calls)
    for stream in (False, True):
        check("tool_single_" + ("stream" if stream else "nonstream"),
              [{"role": "user", "content": "Use get_weather to get weather for Beijing."}],
              {"max_tokens": 256, "tools": tools,
               "tool_choice": {"type": "function", "function": {"name": "get_weather"}}},
              lambda r: tool_ok(r, 1),
              criteria="Request succeeds with finish_reason=tool_calls and at least one valid get_weather call with a string city.",
              stream=stream)
    check("tool_parallel", [{"role": "user", "content": "Get weather for Beijing and Shanghai; call both tools in parallel."}],
          {"max_tokens": 512, "tools": tools, "tool_choice": "required", "parallel_tool_calls": True},
          lambda r: tool_ok(r, 2),
          criteria="Request succeeds with finish_reason=tool_calls and at least two valid get_weather calls with string cities.")
    for name, extra in [("invalid_max_tokens", {"max_tokens": -1}),
                        ("invalid_temperature", {"temperature": -1})]:
        check(name, simple, extra, lambda r: r["http_status"] == 400,
              criteria="Invalid parameter returns HTTP 400.")
    if args.negative_auth:
        check("invalid_auth", simple, {}, lambda r: r["http_status"] == 401,
              criteria="Deliberately invalid API key returns HTTP 401.",
              key="service-check-deliberately-invalid-" + uuid.uuid4().hex)
    else:
        tests.append({"name": "invalid_auth", "status": "NOT_TESTED",
                      "criteria": "Not executed; enable --negative-auth to check for HTTP 401 with an invalid key."})
    tests.extend([{"name": "429_rate_limit_semantics", "status": "MANUAL",
                   "criteria": "Separately verify rate-limit responses and retry behavior; no automatic check."},
                  {"name": "sampling_and_thinking_semantics", "status": "MANUAL",
                   "criteria": "Separately verify that sampling and thinking parameters affect generation; no automatic check."},
                  {"name": "json_schema_and_multiturn_tool_results", "status": "MANUAL",
                   "criteria": "Separately verify JSON Schema enforcement and tool-result continuation across turns; no automatic check."}])
    return {"mode": "compat", "status": "COMPLETED",
            "tests": tests, "note": "Parameter acceptance does not verify its effect. Tool checks require finish_reason=tool_calls."}


def cell(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def number(value):
    return "-" if value is None else f"{value:.2f}"


def render_report(result, config, out):
    """Write a portable Markdown report and a self-contained machine-readable snapshot."""
    snapshot = {"version": VERSION, "config": public_config(config), **result}
    write_json(Path(out) / "report.json", snapshot)
    lines = ["# LLMServiceCheck report", "",
             f"- Model: `{cell(config['model'])}`",
             f"- Test: {result['mode']}; run status: {result['status']}",
             "- Request evidence: requests.jsonl; full statistics and reference snapshots: report.json.",
             "- Reports describe executed checks; they do not certify production readiness.",
             "- TTFT (Time to First Token), TPOT (Time per Output Token), "
             "E2E (End-to-End latency). All timing table values are milliseconds.",
             ""]
    if result["mode"] in ("baseline", "capacity"):
        lines += [f"Load model: {result['load_model']}.",
                  "Latency statistics exclude request errors. Partial success can bias latency downward.",
                  "Main timing tables use valid samples only; report.json also includes all successful samples.",
                  "Throughput uses all successful token counts divided by total wall time, including time spent on errors.",
                  "Percentiles use nearest rank; small samples give unstable tail estimates.", ""]
        for scenario in result["scenarios"]:
            label = (f"Input {scenario['target_tokens']} tokens" if result["mode"] == "baseline"
                     else f"Concurrency {scenario['concurrency']} / input {result['target_tokens']} tokens")
            summary = scenario["summary"]
            valid = scenario["valid_summary"]
            n = summary["requests"]
            error_rate = summary["error_rate"]
            lines += [f"## {label}", "",
                      f"Requests: {n}; completed: {summary['success']}; "
                      f"errors: {summary['failures']}; valid samples: {scenario['valid_samples']}; "
                      f"error rate: {'-' if error_rate is None else f'{error_rate:.2%}'}.", "",
                      f"Wall time: {summary['wall_seconds']:.2f} s; "
                      f"output throughput: {summary['output_tokens_s']:.2f} tokens/s; "
                      f"completed requests/s: {summary['request_rps']:.2f}.", ""]
            refs = scenario.get("reference_comparisons", {})
            metrics = {"ttft_avg", "ttft_p90", "tpot_avg", "tpot_p50", "tpot_p90"}
            for ref in refs.values():
                metrics.update(ref["metrics"])
            columns = ["Metric", "Measured"]
            for name in refs:
                columns += [f"{cell(name)} baseline", f"{cell(name)} upper limit",
                            f"{cell(name)} x baseline", f"{cell(name)} x upper limit"]
            lines += ["| " + " | ".join(columns) + " |",
                      "| " + " | ".join("---" for _ in columns) + " |"]
            order = {key: i for i, key in enumerate(METRICS)}
            for metric in sorted(metrics, key=lambda k: order[k]):
                group, statistic = METRICS[metric]
                cells = [metric, number(valid[group][statistic])]
                for comparison in refs.values():
                    m = comparison["metrics"].get(metric, {})
                    cells += [number(m.get("baseline_ms")), number(m.get("limit_ms")),
                              number(m.get("ratio_to_baseline")), number(m.get("ratio_to_limit"))]
                lines.append("| " + " | ".join(cells) + " |")
            lines += [""]
            for name, comparison in refs.items():
                reasons = ", ".join(comparison["reasons"]) or "declared conditions matched"
                lines += [f"- {cell(name)} comparison: **{comparison['status']}**; {cell(reasons)}."]
            if scenario["valid_samples"] < n:
                lines += ["", "**Partial sample warning:** ratios describe only the valid subset; "
                          "they do not represent the entire attempted load."]
            if scenario["invalid_reasons"]:
                lines += ["", "Sample exclusions: " + cell(", ".join(scenario["invalid_reasons"])) + "."]
            if scenario.get("exhausted_unique_prompts"):
                lines += ["", "Unique prompts exhausted before the requested duration ended."]
            lines += [""]
        lines += ["Ratios are measured / reference; a value above 1 means longer latency. "
                  "Missing bands or metrics appear as '-'; no extrapolation is performed.",
                  "COMPARABLE means declared conditions matched; LIMITED means descriptive comparison only; "
                  "NO_VALID_SAMPLES means no timing comparison; NOT_APPLICABLE means no reference band.", ""]
    elif result["mode"] == "compat":
        lines += ["PASS means this execution met the check's stated criteria. FAIL means it did not; "
                  "inspect requests.jsonl for request evidence. Passing a check does not establish complete feature support.",
                  "NOT_TESTED means the check was not executed. MANUAL means separate verification is required; "
                  "the script does not automatically check it.",
                  "COMPLETED describes run completion, not whether all checks passed.", "",
                  "| Compatibility check | Result | Criteria |", "| --- | --- | --- |"]
        for test in result["tests"]:
            lines.append(f"| {cell(test['name'])} | {cell(test['status'])} | {cell(test['criteria'])} |")
        lines += [""]
    else:
        lines += ["| Cache measurement | Value |", "| --- | --- |"]
        for key, value in result.items():
            if key not in ("mode", "status"):
                lines.append(f"| {cell(key)} | {cell(json.dumps(value, ensure_ascii=False))} |")
        lines += ["", "Hit ratio = server-reported cached input tokens / probe input tokens. "
                  "The requested wait is measured from completion of the warm request. "
                  "One probe does not establish cache lifetime or behavior under concurrent load.", ""]
    if result.get("note"):
        lines += [result["note"], ""]
    (Path(out) / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"LLMServiceCheck {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="Generate workloads using a locally stored tokenizer")
    prep.add_argument("--tokenizer", required=True, help="Local tokenizer directory; model weights are unnecessary")
    prep.add_argument("--config", help="Service context limit, input lengths and tokenizer template settings; no key required")
    prep.add_argument("--lengths", nargs="+", type=int,
                      help="Input token lengths; overrides config lengths (fallback: 4000 8000 16000)")
    prep.add_argument("--samples", type=int, default=10, help="Distinct prompts per input length")
    prep.add_argument("--seed", type=int, default=42)
    prep.add_argument("--run-id", default=uuid.uuid4().hex, help="Fresh default identifier isolates cold runs")
    prep.add_argument("--template-options", help="Path to tokenizer template arguments as JSON (JavaScript Object Notation)")
    prep.add_argument("--cache-type", choices=CACHE_TYPES, default="none",
                      help="none: no pair; prefix-replace: shared ~90%% token prefix with replaced tail; "
                           "message-append: unchanged warm messages followed by assistant/user turns. "
                           "Both cache modes create cache_pair.json alongside prompts.jsonl")
    prep.add_argument("--cache-tokens", type=int, default=70000,
                      help="Approximate probe input length for a cache pair (default: 70000)")
    prep.add_argument("--out", required=True, help="New workload directory")
    for name in ("baseline", "capacity", "cache", "compat"):
        command = sub.add_parser(name, help={
            "baseline": "Measure latency by input length and compare optional reference files",
            "capacity": "Measure closed-loop throughput at selected concurrency levels",
            "cache": "Measure prefix-cache reuse after an explicit wait",
            "compat": "Observe chat, streaming, tools, usage and error behavior",
        }[name])
        command.add_argument("--config", required=True)
        command.add_argument("--out", required=True, help="New result directory; existing evidence is never overwritten")
        if name in ("baseline", "capacity"):
            command.add_argument("--prompts", required=True, help="Prepared prompts.jsonl")
        if name == "baseline":
            command.add_argument("--concurrency", type=int, default=10)
            command.add_argument("--requests", type=int, default=10, help="Requests per input length")
            command.add_argument("--reference", action="append", default=[], metavar="FILE",
                                 help="Reference JSON (JavaScript Object Notation) file; repeat for multiple references; omit for measurements only")
        elif name == "capacity":
            command.add_argument("--concurrency", nargs="+", type=int, default=[1, 2, 4, 8])
            command.add_argument("--length", type=int, default=16000)
            command.add_argument("--duration", type=float, default=60, help="Seconds accepting new work per level")
            command.add_argument("--allow-reuse", action="store_true",
                                 help="Reuse inputs when exhausted; later requests may hit cache")
        elif name == "cache":
            command.add_argument("--pair", required=True, help="Prepared cache_pair.json")
            command.add_argument("--wait-seconds", type=float, required=True,
                                 help="Wait after warm response before probe; independent of read timeout")
        else:
            command.add_argument("--negative-auth", action="store_true", help="Also send an intentionally invalid key")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            if args.samples < 1:
                raise ValueError("samples must be >= 1")
            if args.cache_tokens < 1000:
                raise ValueError("cache-tokens must be >= 1000")
            prepare(args)
            return 0
        config = load_json(args.config)
        validate_config(config)
        for field in ("duration", "wait_seconds"):
            value = getattr(args, field, None)
            if value is not None and (not math.isfinite(value) or value < 0 or (field == "duration" and value == 0)):
                raise ValueError(f"{field} must be finite and {'positive' if field == 'duration' else 'nonnegative'}")
        if hasattr(args, "requests") and args.requests < 1:
            raise ValueError("requests must be positive")
        levels = getattr(args, "concurrency", 1)
        if any(n < 1 for n in (levels if isinstance(levels, list) else [levels])):
            raise ValueError("concurrency must be positive")
        if hasattr(args, "reference"):
            args.references = load_references(args.reference)
        data = load_prompts(args.prompts) if hasattr(args, "prompts") else None
        if args.command == "cache":
            validate_cache_pair(load_json(args.pair), config)
        recorder = Recorder(args.out)
        client = Client(config)
        if args.command == "baseline":
            result = baseline(client, config, data, recorder, args)
        elif args.command == "capacity":
            result = capacity(client, config, data, recorder, args)
        elif args.command == "cache":
            result = cache_test(client, config, recorder, args)
        else:
            result = compatibility(client, config, recorder, args)
        render_report(result, config, args.out)
        print(f"Report: {Path(args.out).resolve() / 'report.md'}", flush=True)
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Completed request records remain in requests.jsonl.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())


