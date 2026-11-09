"""HTTP control server that runs inside the sandbox container (SPEC.md §9.2).

FastAPI app on port 7000 implementing the SandboxHandle operations with
xdotool/scrot/subprocess. Every request requires the per-container bearer
token passed as the CONTROL_TOKEN environment variable; the server fails
closed when that variable is missing or empty.
"""
