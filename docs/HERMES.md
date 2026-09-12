# Hermes adapter compatibility

`integrations/hermes/` is optional. MegaBrain core does not import Hermes.

The current adapter expects the host Hermes installation to expose the generic optional hook:

```python
class MemoryProvider:
    def should_recall(self, prompt: str) -> bool | None:
        return None
```

`True` forces recall even for a trivial-prompt heuristic; `False` explicitly skips recall; `None` preserves the host heuristic. MegaBrain returns `True` for recognized memory intents and `None` otherwise.

Installation:

1. Install and run MegaBrain independently.
2. Install/sync the adapter from `integrations/hermes/` into the Hermes plugin directory.
3. Configure `MEGABRAIN_BASE_URL`, `MEGABRAIN_API_TOKEN_FILE`, and the provider activation in Hermes.
4. Verify the host runtime with `inspect.getsource(MemoryProvider.should_recall)` and run a canary turn.

Upgrade safety:

- The adapter is isolated from core and can be removed without affecting the API.
- On Hermes upgrades, verify the hook signature and outer prefetch dispatch before enabling the provider.
- Do not copy Hermes core into this repository.
