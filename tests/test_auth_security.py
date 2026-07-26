from __future__ import annotations

from pta.auth import _build_cookie_string


def test_cookie_string_accepts_only_exact_pintia_domain_or_subdomains():
    cookies = [
        # Invalid lookalike/superdomains must not reserve this cookie name.
        {"domain": "evilpintia.cn", "name": "PTASession", "value": "attacker-prefix"},
        {"domain": "pintia.cn.evil", "name": "PTASession", "value": "attacker-suffix"},
        {"domain": ".pintia.cn", "name": "PTASession", "value": "legitimate"},
        # The first valid cookie with a name wins, preserving the existing behavior.
        {"domain": "passport.pintia.cn", "name": "PTASession", "value": "duplicate"},
        {"domain": ".PASSPORT.PINTIA.CN.", "name": "passport", "value": "valid"},
        {"domain": "foo..pintia.cn", "name": "invalid-empty-label", "value": "bad"},
        {"domain": "notpintia.cn", "name": "unrelated", "value": "bad"},
    ]

    cookie_string = _build_cookie_string(cookies)

    assert cookie_string == "PTASession=legitimate; passport=valid"
    assert "attacker" not in cookie_string
