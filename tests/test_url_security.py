"""Host checks for redirects, a substring match here once triggered a CodeQL alert."""

from src.core.url_security import url_has_allowed_host


class TestAllowedHost:
    def test_exact_host(self):
        assert url_has_allowed_host("https://www.gog.com/game/x", "www.gog.com")

    def test_subdomain_requires_opt_in(self):
        assert not url_has_allowed_host("https://shop.fanatical.com/x", "fanatical.com")
        assert url_has_allowed_host("https://shop.fanatical.com/x", "fanatical.com", allow_subdomains=True)

    def test_lookalike_domains_are_rejected(self):
        for url in (
            "https://gog.com.evil.tld/x",
            "https://evil-gog.com/x",
            "https://notgog.com/x",
            "https://gog.com.co/x",
        ):
            assert not url_has_allowed_host(url, "gog.com", allow_subdomains=True), url

    def test_host_only_in_path_or_query_is_rejected(self):
        assert not url_has_allowed_host("https://evil.tld/store.epicgames.com", "store.epicgames.com")
        assert not url_has_allowed_host("https://evil.tld/?to=store.epicgames.com", "store.epicgames.com")

    def test_plain_http_is_rejected(self):
        assert not url_has_allowed_host("http://www.gog.com/x", "www.gog.com")

    def test_case_and_trailing_dot(self):
        assert url_has_allowed_host("https://WWW.GOG.COM/x", "www.gog.com")
        assert url_has_allowed_host("https://www.gog.com./x", "www.gog.com")

    def test_garbage_input(self):
        assert not url_has_allowed_host("", "gog.com")
        assert not url_has_allowed_host("not a url", "gog.com")
