# The webR user guide

A playbook, organised by the problem you actually have rather than by the API. If you are
here because something is wrong right now, skip to [My agent returned something
wrong](#my-agent-returned-something-wrong).

- [See it work in 30 seconds](#see-it-work-in-30-seconds)
- [Instrumenting a system for the first time](#instrumenting-a-system-for-the-first-time)
- [Choosing a suspicion profile](#choosing-a-suspicion-profile)
- [My agent returned something wrong](#my-agent-returned-something-wrong)
- [Reading the tree](#reading-the-tree)
- [A visual report you can share](#a-visual-report-you-can-share)
- [My agent doesn't return text](#my-agent-doesnt-return-text)
- [Where did the tokens go](#where-did-the-tokens-go)
- [The wrongness came from data, not a call](#the-wrongness-came-from-data-not-a-call)
- [It only breaks in production](#it-only-breaks-in-production)
- [Teaching webR what "wrong" means for you](#teaching-webr-what-wrong-means-for-you)
- [When the web is incomplete](#when-the-web-is-incomplete)
- [When webR is too slow](#when-webr-is-too-slow)
- [Things webR will not tell you](#things-webr-will-not-tell-you)

---

## See it work in 30 seconds

The repository ships a runnable demo — a five-agent support-ticket pipeline — with three
modes, so you can see a healthy run, a silently-wrong run, and a loud failure side by side.
No API key, no network.

```bash
python -m demo --mode good      # everything works: your baseline
python -m demo --mode silent    # zero exceptions, wrong answer — the case webR is for
python -m demo --mode fail      # an ordinary crash, for contrast
python -m demo --mode silent --open   # also open the HTML report in a browser
```

The `silent` run is the one to study. A model refuses one ticket (a successful, billed
call that returned nothing), another answer is truncated at `max_tokens`, and an embedder
returns a dead vector — and the program raises nothing and reports success. webR marks all
three and taints the final report that was built on them.

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

## Choosing a suspicion profile

There is **one package and one install.** You do not pick an "LLM version" or a "non-LLM
version" — webR looks at what each node actually returned and runs the right checks: text
output gets the lexical detectors, non-text output gets the value detectors. A pipeline
whose planner returns prose and whose embedder returns a vector gets the right checks on
each, with no configuration.

The one thing webR cannot guess is which signals are *damning in your domain*. An all-zero
vector is a dead embedding in one system and an ordinary sparse row in the next. That
policy is a one-liner:

```python
import webrtrace

webrtrace.set_profile("llm")     # default. Conservative: only signals wrong in any context
webrtrace.set_profile("data")    # ML / embeddings / features: all_zeros, empty, unchanged
webrtrace.set_profile("strict")  # everything, incl. novel_numbers — expect false positives
```

Or set `WEBR_PROFILE=data` in the environment to change policy without touching code. A
profile is only a starting point; `set_suspect_signals(...)` sets a policy of your own. It
changes *which signals accuse a node*, never which detectors run — a signal a profile
leaves informational is still recorded, so nothing is lost, it simply does not mark the
node `[SUS]`.

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

## A visual report you can share

The terminal tree is enough for a quick look. For a large web, a wrong answer you need to
hand to a teammate, or a failure worth attaching to a bug report, write an HTML report:

```python
webrtrace.write_html("report.html")            # from the in-memory buffer
webrtrace.write_html("report.html", web)       # or from a document you already have
```

Or from a trace file, without writing any code:

```bash
python -m webrtrace traces/run.jsonl --html report.html
```

Open it in any browser. It is **one self-contained file** — no server, no network, no CDN,
nothing to install — so it renders on an air-gapped machine and is safe to email. It shows
the same tree, expandable per node for payloads, tokens, and signals; a token total across
the run; and a "only failures, suspects, and taint" filter for finding the problem in a
web with thousands of nodes. Payloads are embedded as inert data, never as script, and a
node name is never trusted as markup — a report is not an injection vector.

If you handle data you may not retain, set `set_capture(True, text=False)` before the run;
the report then shows lengths, hashes, and signals but no readable payload.

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

## My agent doesn't return text

Embedders, scorers, retrievers, and feature transforms return vectors, floats, and lists.
The lexical detectors are useless on those, so when a node's output has no text
representation webR runs a value pass instead: `nan`, `infinite`, `all_zeros`,
`empty_collection`, `unchanged_value`.

```python
@webR_node
def embed(text: str) -> list[float]:
    ...            # returns [0.0] * 1536 when the provider call quietly failed
```

That node is flagged `all_zeros: 1536`. A dead embedding is a silent failure of exactly
the kind an exception-based tool never sees: the vector is the right shape, the pipeline
carries on, and retrieval returns garbage two stages later.

The gate is the **output**, not the whole call. `embed("a prompt")` has text going in and a
vector coming out; the value pass still runs. A prose agent never pays for it.

`nan` and `infinite` mark the node suspect. `all_zeros`, `empty_collection`, and
`unchanged_value` do not — an empty result list is often the correct answer, and a flag
that fires on correct answers is one people learn to ignore. Promote them per pipeline:

```python
webrtrace.set_suspect_signals("nan", "infinite", "all_zeros", "refusal")
```

---

## Where did the tokens go

```python
client = webrtrace.instrument(Anthropic())    # or instrument(OpenAI())
```

Each provider call becomes a node with `usage`: model, input and output tokens, cache
counters, and `stop_reason`. Anthropic and OpenAI clients are both recognised — by shape,
not class, so OpenAI-compatible servers (LiteLLM proxy, vLLM, Together) work too, and
OpenAI's `prompt_tokens`/`completion_tokens` land in the same `Usage` fields as
Anthropic's counts, so a mixed pipeline sums cleanly. Since nodes carry a parent chain,
the cost of one agent *including everything it delegated to* is a walk of its subtree.

OpenAI-specific flags you get for free: `finish_reason: "length"` (truncated) and
`"content_filter"` (output removed after generation, billed anyway) mark the node
suspect, alongside Anthropic's `refusal` and `max_tokens`.

Two things to look for, neither of which raises:

- **`stop_reason: "refusal"`** — a billed call that returned no content. Recorded as
  suspect, with the reason in `signals["suspect"]`.
- **`stop_reason: "max_tokens"`** — the answer was cut off mid-thought and passed
  downstream as if complete. Also suspect.

`cache_read_input_tokens` is kept separate from `input_tokens` deliberately: they are
priced differently, and folding them together would understate a cold run and overstate a
warm one. webR does not convert any of this to money — multiply by your own rates.

Not on a supported SDK? Report it yourself from inside the traced call:

```python
from webrtrace import Usage

@webR_node
def call_local_model(prompt: str) -> str:
    result = my_runtime.generate(prompt)
    webrtrace.record_usage(Usage(model="llama-3-8b",
                                 input_tokens=result.n_prompt,
                                 output_tokens=result.n_gen))
    return result.text
```

---

## webR is doing something odd and I want to see why

webR never prints. Its own faults — a buffer that raises, a writer that cannot reach disk,
a detector that throws — go to the `webrtrace` logger at `WARNING`, once per condition:

```python
import logging
logging.getLogger("webrtrace").addHandler(logging.StreamHandler())
logging.getLogger("webrtrace").setLevel(logging.WARNING)
```

If tracing seems to be missing nodes, this is the first thing to turn on.

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

Overhead is ~12µs per call without capture, and ~214µs on a 1KB payload with it.

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
- **What happened in a process it was not running in, unless you carried the context.**
  `inject()` / `remote_parent()` join a trace across a process boundary; without them, a
  node in another process is simply absent.
- **Anything about a node you did not decorate.** A gap in the tree is a gap in the
  instrumentation, not evidence that nothing happened there.
