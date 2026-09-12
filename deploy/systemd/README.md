# Native deployment

Copy `megabrain.service` to the user systemd unit directory, create the referenced env file from `.env.example`, install the package into the configured virtualenv, run `scripts/migrate.py`, then enable the service.

The unit intentionally does not embed credentials or machine-specific paths.
