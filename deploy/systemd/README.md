# Native deployment

Copy `megabrain.service` and `megabrain-embedding-worker.service` to the user systemd unit directory, create the referenced env file from `.env.example`, install the package into the configured virtualenv, run `scripts/migrate.py`, then enable the services. Both API and embedding units verify the pinned local BGE-M3 ONNX INT8 snapshot before start; the first start downloads it only when the local model directory is incomplete. The API prewarms the local session so the first request does not pay model-load latency.

The unit intentionally does not embed credentials or machine-specific paths.
The embedding worker uses database batch 64 and six ONNX CPU threads under
`CPUQuota=300%` and `MemoryMax=4G`; the model is downloaded once and then
loaded locally.
