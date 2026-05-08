"""
One-time Shopify OAuth exchange — gets a permanent access token for your Shopify store.

Run AFTER shopify app deploy has pushed the new scopes config.

Usage:
  python3 shopify_oauth.py

Opens the browser to the Shopify OAuth URL, starts a local server on port 3000
to catch the callback, exchanges the code for an access token, and saves it to .env.
"""

import os
import sys
import urllib.parse
import urllib.request
import json
import webbrowser
import secrets
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

SHOP            = os.getenv("SHOPIFY_SHOP", "yourstore.myshopify.com")
CLIENT_ID       = os.getenv("SHOPIFY_CLIENT_ID", "")
CLIENT_SECRET   = os.getenv("SHOPIFY_CLIENT_SECRET", "")
SCOPES          = "read_products,write_products,read_inventory"
REDIRECT_URI    = "http://localhost:3000/auth/callback"
STATE           = secrets.token_hex(16)

auth_code  = None
auth_error = None


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code, auth_error
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        if "code" in params:
            auth_code = params["code"]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Authenticated! Return to your terminal.</h2>")
        else:
            auth_error = params.get("error", "unknown")
            self.send_response(400)
            self.end_headers()
            self.wfile.write(f"Error: {auth_error}".encode())

    def log_message(self, *args):
        pass


def main():
    auth_url = (
        f"https://{SHOP}/admin/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&scope={SCOPES}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&state={STATE}"
    )

    server = HTTPServer(("localhost", 3000), CallbackHandler)
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()

    print(f"\nOpening browser to Shopify OAuth…")
    print(f"URL: {auth_url}\n")
    webbrowser.open(auth_url)

    print("Waiting for callback on http://localhost:3000/auth/callback …")
    while auth_code is None and auth_error is None:
        pass
    server.shutdown()

    if auth_error:
        print(f"OAuth error: {auth_error}")
        sys.exit(1)

    print(f"Got auth code: {auth_code[:8]}…")

    # Exchange code for access token
    token_url = f"https://{SHOP}/admin/oauth/access_token"
    data = urllib.parse.urlencode({
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code":          auth_code,
    }).encode()

    req = urllib.request.Request(token_url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())

    access_token = result.get("access_token")
    granted_scope = result.get("scope")

    if not access_token:
        print(f"Failed to get access token: {result}")
        sys.exit(1)

    print(f"\n✅ Access token obtained!")
    print(f"   Scopes: {granted_scope}")
    print(f"   Token:  {access_token[:12]}…\n")

    # Save to .env
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path) as f:
            lines = [l for l in f.readlines() if not l.startswith("SHOPIFY_ACCESS_TOKEN=")]

    lines.append(f"SHOPIFY_ACCESS_TOKEN={access_token}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)

    print(f"✅ Token saved to .env as SHOPIFY_ACCESS_TOKEN")


if __name__ == "__main__":
    main()
