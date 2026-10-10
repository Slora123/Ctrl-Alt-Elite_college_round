from __future__ import annotations

from ..domain import EnvSpec, PaperEnv, SessionLimits
from ..env import EnvManager
from ..repos import UnitOfWorkFactory


class PaperEnvService:
    def __init__(self, uow_factory: UnitOfWorkFactory, envs: EnvManager):
        self._uow, self.envs = uow_factory, envs

    def create(self, alias: str, paper_hash: str, spec: EnvSpec | None = None, dataset_ids: list[str] | None = None,
               build_env: bool = False, owner_id: str | None = None) -> PaperEnv:
        spec = spec or EnvSpec()
        env = PaperEnv(alias=alias, paper_hash=paper_hash, env_spec_id=spec.id, dataset_ids=dataset_ids or [],
                       owner_id=owner_id)
        with self._uow() as u:
            u.paper_envs.add(env, spec)
        if build_env:
            self.envs.create_env(env.id)
        return env

    def get(self, paper_env_id: str) -> PaperEnv:
        with self._uow() as u:
            return u.paper_envs.get(paper_env_id)

    def get_by_alias(self, alias: str) -> PaperEnv:
        with self._uow() as u:
            return u.paper_envs.get_by_alias(alias)

    def list(self) -> list[PaperEnv]:
        with self._uow() as u:
            return u.paper_envs.list()

    def get_spec(self, paper_env_id: str) -> EnvSpec:
        with self._uow() as u:
            return u.paper_envs.get_spec(u.paper_envs.get(paper_env_id).env_spec_id)

    def set_datasets(self, paper_env_id: str, dataset_ids: list[str]) -> PaperEnv:
        with self._uow() as u:
            env = u.paper_envs.get(paper_env_id)
            env.dataset_ids = list(dict.fromkeys(dataset_ids))
            u.paper_envs.update(env)
            return env

    def set_runtime(self, paper_env_id: str, runtime: str) -> PaperEnv:
        with self._uow() as u:
            env = u.paper_envs.get(paper_env_id)
            env.runtime = runtime
            u.paper_envs.update(env)
            return env

    def set_limits(self, paper_env_id: str, limits: SessionLimits) -> PaperEnv:
        with self._uow() as u:
            env = u.paper_envs.get(paper_env_id)
            env.limits = limits
            u.paper_envs.update(env)
            return env

    def delete(self, paper_env_id: str) -> None:
        with self._uow() as u:
            u.paper_envs.delete(paper_env_id)
        self.envs.delete_env_files(paper_env_id)
