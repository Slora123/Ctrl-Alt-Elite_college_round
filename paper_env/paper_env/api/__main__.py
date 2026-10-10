import os

import uvicorn

if __name__ == "__main__":
    # Local tool, no authentication: bind to loopback only. Override the port with PAPER_ENV_PORT.
    uvicorn.run("paper_env.api.main:app_factory", factory=True, host="127.0.0.1",
                port=int(os.environ.get("PAPER_ENV_PORT", "8765")))
