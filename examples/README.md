# Examples

Every example runs with no API key, no network, and no configuration. The "agents" are
plain functions with hard-coded outputs, so the behaviour is deterministic and you can
read exactly what produced each result.

```bash
pip install -e .            # from the repo root
python examples/01_hello_web.py
```

| | What it shows |
|---|---|
| [`01_hello_web.py`](01_hello_web.py) | The minimum: three agents, one call chain, and what a node actually stores |
| [`02_catching_hallucinations.py`](02_catching_hallucinations.py) | All eight detectors firing — and which ones webR deliberately refuses to treat as guilt |
| [`03_the_silent_failure.py`](03_the_silent_failure.py) | **Start here.** A run with zero exceptions and a wrong answer, and how webR names the origin |
| [`04_links_across_a_queue.py`](04_links_across_a_queue.py) | Data dependencies the call stack cannot see, in-process and across a thread |
| [`05_across_processes.py`](05_across_processes.py) | A real worker process joining the caller's trace, and a failure chain that crosses the boundary |

## Reading a trace from disk

Any example can stream to disk by adding one line:

```python
webrtrace.start_writer("traces/run.jsonl")
```

Then read it back without writing any code:

```bash
python -m webrtrace traces/run.jsonl              # the whole web
python -m webrtrace traces/run.jsonl --failures   # just the chains that broke
python -m webrtrace traces/ --json > web.json     # raw document, across rotated files
```

## How to read the tree

```
[ ok] * orchestrator                        18.1ms
|- [ ok]   worker                              17.8ms
|  |- [SUS]   llm_call                          16.9ms  suspect=refusal
|  `- [ERR]   extract_customers                  3.4us  ValueError: could not parse JSON
```

| Mark | Meaning |
|---|---|
| `[ ok]` | Completed normally, and nothing looked wrong |
| `[ERR]` | Raised. The exception type and message follow the duration |
| `[SUS]` | Returned normally, but a validator or detector believes the output is wrong |
| `*` | **Tainted** — this node succeeded but consumed something that failed downstream of it |

The `*` is the one worth internalising. In `03_the_silent_failure.py` the worker catches
the exception and returns a fallback, so it reports success — and is still marked, because
the answer it produced was built on a failure.
