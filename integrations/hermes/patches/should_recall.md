# Generic Hermes hook patch reference

This repository does not vendor Hermes. If the target Hermes version lacks the optional hook, apply the smallest upstream-compatible change in Hermes:

```diff
 class MemoryProvider:
+    def should_recall(self, prompt: str) -> bool | None:
+        return None
```

The Hermes MemoryManager should safely call this hook and use `None` to retain its existing trivial-prompt heuristic. Provider exceptions must degrade to `None`. Verify with Hermes unit tests and runtime inspection after every upgrade.
