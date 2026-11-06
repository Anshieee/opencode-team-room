"""Optional room transports. Join codes are credentials; never log them.

Discovery authenticates both endpoint and TLS pin and binds replies to a fresh
request nonce. It is a convenience on trusted networks, not a public relay.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import threading
import time

DISCOVERY_PORT = 49377
MULTICAST_GROUP = '239.255.49.77'
_CODE = re.compile(r'[A-Z2-7]{5}(?:-[A-Z2-7]{5}){4}-[A-Z2-7]\Z')
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_NONCE = re.compile(r'[0-9a-f]{32}\Z')


def generate_join_code() -> str:
    raw = base64.b32encode(secrets.token_bytes(16)).decode().rstrip('=')
    return '-'.join(raw[i:i + 5] for i in range(0, len(raw), 5))


def parse_join_code(code: str) -> bytes:
    """Return the 128-bit secret; reject noncanonical or ambiguous input."""
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise ValueError('Invalid room code')
    raw = code.replace('-', '')
    secret = base64.b32decode(raw + '=' * 6)
    if base64.b32encode(secret).decode().rstrip('=') != raw:
        raise ValueError('Invalid room code')
    return secret


def room_id(code: str) -> str:
    return hashlib.sha256(b'app-toolkit-room-id-v1\0' + parse_join_code(code)).hexdigest()[:24]


def create_certificate(directory: str | Path) -> tuple[Path, Path, str]:
    """Create an ephemeral TLS certificate in a private directory using openssl."""
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    cert, key = directory / 'cert.pem', directory / 'key.pem'
    # Precreate exclusively, avoiding replacement of existing files or symlinks.
    created = []
    try:
        for path in (cert, key):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            created.append(path)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-sha256',
                        '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '2',
                        '-subj', '/CN=app-toolkit-room'], check=True, capture_output=True,
                       timeout=30)
        der = ssl.PEM_cert_to_DER_cert(cert.read_text())
        return cert, key, hashlib.sha256(der).hexdigest()
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def _canonical(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def _encode(data: dict, code: str) -> bytes:
    data = dict(data)
    data['mac'] = hmac.new(parse_join_code(code), b'room-discovery-v1\0' + _canonical(data), hashlib.sha256).hexdigest()
    return _canonical(data)


def _decode(packet: bytes, code: str) -> dict | None:
    if len(packet) > 4096:
        return None
    try:
        data = json.loads(packet)
        if not isinstance(data, dict):
            return None
        mac = data.pop('mac')
        expected = hmac.new(parse_join_code(code), b'room-discovery-v1\0' + _canonical(data), hashlib.sha256).hexdigest()
        if not isinstance(mac, str) or not hmac.compare_digest(mac, expected):
            return None
        return data
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        return None


def _valid_endpoint(host, port, fingerprint) -> bool:
    return (isinstance(host, str) and 0 < len(host) <= 253
            and not any(c.isspace() or ord(c) < 32 or c in '/\\@?#' for c in host)
            and type(port) is int and 1 <= port <= 65535
            and isinstance(fingerprint, str) and bool(_HEX.fullmatch(fingerprint)))


def encode_advertisement(code: str, nonce: str, host: str, port: int, fingerprint: str) -> bytes:
    if not _NONCE.fullmatch(nonce) or not _valid_endpoint(host, port, fingerprint):
        raise ValueError('Invalid discovery endpoint or nonce')
    return _encode({'type': 'reply', 'room_id': room_id(code), 'nonce': nonce,
                    'host': host, 'port': port, 'fingerprint': fingerprint}, code)


def verify_advertisement(packet: bytes, code: str, nonce: str) -> dict | None:
    data = _decode(packet, code)
    if (not data or set(data) != {'type', 'room_id', 'nonce', 'host', 'port', 'fingerprint'}
            or data['type'] != 'reply' or data['room_id'] != room_id(code)
            or data['nonce'] != nonce
            or not _valid_endpoint(data['host'], data['port'], data['fingerprint'])):
        return None
    return {key: data[key] for key in ('room_id', 'host', 'port', 'fingerprint')}


class DiscoveryAnnouncer:
    """Stoppable authenticated UDP responder. Use as a context manager.

    Default bind joins the LAN multicast group. Set bind_host='127.0.0.1' and
    discovery_port=0 for isolated tests. No code or credential is broadcast.
    """
    def __init__(self, code: str, host: str, port: int, fingerprint: str, *,
                 bind_host: str = '0.0.0.0', discovery_port: int = DISCOVERY_PORT, alternate_host: str | None = None):
        encode_advertisement(code, '0' * 32, host, port, fingerprint)
        self.code, self.host, self.port, self.fingerprint = code, host, port, fingerprint
        self.alternate_host = alternate_host
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((bind_host, discovery_port))
            if bind_host == '0.0.0.0':
                self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                      socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton('0.0.0.0'))
            self.socket.settimeout(.1)
        except Exception:
            self.socket.close()
            raise
        self.address = self.socket.getsockname()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def close(self):
        self._stop.set()
        self.socket.close()
        if self._thread.ident is not None:
            self._thread.join(timeout=1)

    def __exit__(self, *args):
        self.close()

    def reply_host(self, source):
        if self.alternate_host and ipaddress.ip_address(source) in ipaddress.ip_network('100.64.0.0/10'):
            return self.alternate_host
        return self.host

    def _serve(self):
        while not self._stop.is_set():
            try:
                packet, address = self.socket.recvfrom(4097)
                data = _decode(packet, self.code)
                if (data and set(data) == {'type', 'room_id', 'nonce'}
                        and data['type'] == 'request' and data['room_id'] == room_id(self.code)
                        and isinstance(data['nonce'], str) and _NONCE.fullmatch(data['nonce'])):
                    self.socket.sendto(encode_advertisement(self.code, data['nonce'], self.reply_host(address[0]),
                                                          self.port, self.fingerprint), address)
            except socket.timeout:
                continue
            except OSError:
                break


def discover(code: str, timeout: float = 3, target: tuple[str, int] | None = None) -> list[dict]:
    """Find authenticated endpoints; nonce binding rejects replies to old requests."""
    nonce = secrets.token_hex(16)
    request = _encode({'type': 'request', 'room_id': room_id(code), 'nonce': nonce}, code)
    results = []
    deadline = time.monotonic() + max(0, timeout)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        targets = [target] if target else [(MULTICAST_GROUP, DISCOVERY_PORT), ('255.255.255.255', DISCOVERY_PORT)]
        for address in targets:
            try:
                sock.sendto(request, address)
            except OSError:
                if target:
                    raise
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                packet, _ = sock.recvfrom(4097)
            except socket.timeout:
                break
            advert = verify_advertisement(packet, code, nonce)
            if advert and advert not in results:
                results.append(advert)
    return results


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS with an out-of-band SHA256 DER pin checked before sending HTTP.

    Calling request() is safe: http.client invokes connect() before transmitting
    any headers/body. Redirect handling is intentionally left to the caller.
    """
    def __init__(self, host: str, port: int, fingerprint: str, timeout: float = 5):
        if not isinstance(fingerprint, str) or not _HEX.fullmatch(fingerprint):
            raise ValueError('Invalid certificate fingerprint')
        self.fingerprint = fingerprint
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        super().__init__(host, port, timeout=timeout, context=context)

    def connect(self):
        # Proxy tunnels send an HTTP request before certificate validation.
        if self._tunnel_host:
            raise ValueError('Proxy tunnels are not supported for pinned rooms')
        super().connect()
        actual = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(actual, self.fingerprint):
            self.close()
            raise ssl.SSLError('Room certificate fingerprint mismatch')


