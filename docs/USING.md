# Diagnosing with webR

A playbook, organised by the problem you actually have rather than by the API. If you are
here because something is wrong right now, start at [My agent returned something
wrong](#my-agent-returned-something-wrong).

- [Instrumenting a system for the first time](#instrumenting-a-system-for-the-first-time)
- [My agent returned something wrong](#my-agent-returned-something-wrong)
- [Reading the tree](#reading-the-tree)
- [The wrongness came from data, not a call](#the-wrongness-came-from-data-not-a-call)
- [It only breaks in production](#it-only-breaks-in-production)
- [Teaching webR what "wrong" means for you](#teaching-webr-what-wrong-means-for-you)
- [When the web is incomplete](#when-the-web-is-incomplete)
- [When webR is too slow](#when-webr-is-too-slow)
- [Things webR will not tell you](#things-webr-will-not-tell-you)

---

## Instrumenting a system for the first time

Decorate the boundaries, not everything.

```python
from webrtrace import webR_node

@webR_node
async def planner(task: str) -> str: ...

@webR_node
async def extractor(text: str) -> dict: ...
```

A good first pass is: every agent, every LLM call, every tool call, and the orchestrator.
That is usually five to fifteen decorators for a real system.

**Do not decorate hot helpers.** A string utility called 50,000 times per run adds noise to
the web and cost to the run. If you want it traced anyway, use `@webR_node(capture=False)`
so it costs ~4.5µs instead of ~80µs.

Sanity-check the shape before you trust it:

```python
import webrtrace
run_your_system()
print(webrtrace.render(webrtrace.export_graph()))
```

If the tree looks flat when you expected nesting, your agents are not calling each other
the way you think they are — which is itself a useful discovery.

---

## My agent returned something wrong

### 1. Ask for the failure chains first

```python
web = webrtrace.export_graph()
print(webrtrace.render_failures(web))
```

```
  orchestrator -> worker -> extract_customers
      ValueError: could not parse JSON from model output
  orchestrator -> worker -> llm_call
      suspect: refusal
```

Each line is a root-to-culprit path. **The last name in the chain is where it started.**
Everything to its left is context, not cause.

If there are several chains, read the one with the **shallowest** culprit first — a failure
close to the root usually explains failures further down, and not the other way round.

### 2. If that says nothing, look for taint

```python
print(webrtrace.render(web))
```

An `*` marks a node that succeeded while consuming something that did not. If the
orchestrator is tainted but no node is `[ERR]` or `[SUS]`, something failed and was
swallowed by a `try/except` in your own code. That is the most common shape of a silent
failure, and it is invisible without the taint marker.

### 3. If nothing is marked at all

webR did not catch it, which narrows things usefully. Either:

- **The wrong thing is semantic** — a fluent, correct-looking, false statement. No lexical
  detector can see that. Skip to [teaching webR what "wrong"
  means](#teaching-webr-what-wrong-means-for-you).
- **The failing code is not decorated.** Look at the tree for a gap where you expected a
  node.
- **Capture was off**, so no detector ever ran. Check `webrtrace.is_enabled()` and whether
  the node was declared `capture=False`.

### 4. Follow the content, not the calls

Two nodes with the **same output hash** did not change the content between them.

```python
for node in web["nodes"]:
    io = node.get("io") or {}
    if "output" in io:
        print(f"{node['name']:<24} {io['output']['hash']}  len={io['output']['len']}")
```

Walk down the list. The first row where the hash changes is where the content was
rewritten. This is the fastest way to find *which* node mangled a payload when several
were involved, and it works even though webR never stored the payload itself.

---

## Reading the tree

```
[ ok] * orchestrator                        18.1ms
|- [ ok]   worker                              16.8ms
|  |- [SUS]   llm_call                          16.9ms  suspect=refusal
|  `- [ERR]   extract_customers                  3.4us  ValueError: could not parse JSON
```

| Mark | Meaning | What to do |
|---|---|---|
| `[ ok]` | Returned normally, nothing looked wrong | Nothing |
| `[ERR]` | Raised | Read the exception; this is a real origin |
| `[SUS]` | Returned normally, output looks wrong | Read the `suspect=` reason — this is usually the *actual* start |
| `*` | Tainted: succeeded on top of something that failed | Trace downward to find what it consumed |

`[SUS]` above `[ERR]` in the same branch is the classic pattern: the model refused, and
the parser downstream then failed on the apology. The refusal is the cause; the parse error
is the symptom.

---

## The wrongness came from data, not a call

If a bad value was *produced* in one place and *consumed* somewhere with no call
relationship, the call tree cannot show it. Declare the edge:

```python
@webR_node
def planner():
    return webrtrace.mark(build_plan(), "plan")   # returns the value unchanged

@webR_node
def executor(plan):
    webrtrace.link(plan)                          # records planner -> executor
```

Then `render()` prints a `data dependencies (SENDS)` section, and the edges appear in
`export_graph()["edges"]` with `"kind": "sends"`.

Across a thread, a queue, or a process, pass a token instead:

```python
message = {"payload": data, "webr": webrtrace.origin("job").to_dict()}
# ... on the other side
webrtrace.link(Link.from_dict(message["webr"]))
```

`link()` returns `False` rather than raising when it cannot resolve the source — an
unmarked value, an evicted mark, or no active node. If your edges are missing, check that
return value: silence there means the mark did not survive, usually because more than 2,048
values were marked since.

---

## It only breaks in production

Stream to disk so the trace survives whatever happens to the process:

```python
webrtrace.start_writer("traces/run.jsonl")
```

Failed and suspect nodes are flushed immediately, so the interesting records are durable
the moment they happen. Then read the file with no code at all:

```bash
python -m webrtrace traces/run.jsonl --failures
python -m webrtrace traces/               # a whole directory, across rotated files
```

**Turning tracing on in a process that is already running** is why the toggle is a runtime
check rather than an import-time one:

```python
webrtrace.enable()                  # or set WEBR_ENABLED=0 to start disabled
webrtrace.set_capture(True)
```

For a rare bug you cannot reproduce, capture the full text rather than fingerprints:

```python
webrtrace.set_capture(True, full=True)
```

Be deliberate about that one. Full capture writes prompts verbatim, and prompts contain
customer data. It is the setting most likely to turn a trace file into a liability.

---

## Teaching webR what "wrong" means for you

The built-in detectors are generic. Your domain knowledge is not, and it is much better.

```python
@webR_node(check=lambda out: out.strip().startswith("{"))
def extractor(prompt: str) -> str: ...

def has_a_citation(output: str) -> bool | str:
    if "[" in output:
        return True
    return "answer contained no citation"          # the reason is recorded

@webR_node(check=has_a_citation)
def summarizer(sources: str) -> str: ...
```

A validator **never raises and never changes the return value**. It marks the node
`[SUS]` and records the reason. A hallucination is a call that succeeded, so treating it as
an exception would be modelling it wrongly.

To make a normally-informational signal damning in your pipeline:

```python
webrtrace.set_suspect_signals("refusal", "empty_output", "json_invalid", "novel_numbers")
```

`novel_numbers` is off that list by default because a node that computes a total is
*supposed* to produce a figure nobody passed in. Turn it on for nodes that should only ever
be summarising — and expect false positives if you turn it on globally.

---

## When the web is incomplete

Every export reports its own gaps. Read them before drawing conclusions:

```python
print(web["stats"])
# {'nodes': 412, 'dropped': 1200, 'dangling_edges': 3, ...}
```

| Symptom | Cause | Fix |
|---|---|---|
| `dropped` > 0 | The in-memory buffer evicted nodes | `webrtrace.configure(capacity=500_000)`, or stream to disk |
| `dangling_edges` > 0 | An edge points at a node that was evicted or lives in another rotated file | Read the whole directory: `graph_from_jsonl("traces/")` |
| A node appears as a root when it should have a parent | Same as above, or the parent was never decorated | Check the gap in the tree |
| `write_errors` > 0 in `get_writer().stats()` | The disk write failed | Tracing continued in memory; disk records after that point are lost |
| Many separate `traces` when you expected one | Work crossed a boundary the context could not | Use `webrtrace.submit()` for `ThreadPoolExecutor`, or a `SENDS` token |

Errors, suspects, tainted nodes, **and their ancestor chains** are never evicted by age. If
`dropped` is large but your failure chain is intact, the trace is still trustworthy for
diagnosis — you have lost uneventful successes, which is the point.

---

## When webR is too slow

Overhead is ~4.5µs per call without capture, and ~80µs on a 1KB payload with it.

- Against an LLM call, that is a rounding error. Leave it on.
- Against a tight pure-Python loop, it is not. Narrow it:

```python
@webR_node(capture=False)              # trace the call, skip the payload work
@webR_node(capture=("prompt",))        # capture one parameter, not a large context blob
webrtrace.set_capture(False)           # off globally; turn on only while investigating
webrtrace.set_detectors()              # keep fingerprints, skip the heuristics
```

Measure rather than guess — `python benchmarks/overhead.py` reproduces the table.

---

## Things webR will not tell you

- **Whether an answer is true.** The detectors are lexical. A fluent, well-formatted, false
  sentence looks identical to a correct one. Use `check=` where you can express what
  correct means.
- **Why the model did something.** webR records what crossed the boundary, not the model's
  reasoning.
- **What happened in a process it was not running in.** Cross-process propagation is not
  implemented; only `SENDS` tokens cross that boundary today.
- **Anything about a node you did not decorate.** A gap in the tree is a gap in the
  instrumentation, not evidence that nothing happened there.
