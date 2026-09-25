from __future__ import annotations

import argparse
import os
import socket

from .app import create_app


def _unique_preserving_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _discover_local_ip_addresses(family: socket.AddressFamily) -> list[str]:
    discovered: list[str] = []
    for lookup_host in _unique_preserving_order([socket.gethostname(), socket.getfqdn()]):
        try:
            infos = socket.getaddrinfo(lookup_host, None, family=family, type=socket.SOCK_STREAM)
        except socket.gaierror:
            continue
        for info in infos:
            address = info[4][0].split("%", 1)[0]
            if family == socket.AF_INET and not address.startswith("127."):
                discovered.append(address)
            elif family == socket.AF_INET6 and address != "::1":
                discovered.append(address)
    return _unique_preserving_order(discovered)


def _resolve_listen_addresses(host: str) -> list[str]:
    if host in {"", "0.0.0.0"}:
        return ["0.0.0.0", *_discover_local_ip_addresses(socket.AF_INET)]
    if host == "::":
        return ["::", *_discover_local_ip_addresses(socket.AF_INET6)]

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return [host]

    return _unique_preserving_order([info[4][0].split("%", 1)[0] for info in infos])


def _format_http_address(address: str, port: int) -> str:
    if ":" in address and not address.startswith("["):
        return f"http://[{address}]:{port}"
    return f"http://{address}:{port}"


def _print_listen_addresses(host: str, port: int) -> None:
    print("Reversi service listening on:")
    for address in _resolve_listen_addresses(host):
        print(f"  - {_format_http_address(address, port)}")


def _resolve_operator_password(cli_value: str | None) -> str | None:
    if cli_value is not None:
        return cli_value
    return os.environ.get("REVERSI_OPERATOR_PASSWORD")


def _print_operator_auth_configuration(operator_password: str | None) -> None:
    print(
        "Operator routes require authentication as username 'operator' with the configured password. "
        "API clients may also send X-Reversi-Operator-Password."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Reversi workshop web server.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind to.")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to.")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode.")
    parser.add_argument(
        "--flask-dev-server",
        action="store_true",
        help="Force the Flask development server instead of Waitress.",
    )
    parser.add_argument(
        "--log-socket-errors",
        action="store_true",
        help="Enable Waitress socket error logging for client disconnects during accept().",
    )
    parser.add_argument(
        "--operator-password",
        default=None,
        help=(
            "Password required for operator/browser routes from any IP. "
            "Can also be provided via REVERSI_OPERATOR_PASSWORD."
        ),
    )
    args = parser.parse_args()

    operator_password = _resolve_operator_password(args.operator_password)
    if not operator_password:
        parser.error("An operator password is required. Set --operator-password or REVERSI_OPERATOR_PASSWORD.")

    app = create_app(operator_password=operator_password)
    if args.debug or args.flask_dev_server:
        if not args.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
            _print_listen_addresses(args.host, args.port)
            _print_operator_auth_configuration(operator_password)
        app.run(host=args.host, port=args.port, debug=args.debug)
        return

    from waitress import serve

    _print_listen_addresses(args.host, args.port)
    _print_operator_auth_configuration(operator_password)
    serve(
        app,
        host=args.host,
        port=args.port,
        threads=8,
        log_socket_errors=args.log_socket_errors,
    )


if __name__ == "__main__":
    main()

