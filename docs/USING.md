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

**If the tree is too big to read**, collapse it. A run where an orchestrator calls an agent
forty times is forty nodes, which is the right thing to store and the wrong thing to look
at:

```python
print(webrtrace.render(webrtrace.collapse_by_agent(webrtrace.export_graph())))
```
```
collapsed from 17 invocations | 3 nodes | 2 edges | 1 trace(s) | 2 ok | 1 error

[ ok] * orchestrator                       872.4us  (max 872.4us)
`- [ERR]   worker x8                          171.2us  (1 err, max 63.0us)
   `- [ ok]   llm_call x8                          5.1us  (max 1.5us)
```

The worst status always wins, so one failure among forty successes still reads as `[ERR]`.
From the CLI: `python -m webrtrace traces/ --collapse`. Each collapsed node keeps the
original `node_ids`, so you can drop back to the raw document to see which invocation it
was.

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

### The process hung or was killed

A node that never returns emits no terminal record — but if a writer was streaming, it
left a start marker, so the stuck node shows up as `running`:

```
[...] * orchestrator
`- [...]   vector_db_query        <-- running: started, never finished
```

`stats["running"]` counts them. This is the case a conventional tracer misses entirely: the
one node that was actually stuck is the one that would otherwise be absent. Requires a
writer (`start_writer(...)`) — without a durable stream there is nothing to read after the
process dies.

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

**`mark()`/`link()` only work on objects with a stable identity — containers, custom
objects.** Strings, numbers, booleans, and `None` are *not* linkable this way: Python
interns them, so `"done" is "done"` and `0 is 0` are `True` for values that different
agents produced independently, and keying on that would invent edges between unrelated
work. `mark()` on such a value returns it unchanged but records nothing. To link a string
or a number, use a token instead — `origin()` on one side, `link(token)` on the other — so
the connection rests on an explicit node id rather than a memory address.

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

Scrub payloads before they are recorded — this runs before the hash, before the detectors,
and before anything touches memory or disk:

```python
webrtrace.set_redactor(webrtrace.common_secrets)     # API keys, tokens, emails, cards
webrtrace.set_redactor(my_own_scrubber)              # anything you actually must remove
```

If a redactor raises, the payload is **dropped rather than stored unredacted**, and
`redaction_failed` shows up in that node's signals. `common_secrets` catches things with a
distinctive shape; it does not catch names, addresses, or medical detail. See
[SECURITY.md](../SECURITY.md#redaction).

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

**Return `True` to pass — nothing else counts as passing.** `False`, `None`, `0`, and `""`
all mark the node suspect. In particular a validator that falls off the end without a
`return` returns `None` and flags every node. That is deliberate: a validator you *thought*
was checking something and which silently passes everything is the exact failure this tool
exists to catch, so the safe default is to fail loudly. If you want a check that abstains,
return `True`.

To make a normally-informational signal damning in your pipeline:

```python
webrtrace.set_suspect_signals("refusal", "empty_output", "json_invalid", "novel_numbers")
```

`novel_numbers` is off that list by default because a node that computes a total is
*supposed* to produce a figure nobody passed in. Turn it on for nodes that should only ever
be summarising — and expect false positives if you turn it on globally.

**One more caveat on `novel_numbers`.** Inputs are joined and then sampled head-and-tail to
bound scan cost, so on a large input the middle is not read. A figure that appears only in
that unread middle will look fabricated when it is not. The node's signals carry
`detection_truncated` whenever this sampling happened — treat `novel_numbers` as
indicative rather than authoritative on those nodes.

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
| `pins_dropped` > 0 | More than `pinned_capacity` failures occurred; the oldest were evicted | Raise `pinned_capacity`, or stream to disk |
| `detection_truncated` on a node | The payload exceeded the detector scan window and was sampled head-and-tail | Treat that node's signals as indicative, not authoritative — see the note on `novel_numbers` below |
| Many separate `traces` when you expected one | Work crossed a boundary the context could not | `webrtrace.submit()` for `ThreadPoolExecutor`; `inject()`/`remote_parent()` across processes; a `SENDS` token for a pure data hand-off |

Errors, suspects, tainted nodes, **and their ancestor chains** are protected from *age*
eviction — a failure at minute two survives an hour of subsequent successes. They are not
protected unconditionally: the pinned store has its own ceiling (`pinned_capacity`,
default 10,000) and drops the oldest pinned record when it fills, counting each one in
`pins_dropped`. A run that fails in a loop will eventually evict its earliest failures,
because otherwise a pathological run would defeat the memory ceiling entirely.

So: if `dropped` is large, `pins_dropped` is zero, and your failure chain is intact, the
trace is trustworthy for diagnosis — you have lost uneventful successes, which is the
point. If `pins_dropped` is non-zero, raise `pinned_capacity` or stream to disk, because
you are now losing failures too.

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
