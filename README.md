# Frame.io remaining-file uploader (legacy baseline)

Historical implementation using `frameioclient` and the legacy Frame.io API.
The next commit migrates the active implementation to the V4 web-client API.

The legacy program reads `FRAMEIO_TOKEN` from the environment or a local `.env`.
Never commit that file or paste a real token into source code. Delivery manifests,
progress, logs, and the original operator handoff are intentionally untracked.

This baseline is retained for history, not recommended for new deployments.
