"""SSRF guarding for user-supplied URLs (watch targets, webhooks).

The watcher fetches arbitrary URLs from inside the deployment's network,
which makes it an SSRF vector by construction: without checks, a tenant
could point a "watch" at http://169.254.169.254/ (the EC2 instance
metadata service, credentials included), the database host, or anything
else on the private network. Defense:

- scheme allowlist (http/https) and port allowlist;
- literal-IP targets checked directly;
- hostnames resolved and EVERY resolved address must be public --
  checked again at fetch time, not just at creation time, so a DNS
  record that later flips to a private address is still caught. (The
  classic rebinding TOCTOU between our resolve and the client's connect
  remains; noted in docs/DESIGN_DECISIONS.md as an accepted residual for
  this deployment scale.)
"""
import asyncio
import ipaddress
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443, 8000, 8080, 8443}


class UrlPolicyError(RuntimeError):
    """The URL violates fetch policy. Not transient: retrying won't help."""


def _reject_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, url: str) -> None:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise UrlPolicyError(f"policy: {url} resolves to a non-public address ({ip})")


def validate_url_syntax(url: str) -> str:
    """Cheap, synchronous checks (used at creation time and again before
    fetching). Returns the hostname. Raises UrlPolicyError."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UrlPolicyError(f"policy: scheme must be http or https, got {parsed.scheme!r}")
    if not parsed.hostname:
        raise UrlPolicyError("policy: URL has no host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise UrlPolicyError(f"policy: port {port} is not allowed")
    if parsed.username or parsed.password:
        raise UrlPolicyError("policy: credentials in URLs are not allowed")
    try:
        _reject_ip(ipaddress.ip_address(parsed.hostname), url)
    except ValueError:
        pass  # a hostname, not a literal IP -- resolution check happens async
    return parsed.hostname


async def ensure_public_url(url: str) -> str:
    """Full check before any fetch: syntax + resolve the host and require
    every address to be public. Returns the hostname."""
    host = validate_url_syntax(url)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError as exc:
        # DNS failure is a *transient* error, not a policy violation --
        # let the caller's normal retry path deal with it.
        raise ConnectionError(f"DNS resolution failed for {host}: {exc}") from exc
    if not infos:
        raise ConnectionError(f"DNS resolution returned no addresses for {host}")
    for info in infos:
        _reject_ip(ipaddress.ip_address(info[4][0]), url)
    return host
