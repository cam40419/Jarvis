"""Bounded mDNS browsing, with no subnet scanning or trust in advertised hostnames."""

from ipaddress import IPv4Address, IPv4Network
from threading import Event, Lock

from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf


def private_address(value: str) -> IPv4Address | None:
    try:
        address = IPv4Address(value)
    except ValueError:
        return None
    return (
        address
        if any(address in IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
        else None
    )


def shelly_candidates(seconds: float = 5) -> tuple[tuple[str, IPv4Address], ...]:
    found: set[tuple[str, IPv4Address]] = set()
    lock = Lock()

    class Listener(ServiceListener):
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            remote_id = name.split(".", 1)[0].lower()
            suffix = remote_id.removeprefix("shellyplugusg4-")
            if (
                not remote_id.startswith("shellyplugusg4-")
                or len(suffix) != 12
                or any(c not in "0123456789abcdef" for c in suffix)
            ):
                return
            with lock:
                if len(found) >= 32:
                    return
            info = zc.get_service_info(type_, name, timeout=700)
            if not info or info.port != 80:
                return
            with lock:
                for value in info.parsed_addresses(IPVersion.V4Only):
                    address = private_address(value)
                    if address and len(found) < 32:
                        found.add((remote_id, address))

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            self.add_service(zc, type_, name)

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

    zc = Zeroconf(ip_version=IPVersion.V4Only)
    try:
        browser = ServiceBrowser(zc, ["_shelly._tcp.local.", "_http._tcp.local."], Listener())
        try:
            Event().wait(seconds)
        finally:
            browser.cancel()
        return tuple(sorted(found))
    finally:
        zc.close()
