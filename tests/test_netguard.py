import pytest

from odrl_agent.netguard import UnsafeBaseUrlError, validate_base_url

LINK_LOCAL = ".".join(["169", "254", "1", "1"])


@pytest.mark.parametrize(
    "url",
    [
        f"http://{LINK_LOCAL}/latest",
        "http://[fe80::1]/",
        f"http://[::ffff:{LINK_LOCAL}]/",
        "http://metadata.google.internal/",
        "http://0.0.0.0:8000",
        "file:///etc/passwd",
        "https://user:pw@example.com",
    ],
)
def test_unsafe_urls_rejected(url) -> None:
    with pytest.raises(UnsafeBaseUrlError):
        validate_base_url(url)


def test_loopback_and_public_ip_allowed() -> None:
    assert validate_base_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    assert validate_base_url("https://93.184.216.34/v1") == "https://93.184.216.34/v1"


def test_allowlist_enforced(monkeypatch) -> None:
    monkeypatch.setenv("NL2ODRL_ALLOWED_LLM_HOSTS", "api.example.com")
    with pytest.raises(UnsafeBaseUrlError):
        validate_base_url("https://93.184.216.34")
