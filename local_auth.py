"""
Run this ONCE, locally on your Mac (not in GitHub Actions), to authorize
this app against your YouTube channel and produce a refresh token that
GitHub Actions can use for unattended uploads.

Usage:
    python local_auth.py

This will open a browser window, ask you to log into the Google account
that owns the YouTube channel, and approve access. When it's done it
prints a refresh token — copy that value into a GitHub Actions secret
named YT_REFRESH_TOKEN.
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    # Needed for compute_category_weights()/compute_country_weights() to
    # read view/like counts on past videos (youtube.videos().list) - without
    # this, those calls fail with 403 insufficientPermissions and the
    # pipeline silently falls back to picking categories/countries
    # uniformly at random instead of favoring what's actually performing.
    "https://www.googleapis.com/auth/youtube.readonly",
    # Needed for videos.delete() (audit_old_videos.py) - youtube.upload
    # alone is NOT sufficient for deleting videos. Per YouTube's own API
    # docs, videos.delete requires one of: youtube.partner, youtube, or
    # youtube.force-ssl. force-ssl is the narrowest of the three that
    # still covers delete.
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
CLIENT_SECRET_FILE = "credentials/client_secret.json"

def main():
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    print("\n--- SUCCESS ---")
    print("Add this as a GitHub Actions secret named YT_REFRESH_TOKEN:\n")
    print(creds.refresh_token)
    print("\nAlso keep these for reference (already in client_secret.json):")
    print("client_id:", creds.client_id)
    print("client_secret:", creds.client_secret)

if __name__ == "__main__":
    main()
