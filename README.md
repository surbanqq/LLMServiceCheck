# LLMServiceCheck

A lightweight tool for checking self-hosted large language model services before production deployment.

[Chinese documentation](README.zh_CN.md)

Measure latency, throughput, request errors, prefix-cache reuse and chat interface behavior through an OpenAI-compatible service. Compare measurements with any number of user-supplied references. No vendor reference data or automatic admission verdicts are bundled.

| Subcommand | Purpose |
| --- | --- |
| `prepare` | Generate inputs at selected token lengths and optional cache pairs using a local tokenizer. |
| `baseline` | Measure latency, throughput and request errors by input length, with optional external reference comparisons. |
| `capacity` | Sweep closed-loop concurrency at one input length to observe throughput, latency and errors as load increases. |
| `cache` | Warm a prefix, wait a specified interval and probe cache reuse and latency changes. |
| `compat` | Check nonstreaming and streaming responses, usage, JSON, tool calls and invalid parameter handling. |

## Requirements and installation

- Python 3.10+ on Linux.
- A reachable model service. The client does not need model weights, PyTorch or a GPU.
- Only `prepare` needs tokenizer packages and the served model's local tokenizer files, including its chat template.

Linux:

```bash
bash install.sh
source .venv/bin/activate
```

Or install manually:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

`bash install.sh --runner-only` creates an environment without tokenizer packages for running existing workloads. In the examples below, `python` means the environment's interpreter.

## Configure the service

Copy the example configuration to `configs/local.json`:

```bash
cp configs/example.service.json configs/local.json
```

Set `base_url` to your service prefix, such as `http://127.0.0.1:8000/v1`; the script appends `/chat/completions`. Set `model` and `max_context_tokens` to match your deployment.

Reference values and latency limits are supplied separately through `--reference` files.

```bash
export LLM_API_KEY='your-key'
```

For a service without authentication, set `require_api_key` to `false`.

| Setting | Meaning |
| --- | --- |
| `max_context_tokens` | Actual deployed context limit, including input and output budget |
| `lengths` | Input token lengths for `prepare`; overridden by `--lengths` |
| `max_tokens`, `min_completion_tokens` | Requested output cap and minimum output for valid timing samples |
| `timeout_s`, `read_timeout_s` | Total request deadline and socket operation timeout; defaults in the example are 900 / 300 seconds |
| `length_tolerance` | Allowed relative difference between target and server input counts |
| `require_cold_cache` | Exclude baseline samples with cached input or unknown cache counts; default `true` |
| `allow_reasoning` | Allow reasoning output in timing samples; default `false` |
| `request_extra`, `template_options` | Server request extensions and matching local tokenizer template options |
| `thinking_variants` | Optional named request extensions for compatibility probes; acceptance alone does not verify thinking behavior |

`allow_reasoning` filters samples; it does not switch server thinking on or off. Configure that through the server's supported request extension. For a server supporting `chat_template_kwargs.enable_thinking`, use:

```json
"request_extra": {"chat_template_kwargs": {"enable_thinking": false}},
"template_options": {"enable_thinking": false}
```

Use the same thinking mode and chat template during preparation and measurement. Tokenizer files depend on the model; commonly they include `tokenizer.json` and `tokenizer_config.json`, with additional vocabulary or template files where required.

## Prepare workloads

`--tokenizer` points to a local directory containing the served model's tokenizer and chat template. Copy these files from the deployed model directory, or download the same model revision from Hugging Face. For example, download the Qwen3.8-27B tokenizer files without model weights:

```bash
python -m pip install -U huggingface_hub
hf download Qwen/Qwen3.8-27B \
  tokenizer.json tokenizer_config.json chat_template.jinja merges.txt \
  --local-dir ./models/tokenizer
```

`prepare` reads this directory locally and does not download files automatically.

```bash
python benchmark.py prepare --config configs/local.json \
  --tokenizer ./models/tokenizer \
  --samples 10 --cache-type prefix-replace --cache-tokens 70000 \
  --out workloads/run01
```

Lengths come from configuration `lengths`; use `--lengths` to override them. If neither is set, the default is `4000 8000 16000`. The list must contain distinct integers of at least 1000.

Creates `prompts.jsonl`, `workload_manifest.json`, and, when selected, `cache_pair.json`. The manifest records the lengths actually used. Lengths are **input tokens including the chat template**, not characters. Input plus `max_tokens` must fit the deployed context. Choose lengths for your real traffic and advertised service limits. `baseline` tests the lengths found in the prepared prompts; you do not need to list them again. `--cache-tokens` independently controls the cache-pair length.

| `--cache-type` | Generated pair |
| --- | --- |
| `none` (default) | No cache pair |
| `prefix-replace` | Approximately 90% identical token prefix, with different final text |
| `message-append` | Preserve warm messages byte-for-byte; append assistant and user turns to form an approximately 90% shared token prefix |

`--cache-tokens` is the approximate probe input length, defaulting to 70000.  Both inputs plus their output budgets must fit the context. The pair records local token counts and the verified shared prefix. Wait duration is configured when running the cache check.

Use a new output directory and a fresh preparation for every cold baseline run; a new run identifier is generated automatically. Do not use workloads from a different tokenizer or thinking configuration.

