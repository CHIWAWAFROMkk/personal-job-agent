"""Public-web fetch with pinned DNS, validated redirects and bounded reads."""
from __future__ import annotations

import http.client
import ipaddress
import queue
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit


class UnsafeJobURL(ValueError):
    pass


def resolve_public(url: str, timeout: float = 8):
    try:
        if any(ord(c) < 33 for c in url) or '\\' in url:
            raise ValueError()
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        if parts.scheme not in {'http', 'https'} or not host or parts.username is not None or parts.password is not None:
            raise ValueError()
        host = host.encode('idna').decode('ascii')
        if '%' in host or host.rstrip('.').lower() == 'localhost':
            raise ValueError()
    except (ValueError, UnicodeError):
        raise UnsafeJobURL('仅允许公开互联网的 HTTP/HTTPS 岗位链接。') from None
    answers = queue.Queue(maxsize=1)
    def lookup():
        try:
            answers.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError:
            answers.put(None)
    threading.Thread(target=lookup, daemon=True).start()
    try:
        addresses = answers.get(timeout=max(0.01, timeout))
    except queue.Empty:
        raise UnsafeJobURL('岗位域名解析超时，请复制岗位文字。') from None
    if not addresses:
        raise UnsafeJobURL('岗位域名无法解析，请核对链接。')
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        translated = ip.version == 6 and any(ip in prefix for prefix in (
            ipaddress.ip_network('64:ff9b::/96'), ipaddress.ip_network('64:ff9b:1::/48'),
            ipaddress.ip_network('2002::/16'), ipaddress.ip_network('2001::/32'),
        ))
        if not ip.is_global or ip.is_multicast or getattr(ip, 'ipv4_mapped', None) is not None or translated:
            raise UnsafeJobURL('禁止抓取本机、内网或保留地址。')
    return parts, host, port, addresses[0]


def fetch_public_html(url: str, *, timeout: float = 12, max_bytes: int = 2_000_000) -> str:
    deadline = time.monotonic() + timeout
    for hop in range(4):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('岗位网页读取超时。')
        parts, host, port, address = resolve_public(url, remaining)
        sock = socket.socket(address[0], address[1], address[2])
        conn = http.client.HTTPConnection(host, port, timeout=remaining)
        active_socket = [sock]
        def abort_connection(active=active_socket):
            try:
                active[0].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            finally:
                active[0].close()
        watchdog = threading.Timer(max(0.01, deadline - time.monotonic()), abort_connection)
        watchdog.daemon = True
        watchdog.start()
        try:
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            # Connect to the validated numeric address, never resolve the host again.
            sock.connect(address[4])
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            if parts.scheme == 'https':
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
                active_socket[0] = sock
            conn.sock = sock
            path = parts.path or '/'
            if parts.query:
                path += '?' + parts.query
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            conn.request('GET', path, headers={'User-Agent': 'PersonalJobAgent/1.0', 'Accept': 'text/html,text/plain', 'Accept-Encoding': 'identity'})
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            response = conn.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader('Location')
                if not location or hop == 3:
                    raise UnsafeJobURL('岗位网页重定向过多或缺少目标。')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError('岗位网页暂不可读取，请直接复制岗位文字。')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise ValueError('岗位网页编码暂不支持，请复制岗位文字。')
            chunks = []
            size = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('岗位网页读取超时。')
                sock.settimeout(remaining)
                chunk = response.read1(min(65536, max_bytes + 1 - size))
                if time.monotonic() >= deadline:
                    raise TimeoutError('岗位网页读取超时。')
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError('岗位网页过大，请直接复制岗位文字。')
                chunks.append(chunk)
            return b''.join(chunks).decode('utf-8', errors='replace')
        finally:
            watchdog.cancel()
            conn.close()
            sock.close()
    raise UnsafeJobURL('岗位网页重定向过多。')
