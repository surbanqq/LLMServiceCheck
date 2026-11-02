# Reference file format

Reference files are JSON (JavaScript Object Notation) documents loaded by repeated `baseline --reference FILE` arguments. No references are loaded implicitly. Complete snapshots are embedded in `report.json`.

`references/demo-a.json` and `references/demo-b.json` use the same format. Each file defines a named reference with comparison conditions and latency values grouped by input token length.

Demo A bands are [10000, 32000), [32000, 64000), [64000, 128000) and [128000, 200000). Demo B adds [200000, infinity). Thus both cover 16000, 48000, 64000, 100000 and 160000; only Demo B covers 224000. Input lengths outside the defined bands yield `NOT_APPLICABLE`; no extrapolation is applied.

| Field | Meaning |
| --- | --- |
| `schema_version` | Reference format version; currently `1` |
| `name` | Reference name displayed in reports, such as `demo-a` or `demo-b` |
| `description` | Text describing the reference and its applicable setup |
| `conditions` | Optional conditions checked when comparing measurements |
| `bands` | Input token length intervals and their reference metrics |
| `bands[].min_input_tokens` | Inclusive lower bound of an interval |
| `bands[].max_input_tokens` | Exclusive upper bound; `null` means no upper bound |
| `bands[].baseline_ms` | Reference latency metrics in milliseconds |
| `bands[].limits_ms` | Latency upper limits in milliseconds |

The following JSON illustrates the format; its numeric values are fictional:

```json
{
  "schema_version": 1,
  "name": "My measured reference",
  "description": "Document the hardware, model, tokenizer, output mode, workload and source here.",
  "conditions": {
    "concurrency": 10,
    "max_tokens": 512,
    "min_samples": 10,
    "min_completion_tokens": 128,
    "require_cold_cache": true,
    "allow_reasoning": false
  },
  "bands": [
    {
      "min_input_tokens": 1000,
      "max_input_tokens": 32000,
      "baseline_ms": {"ttft_p90": 2000, "tpot_p90": 30},
      "limits_ms": {"ttft_p90": 3000, "tpot_p90": 45}
    },
    {
      "min_input_tokens": 32000,
      "max_input_tokens": null,
      "baseline_ms": {"ttft_p90": 5000, "tpot_p90": 40}
    }
  ]
}
```

- Names must be nonempty and unique within a run.
- Bands must be sorted and nonoverlapping. Lower bounds are inclusive; upper bounds are exclusive. `null` means no upper bound and is allowed only in the last band. Gaps are allowed and yield `NOT_APPLICABLE`.
- A band needs `baseline_ms`, `limits_ms`, or both. All supplied values must be finite positive milliseconds.
- Metric keys combine `ttft`, `tpot` or `e2e` with `avg`, `p50`, `p90`, `p95`, `p99` or `max`, such as `ttft_p95`. Unknown fields and metric names are rejected to catch spelling mistakes.
- `conditions` is optional; omitted conditions are unchecked. Explicit conditions are preferable for meaningful comparisons.
- `concurrency` and `max_tokens` must match the run. `min_samples` counts valid samples, and `min_completion_tokens` checks each valid sample's actual output. A true `require_cold_cache` requires zero reported cache usage. A false `allow_reasoning` excludes observed reasoning.
- `max_tokens` describes the requested output budget, not a guarantee that every request generated that many tokens.
- The main table's measured timing uses the same valid subset for every reference. If a server-reported input length falls outside its intended reference band, the comparison becomes `LIMITED`.
- `measured / baseline` describes latency relative to reference measurements. `measured / upper limit` describes latency relative to your configured admission ceiling. No admission verdict is generated. Missing values remain absent.
- Any request errors or exclusions make applicable comparisons `LIMITED`; all-invalid runs use `NO_VALID_SAMPLES`.

TTFT (Time to First Token) and TPOT (Time per Output Token) definitions must match your source before interpreting ratios. E2E (End-to-End latency) is also supported. Record uncheckable assumptions, such as hardware, tokenizer, workload type and quantization, in `description`. A matching declared condition set cannot establish semantic or quality equivalence between models.
