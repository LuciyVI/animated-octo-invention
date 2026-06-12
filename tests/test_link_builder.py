from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from bot.link_builder import build_share_link
from tests.fakes import make_client, make_reality_inbound


def test_link_builder_builds_vless_reality_link():
    client_uuid = "11111111-2222-3333-4444-555555555555"
    inbound = make_reality_inbound(
        7,
        clients=[
            make_client(
                client_uuid,
                "tg_123456789_01",
                enabled=True,
            )
        ],
    )

    link = build_share_link(
        inbound,
        client_uuid=client_uuid,
        email="tg_123456789_01",
        public_host="vpn.example.com",
        name="phone",
    )

    parsed = urlparse(link)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "vless"
    assert parsed.username == client_uuid
    assert parsed.hostname == "vpn.example.com"
    assert parsed.port == 443
    assert query["type"] == ["tcp"]
    assert query["security"] == ["reality"]
    assert query["pbk"] == ["PUBLIC_KEY"]
    assert query["fp"] == ["chrome"]
    assert query["sni"] == ["example.com"]
    assert query["sid"] == ["abcdef"]
    assert parsed.fragment == "phone"

