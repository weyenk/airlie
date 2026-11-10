"""The test image (ops/test/Containerfile) runs its layers with the outside network off and loopback on: a
test that reaches past the container fails on its first run, while local servers and sockets still work.

The outbound check means something only inside the image, which sets AIRLIE_TEST_IMAGE=1; on a host with a
network it would fail, so it is skipped there. `task test:integration` runs it in the image under Apple's
`container` here and Docker in CI."""

from __future__ import annotations

import errno
import os
import socket
import time

import pytest

IN_IMAGE = os.environ.get("AIRLIE_TEST_IMAGE") == "1"
NO_ROUTE = (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ECONNREFUSED)
"""What a connect gets with no network: refused at once. A timeout is not one of these: it means the network
is on and something along the way (a runner's firewall) dropped the packets, which proves nothing."""


@pytest.mark.skipif(not IN_IMAGE, reason="the network is off only in the test image (AIRLIE_TEST_IMAGE=1)")
def test_an_outbound_connection_fails_in_the_image() -> None:
    start = time.monotonic()
    with pytest.raises(OSError) as refused:
        socket.create_connection(("1.1.1.1", 443), timeout=2).close()
    took = time.monotonic() - start
    assert not isinstance(refused.value, TimeoutError), "timed out: the network is on, only filtered"
    assert refused.value.errno in NO_ROUTE, refused.value
    assert took < 0.5, f"refused after {took:.2f} s, not at once"


def test_loopback_still_works() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=2) as client:
            peer, _ = server.accept()
            with peer:
                client.sendall(b"x")
                assert peer.recv(1) == b"x"
