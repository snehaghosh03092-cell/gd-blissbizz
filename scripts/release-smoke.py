#!/usr/bin/env python3
"""Check the isolated release via verified HTTPS without exposing credentials or calling inference."""

import argparse
import http.client
import io
import json
import socket
import ssl
import zipfile
from http.cookies import SimpleCookie
from pathlib import Path


class Client:
    def __init__(self, host, ca_file=None):
        self.host = host
        self.context = ssl.create_default_context(cafile=ca_file)
        self.cookies = {}
        self.csrf = None

    def request(self, method, path, body=None, expected=200):
        connection = http.client.HTTPSConnection(
            self.host, context=self.context, timeout=60
        )
        # Route only the TCP connection to the private proxy. SNI/certificate/Host stay real.
        connection._create_connection = lambda *_a, **_k: socket.create_connection(
            ("proxy", 443), 60
        )
        headers = {"Origin": f"https://{self.host}"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            content = response.read()
            if response.status != expected:
                raise RuntimeError(
                    f"{method} {path}: expected {expected}, got {response.status}"
                )
            for key, value in response.getheaders():
                if key.lower() != "set-cookie":
                    continue
                cookie = SimpleCookie()
                cookie.load(value)
                for name, morsel in cookie.items():
                    if (
                        not morsel["secure"]
                        or not morsel["httponly"]
                        or morsel["domain"]
                    ):
                        raise RuntimeError(
                            "Session cookie must be Secure, HttpOnly and host-only"
                        )
                    if morsel["max-age"] == "0":
                        self.cookies.pop(name, None)
                    else:
                        self.cookies[name] = morsel.value
            return content
        finally:
            connection.close()

    def get_json(self, path):
        return json.loads(self.request("GET", path))

    def login(self, account, storefront=False):
        prefix = "/api/v1/storefront/auth" if storefront else "/api/v1/auth"
        self.csrf = self.get_json(prefix + "/csrf")["csrf_token"]
        if not self.cookies:
            raise RuntimeError("Missing session cookie")
        result = json.loads(
            self.request(
                "POST",
                prefix + "/login",
                {
                    "email": account["email"],
                    "password": account["password"],
                },
            )
        )
        self.csrf = result["csrf_token"]
        return result

    def logout(self, storefront=False):
        prefix = "/api/v1/storefront/auth" if storefront else "/api/v1/auth"
        self.request("POST", prefix + "/logout", expected=204)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True)
    parser.add_argument(
        "--ca-file", help="Optional trusted CA for isolated local test certificates"
    )
    args = parser.parse_args()
    credentials = json.loads(Path(args.credentials).read_text())
    admin = Client("admin.blissbiz.biz", args.ca_file)
    other = Client("admin.blissbiz.biz", args.ca_file)
    shop = Client("threads-co.blissbiz.biz", args.ca_file)
    owner_account = next(
        a for a in credentials if a["email"] == "owner@threads-co.example"
    )
    other_account = next(
        a for a in credentials if a["email"] == "owner@freshbasket.example"
    )
    shopper_account = next(
        a for a in credentials if a["kind"] == "shopper" and a["shop"] == "threads-co"
    )
    logged_in = []
    try:
        ready = Client("blissbiz.biz", args.ca_file).get_json("/api/ready")
        assert ready["status"] == "ready"
        print("PASS: HTTPS and gateway readiness", flush=True)
        own = admin.login(owner_account)
        logged_in.append((admin, False))
        assert (
            admin.get_json("/api/v1/auth/me")["owner"]["email"]
            == owner_account["email"]
        )
        bid = own["businesses"][0]["id"]
        admin.get_json(f"/api/v1/admin/businesses/{bid}/inventory")
        print("PASS: owner login, session cookies and inventory", flush=True)
        foreign = other.login(other_account)
        logged_in.append((other, False))
        foreign_id = foreign["businesses"][0]["id"]
        assert bid != foreign_id
        admin.request("GET", f"/api/v1/admin/businesses/{foreign_id}", expected=404)
        other.request("GET", f"/api/v1/admin/businesses/{bid}/inventory", expected=404)
        print("PASS: owner cross-business access denied in both directions", flush=True)
        assert shop.get_json("/api/v1/storefront")["business"]["slug"] == "threads-co"
        assert shop.get_json("/api/v1/storefront/products")["total"] > 0
        shop.login(shopper_account, storefront=True)
        logged_in.append((shop, True))
        assert (
            shop.get_json("/api/v1/storefront/auth/me")["customer"]["email"]
            == shopper_account["email"]
        )
        # Deliberately replay a shopper session on another shop to test server-side scoping.
        foreign_shop = Client("freshbasket.blissbiz.biz", args.ca_file)
        foreign_shop.cookies = dict(shop.cookies)
        foreign_shop.request("GET", "/api/v1/storefront/auth/me", expected=401)
        print(
            "PASS: storefront catalog, shopper login and shopper tenant isolation",
            flush=True,
        )
        for kind in ("inventory.xlsx", "invoices.xlsx"):
            content = admin.request(
                "GET", f"/api/v1/admin/businesses/{bid}/documents/{kind}"
            )
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                assert "xl/workbook.xml" in archive.namelist()
            other.request(
                "GET", f"/api/v1/admin/businesses/{bid}/documents/{kind}", expected=404
            )
        print(
            "PASS: inventory/invoice workbooks and download ownership checks",
            flush=True,
        )
    finally:
        for client, storefront in reversed(logged_in):
            client.logout(storefront)
    print("ALL CHECKS PASSED. No orders, stock or settings were changed.", flush=True)


if __name__ == "__main__":
    main()
