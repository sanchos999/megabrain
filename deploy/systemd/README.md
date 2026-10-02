# Native deployment

Copy `megabrain.service` and `megabrain-embedding-worker.service` to the user systemd unit directory, create the referenced env file from `.env.example`, install the package into the configured virtualenv, run `scripts/migrate.py`, then enable the services. The API verifies the pinned local BGE-M3 ONNX INT8 snapshot before start and prewarms its session. The embedding worker calls the API's authenticated loopback embedding endpoint, so only the API process loads ONNX.

The unit intentionally does not embed credential values; `MB_API_TOKEN_FILE`
points to the local token file. The worker commits database batches of 64 and
sends embeddings in chunks of two, allowing interactive retrieval requests
to interleave with backfill. Its CPU and memory limits remain in place, though
the worker itself no longer maps a model into memory.