## Run checks

### Compatibility checks (`compat`)

Check structured output, tool calls, usage fields, streaming responses and invalid parameter handling.

```bash
python benchmark.py compat --config configs/local.json --out results/compat01
```

Add `--negative-auth` to test an intentionally invalid key.

### Baseline measurements (`baseline`)

Measurements only:

```bash
python benchmark.py baseline --config configs/local.json \
  --prompts workloads/run01/prompts.jsonl --concurrency 10 --requests 10 \
  --out results/baseline01
```

To compare references, add one or more `--reference` arguments. Before the next cold run, repeat `prepare` with `--out workloads/run02`:

```bash
python benchmark.py baseline --config configs/local.json \
  --prompts workloads/run02/prompts.jsonl --concurrency 10 --requests 10 \
  --reference references/demo-a.json --reference references/demo-b.json \
  --out results/baseline02
```

`demo-a.json` contains Demo A references and `demo-b.json` contains Demo B references. Demo A has no band at or above 200000 input tokens, so 224000 yields `NOT_APPLICABLE`; Demo B covers all six configured (configs/example.service.qwen3.8-27b.json) input lengths. See [reference format and provenance](docs/REFERENCES.md). Reference files are loaded at execution time and saved into `report.json`; changing a file affects later runs only.

### Prefix cache checks (`cache`)

Warm a prefix using a prepared cache pair, wait for the specified interval, then probe cache reuse and latency changes.

```bash
python benchmark.py cache --config configs/local.json \
  --pair workloads/run01/cache_pair.json --wait-seconds 30 --out results/cache01
```

### Capacity checks (`capacity`)

Sweep concurrency at the input length selected by `--length` to measure throughput, latency and errors under increasing load.

```bash
python benchmark.py capacity --config configs/local.json \
  --prompts workloads/run01/prompts.jsonl --length 16000 \
  --concurrency 1 2 4 8 --duration 60 --allow-reuse --out results/capacity01
```

Capacity uses closed-loop concurrency, with `--duration` setting the duration of each level. Later levels reuse earlier inputs; `--allow-reuse` also permits reuse within a level and can warm caches. Without it, a level ends when unique prompts are exhausted. The duration stops new requests, while in-flight requests may continue until completion or timeout.

## Read results

- `report.md`: one timing table per input length, containing measured values and each reference's baseline, upper limit, measured/baseline and measured/limit ratios. Counts, error rate and comparison limitations remain visible.
- `report.json`: full configuration, reference snapshots, all-success statistics, valid-subset statistics and throughput.
- `requests.jsonl`: per-request timing, usage, errors and request identifiers. Prompt/response bodies and tool arguments are omitted.

The main timing tables use valid samples only. Defaults require sufficient output, verified input length, zero cached input, usable stream timing and no observed reasoning. Failed and excluded requests remain in the counts. Partial success can hide the slowest requests; treat subset ratios accordingly. A small sample count cannot establish reliable tail latency.

TTFT (Time to First Token) starts at the client before connection setup and ends at the first content, reasoning or tool delta. `first_content_ms` records visible text separately. TPOT (Time per Output Token) is `(stream completion time - first generation event time) / (completion_tokens - 1)`. It includes stream finalization and is not a measurement of each individual token interval. E2E (End-to-End latency) covers the full request. Percentiles use nearest rank.

`avg` is the arithmetic mean; `p50` is the median; `p90` is the 90th percentile. Socket operations use the smaller of the read timeout and total timeout. Stream heartbeats can keep a socket alive without generated tokens; the total deadline still applies. These timeouts are measurement settings, independent of your reference latency limits.

`combined_tpm_observed` is observed input-plus-output TPM (Tokens per Minute), not an account quota.

Ratios above 1 mean longer latency. `COMPARABLE` means declared reference conditions matched; `LIMITED` means there are caveats; `NO_VALID_SAMPLES` means no timing comparison; `NOT_APPLICABLE` means no matching band. Missing metrics are `-`. Matching conditions do not establish equivalent hardware, model quality or workload difficulty.

Cache hit ratio uses server-reported cached input tokens divided by probe input tokens. The warm request should start with a fresh prefix. A single warm/probe pair does not establish cache lifetime.

Exit codes: `0` means the run finished and a report was written, even with request errors; `2` means a configuration, file or program error; `130` means interruption. Automation should inspect request counts, errors and comparison states. Result and workload directories must be new.

Compatibility reports list each check, its result and its criteria. `PASS` / `FAIL` indicate whether this execution met those criteria, not complete feature support. `NOT_TESTED` means the check was not executed; `MANUAL` means separate verification is required because the script does not automatically check it. Parameter acceptance does not establish its effect. Run status `COMPLETED` means execution finished, not that all checks passed.

## Development

```bash
python -m unittest discover -s tests -q
python benchmark.py --help
python benchmark.py prepare --help
```

Tests use a local fake service and tokenizer fixtures. They validate the harness, not real model performance. Prompts are synthetic numeric text, so complement them with representative application workloads.

See the [report example](docs/SAMPLE_REPORT.md) and [local verification record](docs/VERIFICATION.md).
