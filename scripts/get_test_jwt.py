"""
scripts/get_test_jwt.py — mints a real Supabase-issued JWT for a test user,
so you can hit require_auth()-protected routes without a browser.

Uses the Admin API (service role key) to create the user if it doesn't
already exist, then signs in with the password grant to get a real
access_token — the same kind of JWT the frontend gets after a normal
login, signed with SUPABASE_JWT_SECRET, so it round-trips through
api/server.py's require_auth() exactly like a real request would.

Usage:
    python scripts/get_test_jwt.py                      # uses defaults below
    python scripts/get_test_jwt.py alice@test.dev pw123456
    python scripts/get_test_jwt.py alice@test.dev pw123456 --create-only
    python scripts/get_test_jwt.py alice@test.dev pw123456 --curl

Requires in your .env (or real env vars):
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY   -- admin key, used ONLY to create the test
                                   user. Never send this to a client or
                                   commit it. This script is a dev tool,
                                   not something to run in prod.

Prints:
    user_id       -- the sub claim / owner_id you'll see server-side
    access_token  -- paste into Authorization: Bearer <token>
"""
import os
import sys
import argparse

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SUPABASE_URL = os.getenv("SUPABASE_URL")
SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

DEFAULT_EMAIL = "test-user@minime.local"
DEFAULT_PASSWORD = "test-password-123"


def _require_config():
    missing = [name for name, val in
               [("SUPABASE_URL", SUPABASE_URL), ("SUPABASE_SERVICE_ROLE_KEY", SERVICE_ROLE_KEY)]
               if not val]
    if missing:
        print(f"Missing env var(s): {', '.join(missing)}. Check your .env.", file=sys.stderr)
        sys.exit(1)


def create_user(email: str, password: str) -> dict | None:
    """Creates a confirmed test user via the Admin API. Returns the user
    object, or None if one already exists with this email (not an error —
    sign_in below will just authenticate as the existing user)."""
    resp = requests.post(
        f"{SUPABASE_URL}/auth/v1/admin/users",
        headers={
            "apikey": SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
        },
        json={"email": email, "password": password, "email_confirm": True},
        timeout=15,
    )
    if resp.status_code in (200, 201):
        return resp.json()
    if resp.status_code == 422 and "already been registered" in resp.text.lower():
        return None
    resp.raise_for_status()


def update_password(user_id: str, password: str) -> None:
    """Admin-API password reset for a user that already exists — lets you
    rerun this script with a known password even if the account was
    created earlier (by hand, or by a previous run) with something else."""
    resp = requests.put(
        f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}",
        headers={
            "apikey": SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
        },
        json={"password": password},
        timeout=15,
    )
    resp.raise_for_status()


def find_user_by_email(email: str) -> dict | None:
    """Paginates through every user and matches email exactly (case-
    insensitive). Deliberately does NOT trust the Admin API's `email`
    query param alone: on this project it didn't filter server-side,
    it silently returned the first user in the whole list — meaning
    an earlier version of this function reset the wrong user's
    password. This scans and verifies the match itself instead."""
    page = 1
    per_page = 200
    while True:
        resp = requests.get(
            f"{SUPABASE_URL}/auth/v1/admin/users",
            headers={
                "apikey": SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
            },
            params={"page": page, "per_page": per_page},
            timeout=15,
        )
        resp.raise_for_status()
        users = resp.json().get("users", [])
        if not users:
            return None
        for u in users:
            if (u.get("email") or "").lower() == email.lower():
                return u
        if len(users) < per_page:
            return None
        page += 1


def sign_in(email: str, password: str) -> dict:
    """Password-grant sign-in — same call the frontend's login form makes.
    Uses the anon key convention (service role also works here since it's
    a superset), but deliberately does NOT use the admin endpoint: we want
    a token minted the same way a real user's would be."""
    resp = requests.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
        headers={
            "apikey": SERVICE_ROLE_KEY,
            "Content-Type": "application/json",
        },
        json={"email": email, "password": password},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", nargs="?", default=DEFAULT_EMAIL)
    parser.add_argument("password", nargs="?", default=DEFAULT_PASSWORD)
    parser.add_argument("--create-only", action="store_true",
                         help="Create the user and exit without signing in.")
    parser.add_argument("--curl", action="store_true",
                         help="Also print a ready-to-run curl example against /api/chats.")
    parser.add_argument("--save-to", metavar="PATH", default=None,
                         help="Write ONLY the raw access_token to this file "
                              "(no labels/newline noise), for scripting: "
                              "$token = Get-Content PATH -Raw")
    parser.add_argument("--reset-password", action="store_true",
                         help="If the user already exists with a different "
                              "password, force it to match the password "
                              "argument via the Admin API before signing in.")
    args = parser.parse_args()

    _require_config()

    created = create_user(args.email, args.password)
    if created:
        print(f"Created test user: {args.email} (id={created['id']})")
    else:
        print(f"User {args.email} already exists — signing in as them instead.")
        if args.reset_password:
            existing = find_user_by_email(args.email)
            if not existing:
                print(f"Could not find existing user {args.email} to reset password.", file=sys.stderr)
                sys.exit(1)
            assert existing["email"].lower() == args.email.lower(), (
                f"Refusing to reset password: looked up {args.email!r} but "
                f"got back {existing.get('email')!r} (id={existing.get('id')}). "
                f"This should never happen — bailing out rather than touching "
                f"the wrong account."
            )
            update_password(existing["id"], args.password)
            print(f"Password reset for {args.email} (id={existing['id']}).")

    if args.create_only:
        return

    session = sign_in(args.email, args.password)
    user_id = session["user"]["id"]
    access_token = session["access_token"]

    print()
    print(f"user_id (owner_id):\n  {user_id}")
    print()
    print(f"access_token:\n  {access_token}")

    if args.curl:
        print()
        print("Try it:")
        print(f'  curl -H "Authorization: Bearer {access_token}" http://localhost:8000/api/chats')

    if args.save_to:
        # Write the raw token with no trailing newline, so
        # Get-Content -Raw in PowerShell (or plain `cat` in bash)
        # reads back exactly the token and nothing else.
        with open(args.save_to, "w", encoding="utf-8") as f:
            f.write(access_token)
        print()
        print(f"Saved raw access_token to: {args.save_to}")


if __name__ == "__main__":
    main()