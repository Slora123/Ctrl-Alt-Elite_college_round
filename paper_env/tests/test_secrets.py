import json
import os

import pytest

from paper_env.auth import ANONYMOUS, Principal, current_principal
from paper_env.security import EnvVarSecretsProvider
from paper_env.security.store import ChainedSecretsProvider, SecretError, SecretsStore


@pytest.fixture
def store(tmp_path):
    return SecretsStore(tmp_path / "s")


def test_roundtrip_metadata_and_isolation_between_users(store):
    store.put("alice", "HF_TOKEN", "hf_alice_value_123", allow_in_cells=True)
    store.put("alice", "OTHER", "other-value-456")
    store.put("bob", "HF_TOKEN", "hf_bob_value_789")
    assert [s["name"] for s in store.list("alice")] == ["HF_TOKEN", "OTHER"]
    assert store.get("alice", "HF_TOKEN") == "hf_alice_value_123" and store.get("bob", "HF_TOKEN") == "hf_bob_value_789"
    assert store.get("bob", "OTHER") is None and store.list("carol") == []
    assert store.granted_to_cells("alice") == {"HF_TOKEN": "hf_alice_value_123"}  # only the opted-in one
    assert store.granted_to_cells("bob") == {}
    assert "value" not in json.dumps(store.list("alice"))  # listing never contains values
    store.set_grant("alice", "OTHER", True)
    assert set(store.granted_to_cells("alice")) == {"HF_TOKEN", "OTHER"}
    store.delete("alice", "OTHER")
    assert store.get("alice", "OTHER") is None
    with pytest.raises(KeyError):
        store.delete("alice", "OTHER")
    with pytest.raises(KeyError):
        store.set_grant("alice", "NOPE", True)


def test_encrypted_at_rest_with_private_permissions(store, tmp_path):
    store.put("alice", "K", "plaintext-secret-value")
    raw = (tmp_path / "s" / "secrets.enc").read_bytes()
    assert b"plaintext-secret-value" not in raw and b"alice" not in raw
    assert oct((tmp_path / "s" / "secrets.enc").stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "s" / "secrets.key").stat().st_mode & 0o777) == "0o600"
    # the same key decrypts after a restart
    assert SecretsStore(tmp_path / "s").get("alice", "K") == "plaintext-secret-value"


def test_wrong_key_is_reported_not_silently_empty(tmp_path):
    a = SecretsStore(tmp_path / "s", key=None)
    a.put("u", "K", "value-abc-123")
    (tmp_path / "s" / "secrets.key").unlink()  # key lost: a new one is generated
    with pytest.raises(SecretError, match="cannot be decrypted"):
        SecretsStore(tmp_path / "s").list("u")


@pytest.mark.parametrize("name", ["", "1abc", "a b", "a-b", "x" * 65, "../../etc", "A;B"])
def test_names_must_be_identifiers(store, name):
    with pytest.raises(SecretError):
        store.put("u", name, "value")


def test_value_limits(store):
    for bad in ("", "x" * 16_385):
        with pytest.raises(SecretError):
            store.put("u", "K", bad)


def test_env_key_is_honoured(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("PAPER_ENV_SECRET_KEY", Fernet.generate_key().decode())
    s = SecretsStore(tmp_path / "s")
    s.put("u", "K", "v-value-1")
    assert not (tmp_path / "s" / "secrets.key").exists()  # no key file when the operator supplies one


def test_chained_provider_prefers_the_current_users_secret_then_env(store):
    store.put("alice", "hf_token", "alice-token-value")
    chain = ChainedSecretsProvider(store, EnvVarSecretsProvider({"hf_token": "HF_T", "url_token": "URL_T"},
                                                                 environ={"HF_T": "env-token-value", "URL_T": "env-url-value"}))
    assert chain.get("hf_token") == "env-token-value"  # anonymous principal has no stored secret
    tok = current_principal.set(Principal("alice", "A", "researcher"))
    try:
        assert chain.get("hf_token") == "alice-token-value"
        assert chain.get("url_token") == "env-url-value"
        assert chain.get("missing") is None
    finally:
        current_principal.reset(tok)
    assert "alice-token-value" in chain.known_values() and "env-token-value" in chain.known_values()  # all are scrubbed


def test_secrets_reach_a_kernel_only_when_granted_and_are_redacted(kstack):
    from paper_env.domain import FailureKind
    kstack.runs.secrets_store = kstack.secrets_store = SecretsStore(kstack.settings.data_dir / "sec")
    kstack.secrets_store.put("local", "GRANTED", "granted-secret-value-1", allow_in_cells=True)
    kstack.secrets_store.put("local", "PRIVATE", "private-secret-value-2", allow_in_cells=False)
    kstack.scrubber.add_secrets(kstack.secrets_store.all_values())
    env = kstack.new_ready_env()
    code = ("print(userdata.list())\n"
            "v = userdata.get('GRANTED')\nprint(len(v), 'leak:', v)\n"
            "try:\n    userdata.get('PRIVATE')\nexcept KeyError as e:\n    print('refused:', e)\n"
            "import os\nprint('env has secrets:', any('-secret-value-' in str(x) for x in os.environ.values()))")
    (c,) = kstack.add_cells(env, code)
    run = kstack.runs.run_cell(c.id)
    text = "".join(o.get("text", "") for o in kstack.cell_runs(run)[0].outputs)
    assert "['GRANTED']" in text and "refused" in text and "not available to notebooks" in text
    assert "granted-secret-value-1" not in text and "[REDACTED]" in text  # the cell had it, but it cannot reach storage
    assert "private-secret-value-2" not in text and "env has secrets: False" in text
    blob = json.dumps([e.model_dump(mode="json") for e in kstack.bus.history(limit=10000)])
    assert "granted-secret-value-1" not in blob and "private-secret-value-2" not in blob
    f = kstack.settings.env_dir(env.id) / "userdata.json"
    assert f.exists() and oct(f.stat().st_mode & 0o777) == "0o600" and "private-secret-value-2" not in f.read_text()
    kstack.runner.shutdown()
    assert not f.exists()  # the granted-secrets file leaves with the kernel
    # an unknown name fails the cell as an experiment error with a helpful message
    (d,) = kstack.add_cells(env, "userdata.get('NOPE')")
    r2 = kstack.runs.run_cell(d.id)
    assert kstack.cell_runs(r2)[0].failure_kind == FailureKind.EXPERIMENT and "Secrets panel" in kstack.cell_runs(r2)[0].error.message


def test_userdata_is_not_part_of_the_state_hash_or_checkpoints(kstack):
    env = kstack.new_ready_env()
    (c,) = kstack.add_cells(env, "x = 1")
    kstack.runs.run_cell(c.id)
    first = kstack.cell_runs(kstack.runs.list_runs(env.id)[-1])[0].state_hash
    kstack.runner.shutdown()
    kstack.runs.run_cell(c.id)
    assert kstack.cell_runs(kstack.runs.list_runs(env.id)[-1])[0].state_hash == first  # helper objects are not user state
