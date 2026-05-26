"""
One-off script to get a refresh token for the Google Ads Automation Desktop OAuth client.
Opens a browser, asks you to authorise, then prints the refresh token to copy into google-ads.yaml.
"""
from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_CONFIG = {
    "installed": {
        "client_id": "YOUR_CLIENT_ID",        # from Google Cloud Console → OAuth 2.0 Client IDs
        "client_secret": "YOUR_CLIENT_SECRET", # from Google Cloud Console → OAuth 2.0 Client IDs
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

SCOPES = ["https://www.googleapis.com/auth/adwords"]

flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, scopes=SCOPES)
creds = flow.run_local_server(port=0)

print("\n✅ Auth complete!")
print(f"\nrefresh_token: {creds.refresh_token}")
print(f"\nPaste this into google-ads.yaml as the refresh_token value.")
