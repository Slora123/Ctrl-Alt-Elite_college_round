import uvicorn

if __name__ == "__main__":
    # Local tool, no authentication: bind to loopback only.
    uvicorn.run("paper_env.api.main:app_factory", factory=True, host="127.0.0.1", port=8000)
