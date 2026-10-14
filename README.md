# forge

Test one claim: an AI agent can safely extend its own capabilities if every new tool must earn trust before it runs.

Status: **Stage 1 (sandbox) done.** Checks, staging, approval, the registry and the orchestrator are not built yet.

## Sandbox stage

Every generated-tool run goes into a throwaway Docker container:
no network, read-only root filesystem, non-root user, all capabilities dropped,
a single writable `/scratch` tmpfs, CPU/memory/PID limits, a timeout, and capped output.
The image must be pinned by digest. No host environment variables are passed in.

Run the tests:

    uv run pytest                      # unit tests only, no Docker needed
    FORGE_SANDBOX_IMAGE='python@sha256:<digest>' uv run pytest   # full suite, needs Docker

Docker tests skip when `FORGE_SANDBOX_IMAGE` is unset.
