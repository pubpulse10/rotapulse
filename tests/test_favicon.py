"""
The root /favicon.ico is served (and public): Windows .url desktop shortcuts,
bookmarks and crawlers request it directly with no session and must get the
icon back rather than a 404 or a login redirect.
"""


def test_favicon_served_at_root_without_login(client):
    resp = client.get("/favicon.ico")
    assert resp.status_code == 200
    # Real .ico magic number — proves the icon file itself was served.
    assert resp.data[:4] == b"\x00\x00\x01\x00"
