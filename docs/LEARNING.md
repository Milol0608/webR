# Understanding webR

A guided tour for the person who designed this library and now wants to understand how it
actually works. It assumes you can read Python but not that you know its darker corners —
every concept is explained from the ground up, with a real-world comparison and, more
usefully, a **counter-example**: what breaks if you do it the obvious way instead.

[INTERNALS.md](INTERNALS.md) is the reference for someone maintaining the code. This is the
one that teaches.

**Contents**

1. [What this library actually does](#1-what-this-library-actually-does)
2. [The six ideas you need](#2-the-six-ideas-you-need)
3. [What you decided, and what each decision cost](#3-what-you-decided-and-what-each-decision-cost)
4. [A guided read of the source](#4-a-guided-read-of-the-source)
5. [Six mistakes and what they taught](#5-six-mistakes-and-what-they-taught)
6. [Test yourself](#6-test-yourself)
7. [Glossary](#7-glossary)
8. [Questions worth asking](#8-questions-worth-asking)

---

## 1. What this library actually does

Imagine a kitchen with five cooks. An order comes in. The head chef splits it up, hands
pieces to different cooks, and each cook may hand work to another. Twenty minutes later a
plate goes out and it is **wrong** — but nobody dropped anything, nobody shouted, and every
cook says they did their part.

To find out what happened you would want a record of who handed what to whom. Not the food
itself — just the chain of custody.

That is webR. Each function you mark becomes a **node**. Each "who called whom" becomes an
**edge**. The result is a graph — a web — showing the flow. When the final answer is wrong,
you read the web backwards to find the first place things went bad.

The reason this is hard, and why the library is 2,000 lines rather than 50:

- **Nobody crashes.** When an AI model makes something up, your program does not error. It
  returns a perfectly ordinary string that happens to be false. Every normal debugging tool
  reports success.
- **The cooks work simultaneously.** Five agents running at once must not get their
  "who called me" wires crossed.
- **You cannot store everything.** Prompts are huge. Store them all and you run out of
  memory; store none and the trace tells you nothing.
- **Watching must not change what happens.** If the act of tracing can make your program
  crash, throw a different error, or behave differently, the tool is worse than useless.

That last one is the rule everything else serves.

---

## 2. The six ideas you need

Each one: the idea in plain language, a real-world comparison, a tiny example you can run,
**what goes wrong without it**, and where to find it in webR.

---

### 2.1 Functions are objects, and decorators wrap them

**The idea.** In Python a function is a value, like a number or a string. You can put it in
a variable, pass it to another function, and return it from one.

```python
def greet(name):
    return f"hello {name}"

f = greet          # no parentheses: the function itself, not its result
print(f("world"))  # hello world
```

A **decorator** uses that. It is a function that takes your function and hands back a
*different* function — usually one that does something extra, then calls yours.

```python
def loud(func):
    def wrapper(name):        # a brand-new function
        print("about to run")
        result = func(name)   # call the original
        print("finished")
        return result
    return wrapper            # hand back the new one

@loud                         # shorthand for: greet = loud(greet)
def greet(name):
    return f"hello {name}"

greet("world")
```

`@loud` means *"replace `greet` with `loud(greet)`"*. The name `greet` now points at
`wrapper`. When anyone calls `greet`, they get the wrapper, which quietly calls the real one
in the middle.

The wrapper "remembers" `func` even after `loud` has finished running. A function that
remembers variables from where it was created is called a **closure**. That memory is how
`wrapper` still knows which function to call.

**Real-world comparison.** A receptionist at the front desk. Visitors ask for you; the
receptionist logs the time, sends them through, logs when they leave. You do your job
unchanged, and there is now a record.

**Counter-example — what breaks without care.** A wrapper is a *different* function, so by
default it has a different name and description:

```python
@loud
def greet(name):
    """Say hello."""
    return f"hello {name}"

print(greet.__name__)   # 'wrapper'  <- not 'greet'
print(greet.__doc__)    # None       <- the docstring is gone
```

Your function has been replaced by an impostor wearing no name tag. Anything that inspects
functions — test frameworks, web frameworks deciding how to route a request, documentation
tools — now sees `wrapper` instead of `greet`. `functools.wraps` copies the identity across
and fixes it. **webR uses it on all four wrappers**; without it, decorating a function could
break a framework that was working fine.

**Where in webR.** `decorator.py`. Read `_wrap_sync` first — about twenty lines, and the
whole idea in miniature.

---

### 2.2 `contextvars` — how a function knows who called it

**The idea.** webR needs each function to know its caller. The obvious approach is a global
variable holding "who is running right now."

```python
current = None            # a plain global

def traced(func):
    def wrapper(*args):
        global current
        parent = current  # who called me?
        current = func.__name__
        result = func(*args)
        current = parent  # put it back
        return result
    return wrapper
```

This works fine for one thing at a time. It falls apart the moment two things run at once.

**Real-world comparison.** One shared whiteboard in a busy kitchen. With one cook, writing
"currently making: soup" is fine. With five cooks all writing on the same board, everyone
reads whatever the last person wrote. The information is worse than useless — it is
confidently wrong.

`contextvars` is a whiteboard that **automatically photocopies itself** for each new task.
Every task gets its own copy. Writing on yours never touches anyone else's.

```python
import asyncio, contextvars

current = contextvars.ContextVar("current", default=None)

async def worker(name):
    current.set(name)          # writes to THIS task's copy only
    await asyncio.sleep(0.1)
    print(name, "sees", current.get())

async def main():
    await asyncio.gather(worker("a"), worker("b"), worker("c"))

asyncio.run(main())
# a sees a
# b sees b
# c sees c
```

Swap `ContextVar` for a plain global and run it again — every worker reports whichever name
was set last. That single difference is why webR can trace ten concurrent agents and get
every parent right.

**Counter-example — where it silently stops working.** The automatic copying happens for
`asyncio` tasks. It does **not** happen for every kind of background work:

| How you run it | Does the context follow? |
|---|---|
| `await something()` | yes |
| `asyncio.gather(...)` | yes |
| `asyncio.to_thread(fn)` | yes |
| `ThreadPoolExecutor.submit(fn)` | **no** |
| A separate process | **no** |

The dangerous part is that nothing errors. The work runs; it just looks like it had no
caller, so the web shows a disconnected node with no explanation. That is why
`webrtrace.submit()` exists — it copies the context by hand. And there is a test,
`test_plain_executor_submit_orphans_the_worker`, whose entire job is to **document the
broken behaviour** so nobody assumes it works.

**Where in webR.** `propagation.py`, the `ContextVarPropagator` class.

---

### 2.3 Mutable vs immutable — why records are frozen

**The idea.** Some Python values can be changed after they are created (mutable) and some
cannot (immutable).

```python
scores = [1, 2, 3]     # a list: mutable
scores.append(4)       # fine, the list itself changed

name = "hello"         # a string: immutable
name.upper()           # does NOT change name; it returns a new string
print(name)            # still 'hello'
```

A normal Python object is mutable — you can reassign its attributes. webR's records are
deliberately made **frozen**, meaning any attempt to change one raises an error.

**Real-world comparison.** A printed receipt versus a whiteboard. Once printed, the receipt
says what it says. You can hand it to someone across the room and be certain it will still
say the same thing when they read it.

**Counter-example — the bug this prevents.** webR builds a record and hands it to a
background worker that writes it to a file. Suppose records were editable and something
changed one while the worker was mid-write:

```
main thread:    record.status = "error"
writer thread:  ...halfway through writing {"status": "ok", ...
result:         a file containing a record that never existed
```

Two things touching one object at the same time, with unpredictable results, is called a
**race condition** — and it produces corruption that appears randomly and is nearly
impossible to reproduce. Freezing the record makes it impossible by construction.

**The surprising consequence.** Because records cannot be changed after they are created,
every piece of information must be ready *before* creation. The original plan was for the
background worker to analyse payloads and add the results later — which frozen records make
impossible. The whole analysis step had to move earlier. That reversal is written up in
[ADR 0002](adr/0002-inline-detection.md). A small decision about data shape forced a large
one about architecture.

**Where in webR.** `records.py`, the `NodeRecord` class.

---

### 2.4 Generators — functions that hand back one thing at a time

**The idea.** A normal function computes everything and returns once. A **generator** hands
back values one at a time, pausing between them.

```python
def normal():
    return [1, 2, 3]        # builds the whole list, returns once

def generating():
    yield 1                 # hand back 1, then PAUSE here
    yield 2
    yield 3

for value in generating():
    print(value)            # 1, 2, 3
```

`yield` is the pause. The function's state is frozen mid-execution and resumes where it left
off next time you ask for a value. This matters for AI systems because streaming responses —
text arriving word by word — are generators.

**Real-world comparison.** A vending machine, not a shopping bag. A bag hands you everything
at once. A vending machine gives you one item each time you press the button, and does
nothing in between.

**The trap.** Calling a generator function runs **none** of its body:

```python
def generating():
    print("body running!")
    yield 1

g = generating()        # prints nothing! the body has not started
next(g)                 # NOW it prints "body running!"
```

For webR this is critical. A naive wrapper would time the call that creates the generator —
which does nothing, taking microseconds — and everything the generator actually does later
would be blamed on whoever consumed it. So webR's generator wrapper stays involved across
the generator's whole life.

**Counter-example — the bug an outside reviewer found in our code.** A generator supports
four operations, not one:

| Operation | Meaning |
|---|---|
| `next(g)` | give me the next value |
| `g.send(x)` | here is a value, and give me the next one |
| `g.throw(exc)` | raise this exception *inside* the generator |
| `g.close()` | you are done, shut down |

Our first version handled `next` and `close` and quietly dropped `throw`. That broke real
code:

```python
@webR_node
def stream():
    try:
        yield 1
    except ValueError as e:
        yield f"caught: {e}"     # this generator RECOVERS from the error

g = stream()
next(g)
g.throw(ValueError("x"))
# without tracing: returns "caught: x"
# with our broken version: the ValueError escaped, the recovery never ran
```

Adding tracing changed what the program did — the one rule webR must never break. **When
you wrap something, you must forward its entire behaviour, not just the parts you happen to
use.** Both wrappers are now complete. See `tests/test_review_findings.py`.

**Where in webR.** `decorator.py`, `_wrap_generator`. Read it last; it is the hardest code
in the library.

---

### 2.5 Threads — doing the slow part somewhere else

**The idea.** A **thread** is a second line of execution inside your program. Two things
happen at once, sharing the same memory.

webR needs one because writing to a file is slow. If it wrote the trace while your agent
waited, tracing would make your program slower — so records go into a queue, and a
background thread drains the queue and writes to disk. Your agent never waits.

**Real-world comparison.** A courier. Instead of walking to the post office yourself, you
drop mail in a tray. Someone else empties the tray. You keep working.

**Counter-example #1 — the deadlock that is genuinely surprising.** Threads come in two
kinds. A **non-daemon** thread keeps the program alive until it finishes; a **daemon** thread
does not. Separately, Python lets you register cleanup functions with `atexit` that run when
the program shuts down.

Here is the trap. Python waits for non-daemon threads **before** running `atexit` cleanup.
webR's writer loops forever until the `atexit` cleanup tells it to stop. So with a
non-daemon thread:

```
Python:  I'll wait for the writer thread to finish before cleaning up.
Writer:  I'll stop as soon as the cleanup tells me to.
```

Neither moves. The program hangs on exit — **every single time**. A daemon thread plus an
`atexit` drain is the only arrangement that terminates. This is not something you would
guess; it is something you discover by hanging.

**Counter-example #2 — why a lock is still needed.** Python has a mechanism (the GIL) that
makes single operations safe. It does **not** make *sequences* safe:

```python
if len(buffer) >= capacity:   # thread A checks: 99, not full
    buffer.popleft()          # thread B appends here -> now 100
buffer.append(record)         # thread A appends -> 101, over capacity
```

Each line is fine. The three together are not, because another thread can act between them.
A **lock** — meaning "only one thread inside this block at a time" — is required. webR's
buffer uses one for exactly this.

**Where in webR.** `writer.py` for the thread, `buffer.py` for the lock.

---

### 2.6 References and `id()` — the subtle one

**The idea.** A variable does not contain an object; it *points at* one. Two variables can
point at the same object. `id(x)` gives that object's address in memory.

```python
a = [1, 2, 3]
b = a                  # b points at the SAME list
b.append(4)
print(a)               # [1, 2, 3, 4]  -- a changed too
print(id(a) == id(b))  # True: same object

c = [1, 2, 3, 4]
print(a == c)          # True:  same contents
print(a is c)          # False: different objects
```

`==` asks *"same contents?"*. `is` asks *"literally the same object?"*.

webR needs this for `mark()`/`link()` — recording that a value produced in one place was
consumed in another. It matches on **identity** (`is`), never contents (`==`).

**Real-world comparison.** Two identical black suitcases at baggage claim. Same contents,
same appearance — but yours is *yours*. Grabbing the other one because it looks the same is
exactly the mistake `==` would make.

**Counter-example — the memory-address trap.** The obvious way to remember a value is to
store its `id()` in a dictionary. That is a real bug, because addresses get reused:

```python
a = [1, 2, 3]
address = id(a)
del a                  # the list is gone; its memory is free
b = ["something", "completely", "different"]
# b may now live at exactly that address
```

If webR had recorded "the plan is at address 140234", after the plan was discarded a totally
unrelated object could land there — and webR would report that data flowed from a node it
never came from. **Inventing a false connection is worse than reporting none.**

Like a parking space number: "the important car is in space 47" is only true while that car
is still parked. The fix is to keep the car parked — webR keeps a **strong reference** to
each marked value, so the object cannot be freed and its address cannot be recycled. That is
also why the registry has a size limit (2,048 entries): holding references keeps objects
alive, so it must be bounded on purpose.

**Where in webR.** `links.py`, the `_marks` dictionary and the `lookup` function.

---

## 3. What you decided, and what each decision cost

Eight questions, answered before any code existed. Here is what each one did to the library.

### Q1 — One node per *call*, not per agent

An orchestrator calling an agent forty times produces forty records rather than one.

**What it cost:** more data. **What it bought:** `collapse.py` — you can always fold forty
records into a summary, but you can never expand a summary back into forty records.

**You can throw information away later; you can never invent it later.** That asymmetry is
worth carrying into every design you do.

### Q2 — asyncio first, other boundaries later

**What it cost:** an extra layer of indirection. The decorator does not use `contextvars`
directly; it asks a `Propagator` object.

**What it bought:** when we added cross-process tracing in Phase H2, we changed
`propagation.py` and **nothing else**. The four decorator wrappers were untouched. Most
"flexible" abstractions never get tested; this one did, and it held.

### Q3 — Bounded memory, but "forget older *safe* nodes"

This was your idea and it improved the design. I had proposed simply discarding the oldest
records. You asked whether the *uninteresting* ones could go first.

That became the two-tier system in `buffer.py`. Without it: a failure at minute two, an hour
of ordinary successes, and by the time you look, the one record you needed is gone.

**The clever part is subtle.** A parent function finishes *after* its children. So when a
child fails, its parents are still running and have no records yet — webR marks their ids as
"keep this when it shows up." Read `TraceBuffer.pin` and `NodeRef.chain_ids` together.

### Q4 — Record payloads by default; catch hallucinations cheaply

You asked whether hallucinations could be caught without storing everything. That question
produced `detectors.py`.

**The trick:** examine the text while it is in memory, keep only the *conclusions*, throw the
text away. A 4KB prompt becomes about 60 bytes of findings plus a fingerprint.

**What it cost:** honestly measured, ~214 microseconds per call on a 1KB payload instead of
~12. Invisible next to an AI call taking seconds; not invisible in a tight loop. Documented
rather than hidden — and re-measured, and the number went *up*, after the adversarial review
added fault isolation to every traced call.

### Q5 — Data-flow edges must be declared, not guessed

Guessing would mean secretly tagging values — impossible for strings, which is what AI
agents pass most.

**The principle:** a detector that fails silently in the common case is disqualifying for a
tool built to catch silent failure.

### Q6 — Validators that never raise, plus "taint"

When a check decides an output looks wrong, webR marks the node and **returns the value
unchanged**. Nothing raises.

**Why:** a hallucination is a call that *succeeded*. Turning it into an exception would
misrepresent what happened and change your program's behaviour.

"Taint" marks every node above a failure — so a function that catches an error and returns a
fallback still shows in the web as *"my answer was built on something that failed."*

### Q7 — Build first, measure later

**What it cost:** we shipped a version 16 times slower than necessary and only found out
when we finally measured. See mistake #1.

### Q8 — Python 3.10+, zero dependencies

Everything is hand-written: the fingerprinting, the detectors, the file writer.

**What it bought:** `pip install webrtrace` works anywhere, with nothing else pulled in.
For a debugging tool, that is worth more than the code it would have saved.

---

## 4. A guided read of the source

About ninety minutes. In this order — each stop assumes the one before.

| # | File | What to look for |
|---|---|---|
| 1 | `records.py` | What gets stored per node. Small and concrete — a good place to start |
| 2 | `propagation.py` | `NodeRef.child()` and `chain_ids()`. How does a node know its ancestors? |
| 3 | `decorator.py` → `_wrap_sync` | The whole flow in twenty lines. Follow it top to bottom |
| 4 | `decorator.py` → `_finish` | Where everything meets: scrub, fingerprint, analyse, validate, decide status, mark ancestors, save |
| 5 | `buffer.py` | The two-tier memory. Read `pin()` and `_evict_oldest()` together |
| 6 | `writer.py` | The background thread. Why daemon? Why write failures out immediately? |
| 7 | `decorator.py` → `_wrap_generator` | Last. The hardest code here |

Then read **`tests/test_edge_cases.py`**. Every test in it is an attempt to break something,
and the comments explain what was being attacked. It is the most instructive file in the
repository.

---

## 5. Six mistakes and what they taught

Real defects that reached working code.

### 1. A limit that limited the wrong thing

To keep analysis fast, I capped it at 2,000 words with `findall(text)[:2000]`. That reads the
**entire** text, builds every word, and *then* throws most away — a limit on memory that is
no limit at all on time. Cost: **6.4 milliseconds per call** on a large payload.

**Lesson:** "I set a limit" and "I limited the *work*" are different claims. Only measuring
tells them apart.

### 2. The fix that made it slower

I rewrote it to stop early. It came out **worse** — the new approach added millions of small
Python-level steps that cost more than the fast built-in scan they replaced.

**Lesson:** I guessed twice and was wrong twice. Profiling — asking the computer where the
time goes — took one command and answered it immediately. **Measure before optimising.**

### 3. A tracing library that could break your program

To record an error, webR calls `str(exception)` to get its message. Some exception types
build their message on demand — and can fail while doing it. That failure escaped webR and
**replaced your original error with a different one**.

**Lesson:** anywhere you touch someone else's data, assume every operation can fail —
including ones as innocent-looking as converting a value to text.

### 4. A background worker that died silently

When the disk filled, the writer thread crashed and the error surfaced in unrelated user
code. Your trace would simply stop, mid-incident, with no explanation.

**Lesson:** ask of every background worker, *"what happens when its work fails?"* Silence is
the worst possible answer, and it is the default one.

### 5. Documentation that contradicted the code

The user guide promised that failures are "never discarded." The code discards them once a
limit is reached — and one of our own tests proves it does.

**Lesson:** documentation drifts toward what you *meant* to build. The second review pass —
"list every place the docs and the code disagree" — was worth as much as the first.

### 6. A test that hid a bug

The link tests had a setup step clearing state before each test. It was there because
`reset()` failed to clear that state. The workaround made the tests pass and kept the bug
invisible for weeks.

**Lesson:** when a test needs a workaround to pass, **the workaround is the bug report.**

---

## 6. Test yourself

Try to answer before opening the box.

1. Ten agents run at the same time. Why doesn't agent 7 think agent 3 called it?
2. A function fails while its caller is still running. How does the caller's record survive
   being discarded, when it does not exist yet?
3. Why does "taint" spread *upward* to callers rather than downward?
4. Why does `link()` compare with `is` rather than `==`?
5. Why does `mark()` hold on to the value instead of just remembering its address?
6. Why can't the payload analysis run on the background thread, as originally planned?
7. Your trace shows four separate traces where you expected one. Name three possible causes.
8. Why must a validator return `True` to pass, rather than just "not False"?
9. Why is a generator being closed early recorded as success rather than failure?
10. Two nodes have the same output fingerprint. What does that tell you?

<details>
<summary>Answers</summary>

1. Each asyncio task gets its own **copy** of the context. Agent 7 writes into its copy;
   agent 3 cannot see it and vice versa.
2. Its **id** is recorded as "keep this when it arrives." Ids come from the live parent
   chain, which exists in memory even though the records do not.
3. Because that is the direction the data flowed. The caller used what the failing function
   produced, so the caller's answer is built on top of a problem.
4. Two lists with identical contents are still different pieces of data. Treating them as one
   would invent a connection that never existed.
5. So the memory address stays valid. A discarded object's address can be reused by something
   unrelated; holding the object prevents that. It is also why the registry is size-limited.
6. Records are frozen the moment they are created and handed straight to the writer. The
   findings must exist *before* creation. Deferring would also mean holding onto every
   payload — unbounded memory growth.
7. Work handed to a `ThreadPoolExecutor` without `webrtrace.submit()`; work sent to another
   process without `inject()`/`remote_parent()`; or calls that genuinely had no caller.
8. Because a validator that forgets to return anything gives back `None`, and silently
   passing every check is exactly the failure this library exists to catch. Fail loudly.
9. Ending a loop early with `break` closes the generator. That is ordinary control flow;
   calling it an error would fill the web with failures that never happened.
10. The content passed through unchanged between them — that node did not modify it. It is
    how you find *which* function altered a payload without ever storing the payload.

</details>

---

## 7. Glossary

| Term | Meaning |
|---|---|
| **decorator** | A function that wraps another function to add behaviour around it |
| **closure** | A function that remembers variables from where it was created |
| **context variable** | A variable with a separate value per concurrent task, instead of one shared value |
| **immutable / frozen** | Cannot be changed after creation |
| **race condition** | A bug where two things touching the same data at once produce unpredictable results |
| **lock** | A guard letting only one thread into a block of code at a time |
| **thread** | A second line of execution inside one program, sharing its memory |
| **daemon thread** | A thread that does not keep the program alive when everything else finishes |
| **generator** | A function that produces values one at a time, pausing in between |
| **reference** | A pointer to an object; several variables can reference the same one |
| **strong reference** | A reference that keeps an object from being discarded |
| **hash / fingerprint** | A short value derived from text; identical text gives identical hashes |
| **node / edge** | A recorded call / a connection between two calls |
| **taint** | A mark meaning "this succeeded, but used something that failed" |
| **profiling** | Measuring where a program actually spends its time |

---

## 8. Questions worth asking

Genuinely open. Your judgement matters more than mine on these.

**On the design**
- Should payload capture stay on by default, now that we know it costs ~82µs on 1KB? On:
  useful immediately. Off: honest about being nearly free.
- Should "invented a number that appears nowhere in the input" flag a node as suspect by
  default? It is the strongest sign of fabrication and also the noisiest.

**On what is still unsolved**
- **Clocks disagree between machines.** Ordering events across processes by timestamp is
  unreliable. What should "order" even mean in a web spanning several machines?
- **Taint stops at process boundaries.** Fixing it needs the child process to talk back to
  the parent. Worth it?
- **A fluent, well-formed, false sentence is undetectable by any of our methods.** Catching
  it needs an AI model reading the trace afterwards. Is that webR's job, or a separate tool?

**On the process**
- We wrote 254 tests and an outside reviewer still found six real defects in an afternoon.
  What does that tell you about what the remaining tests are actually worth?
