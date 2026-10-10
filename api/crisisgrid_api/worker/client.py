from __future__ import annotations

from typing import Any

import httpx

from crisisgrid_core.jobs import Capabilities


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        super().__init__(f"API {status}: {detail}")


class ApiClient:
    """Outbound-only client for the worker: it talks to the control plane, never the other way round."""

    def __init__(self, http: httpx.Client, token: str | None = None) -> None:
        self.http, self.token = http, token

    def _call(self, method: str, path: str, json: Any = None, headers: dict[str, str] | None = None) -> Any:
        h = dict(headers or {})
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        r = self.http.request(method, path, json=json, headers=h)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ApiError(r.status_code, str(detail))
        return r.json()

    def register(self, name: str, caps: Capabilities, registration_token: str | None = None) -> str:
        h = {"X-Registration-Token": registration_token} if registration_token else None
        body = self._call("POST", "/workers/register", {"name": name, "capabilities": caps.model_dump(mode="json")}, h)
        self.token = body["token"]
        return str(body["worker_id"])

    def heartbeat(self) -> None:
        self._call("POST", "/workers/me/heartbeat")

    def claim(self) -> dict[str, Any] | None:
        job = self._call("POST", "/workers/me/claim")["job"]
        return job  # type: ignore[no-any-return]

    def start(self, job_id: str) -> None:
        self._call("POST", f"/jobs/{job_id}/start")

    def logs(self, job_id: str, lines: list[str]) -> None:
        if lines:
            self._call("POST", f"/jobs/{job_id}/logs", {"lines": lines})

    def complete(self, job_id: str, status: str, result: dict[str, Any] | None = None, error: str | None = None) -> dict[str, Any]:
        return self._call("POST", f"/jobs/{job_id}/complete", {"status": status, "result": result, "error": error})  # type: ignore[no-any-return]
