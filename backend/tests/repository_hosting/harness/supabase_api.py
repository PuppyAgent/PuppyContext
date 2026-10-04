"""Real HTTP/JWT fixtures, restricted to this runner's disposable local stack."""

import re
import uuid
from urllib.parse import urlsplit

import httpx


class SupabaseAPI:
    def __init__(self, environ):
        url = environ.get("SUPABASE_URL", "")
        try:
            parsed = urlsplit(url)
            owned = (
                environ.get("HOSTING_TEST_SUPABASE") == "1"
                and re.fullmatch(r"puppy-baseline-[a-z0-9]{8}", environ.get("HOSTING_TEST_STACK", ""))
                and parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "::1")
                and parsed.port and 1024 <= parsed.port <= 65535
                and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
                and parsed.path in ("", "/")
            )
        except ValueError:
            owned = False
        if not owned:
            raise RuntimeError("HTTP tests require an owned loopback Supabase stack")
        self._tokens = {
            "anon": environ["HOSTING_TEST_ANON_KEY"],
            "service_role": environ["SUPABASE_SERVICE_ROLE_KEY"],
        }
        self.client = httpx.Client(base_url=url, timeout=15, trust_env=False, follow_redirects=False)

    def headers(self, role):
        key = self._tokens["service_role" if role == "service_role" else "anon"]
        return {"apikey": key, "Authorization": "Bearer " + self._tokens[role]}

    def request(self, method, path, *, role="service_role", **kwargs):
        return self.client.request(method, path, headers=self.headers(role), **kwargs)

    def authenticate(self):
        # Real GoTrue creates, confirms and logs in a synthetic user. No JWT
        # signing stub, email delivery or pre-existing account is involved.
        credentials = {"email": f"hosting-{uuid.uuid4().hex}@example.test", "password": uuid.uuid4().hex}
        created = self.request("POST", "/auth/v1/admin/users", json=credentials | {"email_confirm": True})
        assert created.status_code == 200
        user_id = created.json()["id"]
        login = self.request("POST", "/auth/v1/token?grant_type=password", role="anon", json=credentials)
        assert login.status_code == 200
        self._tokens["authenticated"] = login.json()["access_token"]
        user = self.request("GET", "/auth/v1/user", role="authenticated")
        assert user.status_code == 200
        assert user.json()["id"] == user_id

    def close(self):
        self.client.close()
