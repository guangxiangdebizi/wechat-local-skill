"""Bounded read-only HTTP/TLS classification of Weixin-owned loopback listeners."""
import argparse
import json
import socket
import ssl

import psutil


def classify(port, tls=False):
    conn = None
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=0.7)
        conn.settimeout(0.7)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            conn = context.wrap_socket(conn, server_hostname="localhost")
        request = ("GET /json/version HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                   "Connection: close\r\n\r\n" % port).encode("ascii")
        conn.sendall(request)
        data = conn.recv(1024)
        # Do not return application bodies, tokens or arbitrary binary data.
        if data.startswith(b"HTTP/"):
            return {"protocol": "https" if tls else "http",
                    "status_line": data.split(b"\r\n", 1)[0].decode("ascii", "replace")}
        return {"protocol": "tls_non_http" if tls else "non_http_or_closed", "bytes_received": len(data)}
    except (OSError, ssl.SSLError) as exc:
        return {"protocol": "not_verified", "error_type": type(exc).__name__}
    finally:
        if conn is not None:
            conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    args = parser.parse_args()
    process = psutil.Process(args.pid)
    if process.name().lower() != "weixin.exe":
        raise SystemExit("Target is not Weixin.exe")
    ports = sorted({conn.laddr.port for conn in process.net_connections(kind="tcp")
                    if conn.status == psutil.CONN_LISTEN and conn.laddr.ip == "127.0.0.1"})
    result = [{"port": port, "http": classify(port), "tls": classify(port, True)} for port in ports[:8]]
    print(json.dumps({"listeners_checked": len(result), "listeners_truncated": len(ports) > 8,
                      "results": result, "writes_attempted": 0,
                      "scope": "GET /json/version plus TLS negotiation on Weixin-owned IPv4 loopback listeners",
                      "conclusion_limit": "Not a complete IPC audit or proof that native write APIs are absent"}))


if __name__ == "__main__":
    main()
