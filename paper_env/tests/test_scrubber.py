import pytest

from paper_env.security import EnvVarSecretsProvider, Scrubber
from paper_env.security.scrubber import REDACTED


@pytest.fixture
def s():
    return Scrubber(scan_environ=False)


@pytest.mark.parametrize("text", [
    "token hf_abcdefghijklmnopqrstuvwxyz0123456789 end",
    "key AKIAIOSFODNN7EXAMPLE end",
    "Authorization: Bearer abcdef1234567890abcdef",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "sk-abcdefghijklmnopqrstuvwxyz",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----",
    "https://user:hunter2pass@example.com/x",
])
def test_known_token_shapes(s, text):
    out = s.scrub(text)
    assert REDACTED in out
    for secret in ("hf_abcdef", "AKIAIOSFOD", "abcdef1234567890", "ghp_abc", "sk-abc", "MIIE", "hunter2pass"):
        assert secret not in out


def test_key_value_pairs_redact_value_only(s):
    out = s.scrub('API_KEY=supersecret123 and "password": "p4ssw0rd!x" ok')
    assert "supersecret123" not in out and "p4ssw0rd!x" not in out
    assert "API_KEY=" in out and "ok" in out


def test_registered_secret_values_are_replaced_anywhere(s):
    s.add_secrets(["my-custom-credential-value"])
    assert s.scrub("x my-custom-credential-value y") == f"x {REDACTED} y"


def test_short_values_not_registered(s):
    s.add_secrets(["ab"])
    assert s.scrub("ab cd") == "ab cd"


def test_scrub_obj_recurses(s):
    s.add_secrets(["topsecretvalue"])
    obj = {"a": ["topsecretvalue", {"b": "x topsecretvalue"}], "n": 3}
    assert s.scrub_obj(obj) == {"a": [REDACTED, {"b": f"x {REDACTED}"}], "n": 3}


def test_environ_scan_and_subprocess_env(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "envsecret-value-123")
    monkeypatch.setenv("HARMLESS", "visible-value")
    sc = Scrubber()
    assert REDACTED in sc.scrub("leak envsecret-value-123")
    env = sc.subprocess_env()
    assert "MY_SERVICE_TOKEN" not in env and env["HARMLESS"] == "visible-value"


def test_provider_values_feed_scrubber():
    p = EnvVarSecretsProvider({"hf": "HF_TOK"}, environ={"HF_TOK": "provider-secret-value"})
    assert p.get("hf") == "provider-secret-value" and p.get("missing") is None
    assert Scrubber(p.known_values(), scan_environ=False).scrub("provider-secret-value") == REDACTED


def test_plain_text_untouched(s):
    t = "Epoch 1/10 loss=0.123 acc=0.9 tokens=128"
    assert s.scrub(t) == t
