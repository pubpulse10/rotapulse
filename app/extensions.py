from flask import request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


def _client_ip():
    # Behind Cloudflare the real visitor IP is in CF-Connecting-IP (Cloudflare
    # always sets it), regardless of how many proxy hops sit in front. Keying
    # off that — not the ProxyFix-resolved address — matters because plain
    # get_remote_address()+ProxyFix was confirmed LIVE not to work: 65
    # concurrent requests through the real Cloudflare->Render chain against
    # /internal/clock-status never tripped a 429, despite passing every local
    # test (the test client has no real proxy chain to expose the bug).
    # Root cause confirmed 2026-08-17, and it is not ProxyFix's hop count:
    # waitress runs with clear_untrusted_proxy_headers=True and trusted_proxy
    # unset, so it STRIPS X-Forwarded-For before ProxyFix ever sees it.
    # CF-Connecting-IP survives only because waitress clears the standard
    # X-Forwarded-* set and nothing else — which is exactly why this
    # workaround works. The same stripping put every url_for(_external) on
    # http; see config.pin_https_scheme. Falls back to the proxied address
    # locally, where the Cloudflare header isn't present.
    return request.headers.get("CF-Connecting-IP") or get_remote_address()


limiter = Limiter(key_func=_client_ip, default_limits=[])
