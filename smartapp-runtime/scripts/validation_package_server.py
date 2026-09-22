#!/usr/bin/env python3
"""Serve the local validation SmartApp package over HTTPS."""

import argparse
import http.server
import ssl
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--cert", required=True, type=Path)
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=18443, type=int)
    args = parser.parse_args()

    handler = lambda *items, directory=str(args.directory), **kwargs: http.server.SimpleHTTPRequestHandler(
        *items, directory=directory, **kwargs
    )
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=args.cert, keyfile=args.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print("validation package server: https://{0}:{1}".format(args.host, args.port), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
