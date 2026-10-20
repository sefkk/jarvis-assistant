"""Validation at the risk boundaries: paths, shell, URLs, AppleScript args, email.

Only clearly dangerous input is blocked. Everything else runs or goes to confirmation.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shlex
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.parse import urljoin, urlsplit

import httpx

from jarvis.exceptions import ToolBlockedError, ToolExecutionError, ToolValidationError

# --- paths ---------------------------------------------------------------------------------

_CASE_INSENSITIVE_FS = sys.platform == "darwin"


class PathGuard:
    """Paths must resolve (symlinks followed) inside an allowed root and outside denied paths."""

    def __init__(
        self,
        allowed_roots: Iterable[str | Path],
        denied_paths: Iterable[str | Path] = (),
        extra_denied: Iterable[str | Path] = (),
    ) -> None:
        self.allowed = [self._resolve(p) for p in allowed_roots]
        self.denied = [self._resolve(p) for p in list(denied_paths) + list(extra_denied)]
        if not self.allowed:
            raise ValueError("PathGuard needs at least one allowed root")

    @staticmethod
    def _resolve(p: str | Path) -> Path:
        return Path(p).expanduser().resolve()

    @staticmethod
    def _same(a: Path, b: Path) -> bool:
        """Same filesystem object. Inode comparison when both exist (case- and symlink-proof on
        APFS, which is case-insensitive by default); otherwise a case-folded string comparison."""
        try:
            return os.path.samestat(a.stat(), b.stat())
        except OSError:
            pass
        if _CASE_INSENSITIVE_FS:
            return str(a).lower() == str(b).lower()
        return a == b

    @classmethod
    def _within(cls, path: Path, root: Path) -> bool:
        return any(cls._same(candidate, root) for candidate in (path, *path.parents))

    def check(self, raw: str, *, for_write: bool = False) -> Path:
        """Return the real path or raise. Reads additionally require the path to exist."""
        if not isinstance(raw, str) or not raw.strip():
            raise ToolValidationError("path is empty")
        if "\x00" in raw:
            raise ToolValidationError("path contains a NUL byte")
        candidate = Path(raw.strip()).expanduser()
        if not candidate.is_absolute():
            raise ToolValidationError(f"path must be absolute or start with ~ (got {raw!r})")
        real = candidate.resolve()
        for denied in self.denied:
            if self._within(real, denied):
                raise ToolBlockedError(f"access to {denied} is denied")
        lowered = real.name.lower()
        if lowered == ".env" or lowered.startswith(".env."):
            raise ToolBlockedError(".env files are off limits")
        if not any(self._within(real, root) for root in self.allowed):
            roots = ", ".join(str(r) for r in self.allowed)
            raise ToolBlockedError(f"{real} is outside the allowed roots ({roots})")
        if not for_write and not real.exists():
            raise ToolExecutionError(f"no such file: {real}")
        return real

    def check_dir(self, raw: str) -> Path:
        real = self.check(raw)
        if not real.is_dir():
            raise ToolValidationError(f"not a directory: {real}")
        return real


# --- shell --------------------------------------------------------------------------------

_SHELL_OPERATOR_RE = re.compile(r"\|\|?|&&|;|\$\(|`|(?<![<>])[<>]{1,2}(?!&)|\d?>&|&\s*$")
_SECRET_ENV_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.IGNORECASE)


_OPERATOR_TOKENS = {"|", "||", "&&", ";", "&"}


def _command_positions_basenamed(argv: list[str]) -> list[str]:
    """argv with the directory stripped from every token in command position (first, or after an operator)."""
    out: list[str] = []
    command_position = True
    for tok in argv:
        if tok in _OPERATOR_TOKENS:
            out.append(tok)
            command_position = True
            continue
        out.append(os.path.basename(tok) if command_position else tok)
        command_position = False
    return out


def denylist_regex(pattern: str) -> re.Pattern[str]:
    """`*` matches anything; the pattern must start and end at a token boundary."""
    body = r".*".join(re.escape(part) for part in pattern.split("*"))
    return re.compile(r"(?<![\w./-])" + body + r"(?![\w-])", re.IGNORECASE)


@dataclass
class ShellCommand:
    raw: str  # exactly what the user approves
    argv: list[str]  # what is executed (never shell=True)
    uses_shell: bool  # True when argv is ["/bin/sh", "-c", raw] because of pipes/redirects
    cwd: Path

    def preview(self) -> str:
        where = f"cwd: {self.cwd}"
        if self.uses_shell:
            return f"{self.raw}\n({where}; runs through /bin/sh because it contains pipes, redirects or operators)"
        return f"{self.raw}\n({where})"


class ShellGuard:
    def __init__(self, denylist: Iterable[str], path_guard: PathGuard) -> None:
        self.patterns = [(p, denylist_regex(p)) for p in denylist]
        self.path_guard = path_guard

    def check(self, command: str, cwd: str | None = None) -> ShellCommand:
        if not isinstance(command, str) or not command.strip():
            raise ToolValidationError("command is empty")
        if "\x00" in command:
            raise ToolValidationError("command contains a NUL byte")
        command = command.strip()
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ToolValidationError(f"cannot parse command: {exc}") from None
        if not argv:
            raise ToolValidationError("command is empty")
        # Match the denylist against the raw text and against the parsed argv (with the
        # program's directory stripped), so quoting tricks such as su""do or /usr/bin/sudo
        # cannot slip past the patterns.
        views = [" ".join(command.split()), " ".join(_command_positions_basenamed(argv))]
        for pattern, regex in self.patterns:
            if any(regex.search(view) for view in views):
                raise ToolBlockedError(f"command matches the shell denylist entry {pattern!r}")
        uses_shell = bool(_SHELL_OPERATOR_RE.search(command))
        workdir = self.path_guard.check_dir(cwd) if cwd else self.path_guard.allowed[0]
        if uses_shell:
            argv = ["/bin/sh", "-c", command]
        else:
            # No shell is involved, so expand ~ ourselves; the model expects `ls ~/Downloads` to work.
            argv = [os.path.expanduser(tok) if tok == "~" or tok.startswith("~/") else tok for tok in argv]
        return ShellCommand(raw=command, argv=argv, uses_shell=uses_shell, cwd=workdir)

    @staticmethod
    def child_env() -> dict[str, str]:
        """The child process environment without anything that looks like a secret."""
        return {k: v for k, v in os.environ.items() if not _SECRET_ENV_RE.search(k)}


# --- URLs (SSRF) -----------------------------------------------------------------------------

_BLOCKED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
]

Resolver = Callable[[str], list[str]]


def system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ToolExecutionError(f"DNS lookup failed for {host}: {exc}") from None
    return sorted({info[4][0] for info in infos})


def is_blocked_address(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return True
    return any(ip in net for net in _BLOCKED_NETWORKS)


class UrlGuard:
    def __init__(self, resolver: Resolver = system_resolver) -> None:
        self.resolve = resolver

    def check(self, url: str) -> str:
        """Allow only http(s) URLs whose host resolves to public addresses. Returns the URL."""
        if not isinstance(url, str) or not url.strip():
            raise ToolValidationError("url is empty")
        url = url.strip()
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            raise ToolBlockedError(f"only http and https URLs are allowed (got {parts.scheme or 'none'})")
        host = parts.hostname
        if not host:
            raise ToolValidationError("url has no host")
        if parts.username or parts.password:
            raise ToolBlockedError("URLs with embedded credentials are not allowed")
        lowered = host.lower().rstrip(".")
        if lowered == "localhost" or lowered.endswith((".localhost", ".local", ".internal", ".home.arpa")):
            raise ToolBlockedError(f"{host} is a local hostname")
        try:
            addresses = [str(ipaddress.ip_address(lowered.strip("[]")))]
        except ValueError:
            addresses = self.resolve(lowered)
        if not addresses:
            raise ToolExecutionError(f"DNS lookup returned no addresses for {host}")
        for address in addresses:
            if is_blocked_address(address):
                raise ToolBlockedError(f"{host} resolves to a private or local address ({address})")
        return url


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    status: int
    content_type: str
    body: str
    truncated: bool


def safe_fetch(
    url: str,
    *,
    guard: UrlGuard,
    max_redirects: int = 5,
    max_bytes: int = 2_000_000,
    timeout: float = 20.0,
    transport: httpx.BaseTransport | None = None,
) -> FetchResult:
    """GET with the SSRF guard applied to the URL and to every redirect target.

    TODO(setup): DNS rebinding (a host that resolves to a public address for the check and a
    private one for the connection) is not covered; pinning the connection to the checked
    address needs a custom transport.
    """
    current = guard.check(url)
    headers = {"User-Agent": "Jarvis/0.1 (+personal assistant)", "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"}
    deadline = time.monotonic() + timeout * 3  # wall clock for the whole fetch, redirects included
    with httpx.Client(follow_redirects=False, timeout=timeout, headers=headers, transport=transport) as client:
        for _ in range(max_redirects + 1):
            try:
                with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ToolExecutionError(f"redirect from {current} without a Location header")
                        current = guard.check(urljoin(current, location))
                        continue
                    chunks: list[bytes] = []
                    size = 0
                    truncated = False
                    for chunk in response.iter_bytes():
                        if time.monotonic() > deadline:
                            raise ToolExecutionError(f"fetching {current} took longer than {timeout * 3:.0f}s")
                        size += len(chunk)
                        if size > max_bytes:
                            chunks.append(chunk[: max_bytes - (size - len(chunk))])
                            truncated = True
                            break
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    encoding = response.encoding or "utf-8"
                    try:
                        body = raw.decode(encoding, errors="replace")
                    except LookupError:
                        body = raw.decode("utf-8", errors="replace")
                    return FetchResult(
                        url=str(response.url),
                        status=response.status_code,
                        content_type=response.headers.get("content-type", ""),
                        body=body,
                        truncated=truncated,
                    )
            except httpx.HTTPError as exc:
                raise ToolExecutionError(f"request to {current} failed: {type(exc).__name__}: {exc}") from None
    raise ToolExecutionError(f"too many redirects (more than {max_redirects})")


# --- AppleScript arguments ---------------------------------------------------------------------

APPLESCRIPT_ARG_MAX_CHARS = 4000


def check_applescript_args(args: Sequence[object]) -> list[str]:
    """Arguments are passed as argv, never interpolated. Only shape checks are needed."""
    checked: list[str] = []
    for arg in args:
        if not isinstance(arg, str):
            raise ToolValidationError(f"AppleScript argument must be a string, got {type(arg).__name__}")
        if "\x00" in arg:
            raise ToolValidationError("AppleScript argument contains a NUL byte")
        if len(arg) > APPLESCRIPT_ARG_MAX_CHARS:
            raise ToolValidationError(f"AppleScript argument longer than {APPLESCRIPT_ARG_MAX_CHARS} characters")
        checked.append(arg)
    return checked


# --- email --------------------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+$")


def validate_email(address: str) -> str:
    if not isinstance(address, str):
        raise ToolValidationError("email address must be a string")
    address = address.strip()
    if "\r" in address or "\n" in address:
        raise ToolValidationError("email address contains a line break")
    if len(address) > 254 or not _EMAIL_RE.match(address) or ".." in address:
        raise ToolValidationError(f"invalid email address: {address!r}")
    return address


def validate_header_value(name: str, value: str) -> str:
    """Email header values (subject, names) must not contain CR or LF: that is header injection."""
    if not isinstance(value, str):
        raise ToolValidationError(f"{name} must be a string")
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ToolValidationError(f"{name} contains a line break or NUL (header injection)")
    return value
