import pytest

from crisisgrid_core.scrub import REDACTED, scrub


@pytest.mark.parametrize("secret", [
    "hf_" + "a" * 30, "ghp_" + "B" * 36, "sk-" + "x" * 30, "sk-ant-" + "y" * 30, "AKIAABCDEFGHIJKLMNOP", "xoxb-1234567890-abcdef",
])
def test_credential_shapes_are_redacted_without_being_told(secret):
    out = scrub(f"loading with token {secret} ok")
    assert secret not in out and REDACTED in out and out.startswith("loading with token")


def test_known_secrets_are_redacted_everywhere_and_longest_first():
    out = scrub("pw=hunter22 and hunter22extra and hunter22", secrets=["hunter22", "hunter22extra"])
    assert "hunter22" not in out


def test_short_or_empty_secrets_are_ignored_to_avoid_mangling_text():
    assert scrub("a b c", secrets=["", "a", "ab"]) == "a b c"


def test_assignment_style_secrets_keep_the_name():
    out = scrub("API_KEY = 'abcd1234efgh' and password: s3cretvalue!")
    assert "abcd1234efgh" not in out and "s3cretvalue" not in out and "API_KEY" in out and "password" in out


def test_bearer_and_private_keys():
    assert "abc" not in scrub("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123")
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\ndef\n-----END RSA PRIVATE KEY-----"
    assert "MIIabc" not in scrub(f"x {pem} y")


@pytest.mark.parametrize("clean", ["epoch 3 loss 0.123", "tokenizer loaded", "learning_rate = 0.1", "Traceback (most recent call last):"])
def test_ordinary_text_is_untouched(clean):
    assert scrub(clean) == clean
