# Contributing

1. Create a focused branch.
2. Do not include `.env`, state, databases, logs, model files or production identifiers.
3. Run `python -m pytest` and `ruff check .`.
4. Add or update a regression test for behavior changes.
5. Keep core agent-neutral; integrations belong under `integrations/`.
6. Document API changes and migration impact.

Pull requests should state test commands and any optional integration dependencies.
