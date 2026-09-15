#!/usr/bin/env python3
"""Loopback-only HTTPS CONNECT proxy for AudioSpider YouTube downloads."""

from __future__ import annotations

import argparse
import ipaddress
import socket
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler


ALLOWED_SUFFIXES = ("youtube.com", "googlevideo.com")
BUFFER_SIZE = 64 * 1024
CONNECT_TIMEOUT_SECONDS = 15
IDLE_TIMEOUT_SECONDS = 180


class LoopbackThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class YouTubeConnectHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AudioSpiderYouTubeProxy/1.0"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_CONNECT(self) -> None:  # noqa: N802
        authority = self._parse_authority(self.path)
        if authority is None or authority[1] != 443 or not self._is_allowed_host(authority[0]):
            self.send_error(403, "destination is not allow-listed")
            return
        try:
            addresses = self._public_addresses(*authority)
            upstream = self._connect(addresses)
        except OSError:
            self.send_error(502, "upstream connection failed")
            return

        try:
            self.send_response(200, "Connection Established")
            self.end_headers()
            self._relay(self.connection, upstream)
        finally:
            upstream.close()

    def do_GET(self) -> None:  # noqa: N802
        self.send_error(405, "only HTTPS CONNECT is supported")

    do_HEAD = do_GET
    do_POST = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET
    do_OPTIONS = do_GET

    @staticmethod
    def _parse_authority(value: str) -> tuple[str, int] | None:
        if not value or "@" in value or "/" in value or "?" in value or "#" in value:
            return None
        host, separator, port_text = value.rpartition(":")
        if not separator or not host or not port_text.isascii() or not port_text.isdigit():
            return None
        return host.rstrip(".").lower(), int(port_text)

    @staticmethod
    def _is_allowed_host(host: str) -> bool:
        normalized = host.rstrip(".").lower()
        return any(
            normalized == suffix or normalized.endswith("." + suffix)
            for suffix in ALLOWED_SUFFIXES
        )

    @staticmethod
    def _public_addresses(host: str, port: int) -> list[tuple]:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise OSError("DNS returned no addresses")
        for family, _, _, _, sockaddr in addresses:
            address = ipaddress.ip_address(sockaddr[0])
            if not address.is_global:
                raise OSError("DNS returned a non-public address")
            if family not in {socket.AF_INET, socket.AF_INET6}:
                raise OSError("unsupported address family")
        return addresses

    @staticmethod
    def _connect(addresses: list[tuple]) -> socket.socket:
        last_error: OSError | None = None
        for family, socktype, protocol, _, sockaddr in addresses:
            upstream = socket.socket(family, socktype, protocol)
            upstream.settimeout(CONNECT_TIMEOUT_SECONDS)
            try:
                upstream.connect(sockaddr)
                return upstream
            except OSError as exc:
                last_error = exc
                upstream.close()
        raise last_error or OSError("connection failed")

    @staticmethod
    def _relay(client: socket.socket, upstream: socket.socket) -> None:
        client.settimeout(IDLE_TIMEOUT_SECONDS)
        upstream.settimeout(IDLE_TIMEOUT_SECONDS)

        def pump(source: socket.socket, destination: socket.socket) -> None:
            try:
                while True:
                    chunk = source.recv(BUFFER_SIZE)
                    if not chunk:
                        break
                    destination.sendall(chunk)
            except (TimeoutError, BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                try:
                    destination.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        request_thread = threading.Thread(target=pump, args=(client, upstream), daemon=True)
        response_thread = threading.Thread(target=pump, args=(upstream, client), daemon=True)
        request_thread.start()
        response_thread.start()
        request_thread.join()
        response_thread.join()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18797)
    args = parser.parse_args()
    if args.host != "127.0.0.1" or not 1 <= args.port <= 65535:
        parser.error("the proxy must bind to loopback on a valid TCP port")
    with LoopbackThreadingHTTPServer((args.host, args.port), YouTubeConnectHandler) as server:
        print(f"YouTube allow-list proxy listening on {args.host}:{args.port}", flush=True)
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
