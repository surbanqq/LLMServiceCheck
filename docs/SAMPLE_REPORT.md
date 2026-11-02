# Synthetic report example

All measurements below are fictional; no real service was tested.

# LLMServiceCheck report

- Model: `synthetic-report-example`
- Test: baseline; run status: COMPLETED
- Request evidence: requests.jsonl; full statistics and reference snapshots: report.json.
- Reports describe executed checks; they do not certify production readiness.
- TTFT (Time to First Token), TPOT (Time per Output Token), E2E (End-to-End latency). All timing table values are milliseconds.

Load model: synthetic example; no real service was contacted.
Latency statistics exclude request errors. Partial success can bias latency downward.
Main timing tables use valid samples only; report.json also includes all successful samples.
Throughput uses all successful token counts divided by total wall time, including time spent on errors.
Percentiles use nearest rank; small samples give unstable tail estimates.

## Input 16000 tokens

Requests: 10; completed: 8; errors: 2; valid samples: 8; error rate: 20.00%.

Wall time: 301.00 s; output throughput: 13.61 tokens/s; completed requests/s: 0.03.

| Metric | Measured | Demo A (fictional) baseline | Demo A (fictional) upper limit | Demo A (fictional) x baseline | Demo A (fictional) x upper limit | Demo B (fictional) baseline | Demo B (fictional) upper limit | Demo B (fictional) x baseline | Demo B (fictional) x upper limit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ttft_avg | 2350.00 | 2400.00 | 3600.00 | 0.98 | 0.65 | 2100.00 | - | 1.12 | - |
| ttft_p90 | 2700.00 | 4000.00 | 6000.00 | 0.68 | 0.45 | 3500.00 | 5200.00 | 0.77 | 0.52 |
| tpot_avg | 28.50 | 28.00 | 42.00 | 1.02 | 0.68 | 24.00 | - | 1.19 | - |
| tpot_p50 | 28.00 | 26.00 | 39.00 | 1.08 | 0.72 | 22.00 | - | 1.27 | - |
| tpot_p90 | 32.00 | 40.00 | 60.00 | 0.80 | 0.53 | 34.00 | 51.00 | 0.94 | 0.63 |

- Demo A (fictional) comparison: **LIMITED**; missing_prompt_tokens, request_failed, valid_samples_below_10.
- Demo B (fictional) comparison: **LIMITED**; missing_prompt_tokens, request_failed, valid_samples_below_10.

**Partial sample warning:** ratios describe only the valid subset; they do not represent the entire attempted load.

Sample exclusions: request_failed, missing_prompt_tokens.

Ratios are measured / reference; a value above 1 means longer latency. Missing bands or metrics appear as '-'; no extrapolation is performed.
COMPARABLE means declared conditions matched; LIMITED means descriptive comparison only; NO_VALID_SAMPLES means no timing comparison; NOT_APPLICABLE means no reference band.

Every measurement and reference value in this example is fictional. The example demonstrates partial success and dynamic comparison columns.