def tailscale_status() -> dict:
    """Read local Tailscale status only. Never log in, alter firewall, Serve or Funnel."""
    executable = shutil.which('tailscale')
    result = {'installed': bool(executable), 'state': 'Unavailable', 'addresses': [], 'peer_addresses': [], 'hostname': None}
    if not executable:
        return result
    try:
        status = subprocess.run([executable, 'status', '--json'], capture_output=True, text=True, timeout=5)
        data = json.loads(status.stdout)
        result['state'] = data.get('BackendState', 'Unavailable')
        result['hostname'] = data.get('Self', {}).get('HostName')
        if status.returncode == 0 and result['state'] == 'Running':
            peers = data.get('Peer', {})
            if isinstance(peers, dict):
                for peer in peers.values():
                    if (not isinstance(peer, dict) or peer.get('Online') is not True
                            or peer.get('InNetwork') is False or peer.get('Expired') is True):
                        continue
                    for address in peer.get('TailscaleIPs', []):
                        try:
                            address = str(ipaddress.IPv4Address(address))
                        except (ValueError, TypeError):
                            continue
                        if address not in result['peer_addresses']:
                            result['peer_addresses'].append(address)
            ips = subprocess.run([executable, 'ip', '-4'], capture_output=True, text=True, timeout=5)
            if ips.returncode == 0:
                result['addresses'] = [str(ipaddress.IPv4Address(ip)) for ip in ips.stdout.split()]
    except (OSError, ValueError, subprocess.TimeoutExpired, TypeError, AttributeError):
        result['state'] = 'Unavailable'
        result['addresses'] = []
        result['peer_addresses'] = []
    return result


def host_candidates() -> list[str]:
    """Return IPv4 LAN/tailnet candidates without opening a listening socket."""
    addresses = ['127.0.0.1']
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address != '0.0.0.0' and address not in addresses:
                addresses.append(address)
    except OSError:
        pass
    # UDP connect selects a source route locally; it sends no datagram.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect((MULTICAST_GROUP, DISCOVERY_PORT))
            address = route.getsockname()[0]
            if address != '0.0.0.0' and address not in addresses:
                addresses.append(address)
    except OSError:
        pass
    for address in tailscale_status()['addresses']:
        if address not in addresses:
            addresses.append(address)
    return [address for address in addresses if not ipaddress.ip_address(address).is_loopback] + [
        address for address in addresses if ipaddress.ip_address(address).is_loopback]
