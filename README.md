# webR

**Causality tracing for multi-agent AI systems.**

When a multi-agent system hallucinates or fails silently, nothing crashes. There is no
stack trace, no error code — just a plausible-looking answer that is wrong. webR builds a
directed acyclic graph of your agents at runtime, so you can point at the exact node where
the logic broke.

```python
from webr import webR_node

@webR_node
async def planner(task: str) -> str:
    ...
```

> **Status: pre-alpha.** The core data model is landing now. The public API is not yet
> stable. A full README — problem statement, architecture, benchmarks — ships with v0.1.

## License

Apache License 2.0. See [LICENSE](LICENSE).
