# Local verification

Version: 0.1.0. Verified on October 30, 2026 with Python 3.12 on Windows.

```bash
python -m unittest discover -s tests -q
```

Result: 27 tests completed successfully.

Coverage includes:

- Real local HTTP (Hypertext Transfer Protocol) requests and SSE (Server-Sent Events) parsing.
- Streaming completion, usage, reasoning, tool calls and malformed or incomplete responses.
- Read timeout after response headers but before the first generated token.
- Total deadline even while stream heartbeats arrive.
- Multiple external references, interval boundaries, missing bands and malformed reference data.
- Valid-subset statistics for partial success and all-error runs.
- Dynamic Markdown tables, report snapshots and request evidence without response bodies.
- Both cache workload methods with tokenizer fixtures, including short inputs and mapping-shaped tokenizer results.
- Cache wait measurement, failed warm requests and tokenizer/server count discrepancies.
- Capacity prompt exhaustion and protection against overwriting existing outputs.
- Running preparation without credentials and running measurement commands as subprocesses.

The tests do not require tokenizer package installation or model weights. They use a character-tokenizer fixture and a local fake service. Real model tokenization, package installation, Linux execution, hardware capacity and model performance have not been verified in this environment. The included workflow is configured to run the tests on Linux and Windows with Python 3.10 and 3.12; those remote runs have not been executed here.

The example report in `SAMPLE_REPORT.md` is entirely synthetic.
