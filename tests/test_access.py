"""Rights: one table for every route, checked on every request (v0.7.4).

The first test here is the one that matters most. Forgetting the
check on one route is the mistake rights systems actually die of; this
test lists every route the app has and fails on any that is not
written down in access.RULES.
"""

import contextlib
import io
import time
import unittest
from unittest import mock

from starlette.routing import Mount

import service_fixture  # noqa: F401  (sets MOONLAN_CONFIG first)
from asgi_client import call
from moonlan import access, auth, server, signin, users
from moonlan.access import Principal


def app_route_keys():
    """Every route of the app, as the guard names it — FastAPI's own
    pages included: they are routes of the app like any other."""
    keys = set()
    for route in server.app.routes:
        if isinstance(route, Mount):
            keys.add(access.STATIC)
        elif getattr(route, "methods", None):
            for method in route.methods:
                # HEAD has GET's rights (signin.route_key)
                if method != "HEAD":
                    keys.add(f"{method} {route.path}")
        else:
            keys.add(("unknown", repr(route)))
    return keys


class TableCompletenessTest(unittest.TestCase):
    def test_every_route_is_in_the_table(self):
        missing = sorted(
            key for key in app_route_keys()
            if isinstance(key, str) and key not in access.RULES
        )
        self.assertEqual(
            missing, [],
            "routes with no entry in access.RULES — decide who may call "
            "them and write it down there",
        )

    def test_nothing_else_is_served(self):
        # a route of a kind this test cannot name (a websocket, say)
        # would escape the test above
        unknown = [k for k in app_route_keys() if isinstance(k, tuple)
                   and k[0] == "unknown"]
        self.assertEqual(unknown, [])

    def test_the_table_names_only_real_routes(self):
        stale = sorted(set(access.RULES) - app_route_keys())
        self.assertEqual(stale, [], "entries for routes that do not exist")

    def test_not_in_the_table_is_for_administrators(self):
        self.assertEqual(access.rule_for("POST /api/something-new"), access.ADMIN)
        viewer = Principal("v", "viewer")
        user = Principal("u", "user")
        admin = Principal("a", "admin")
        key = "POST /api/something-new"
        self.assertEqual(access.decide(key, user, True).status, 403)
        self.assertEqual(access.decide(key, viewer, True).status, 403)
        self.assertIsNone(access.decide(key, admin, True))


class MatrixTest(unittest.TestCase):
    """The matrix the operator approved, row by row."""

    VIEWER = Principal("v", "viewer")
    USER = Principal("u", "user")
    ADMIN = Principal("a", "admin")

    def allowed(self, key):
        return [
            who.role for who in (self.VIEWER, self.USER, self.ADMIN)
            if access.decide(key, who, True) is None
        ]

    def test_looking_is_for_everybody(self):
        for key in ("GET /api/topology", "GET /api/switch/{ip}/ports",
                    "GET /api/stp", "GET /api/alarms", "GET /api/journal",
                    "GET /api/node-menu", "GET /api/layout",
                    "GET /api/search"):
            self.assertEqual(self.allowed(key), ["viewer", "user", "admin"], key)

    def test_the_server_acting_is_for_users(self):
        for key in ("POST /api/actions", "GET /api/actions/{job_id}",
                    "POST /api/scan", "PATCH /api/layout",
                    "PATCH /api/layout/{node_id:path}",
                    "DELETE /api/layout/{node_id:path}",
                    "POST /api/alarms/{alarm_id}/clear",
                    "PATCH /api/host/{mac}"):
            self.assertEqual(self.allowed(key), ["user", "admin"], key)

    def test_the_whole_picture_is_for_administrators(self):
        for key in ("PUT /api/layout", "DELETE /api/layout"):
            self.assertEqual(self.allowed(key), ["admin"], key)

    def test_a_refusal_says_which_role_it_needs(self):
        refusal = access.decide("DELETE /api/layout", self.USER, True)
        self.assertEqual(refusal.status, 403)
        self.assertEqual(refusal.body["need"], "admin")
        self.assertEqual(access.decide("GET /api/topology", None, True).status, 401)

    def test_all_open_while_sign_in_is_off(self):
        for key in ("GET /api/topology", "POST /api/scan", "DELETE /api/layout",
                    "POST /api/something-new"):
            self.assertIsNone(access.decide(key, None, False), key)


class ServiceTest(unittest.TestCase):
    """Through the real app: the guard in front of the real handlers."""

    def setUp(self):
        self.db = server.accounts
        self.clean()
        self.addCleanup(self.clean)

    def clean(self):
        with self.db._lock, self.db._conn:
            self.db._conn.execute("DELETE FROM sessions")
            self.db._conn.execute("DELETE FROM users")
            self.db._conn.execute("DELETE FROM layout")

    def account(self, name, role, second_factor=True):
        row = self.db.add_user(name, role, auth.hash_password("x" * 12))
        token = auth.new_token()
        self.db.add_session(
            auth.token_hash(token), row["id"], time.time(),
            second_factor=second_factor,
        )
        return f"{signin.SESSION_COOKIE}={token}"

    def test_open_until_an_administrator_exists(self):
        self.assertEqual(call(server.app, "GET", "/api/topology").status, 200)
        self.assertEqual(
            call(server.app, "DELETE", "/api/layout", origin=False).status,
            200,
        )
        # a viewer alone does not switch sign-in on
        self.account("vera", "viewer")
        self.assertEqual(call(server.app, "GET", "/api/topology").status, 200)

    def test_closed_once_an_administrator_exists(self):
        self.account("anton", "admin")
        answer = call(server.app, "GET", "/api/topology")
        self.assertEqual(answer.status, 401)
        self.assertEqual(answer.json(), {"error": "sign_in_required"})
        # the page and its files still load: the sign-in form is on it
        self.assertEqual(call(server.app, "GET", "/").status, 200)
        self.assertEqual(call(server.app, "GET", "/app.js").status, 200)

    def test_viewer_user_admin(self):
        admin = self.account("anton", "admin")
        user = self.account("vera", "user")
        viewer = self.account("wall", "viewer")
        for cookie in (viewer, user, admin):
            self.assertEqual(
                call(server.app, "GET", "/api/topology", cookie=cookie).status,
                200,
            )
        scan = call(server.app, "POST", "/api/scan", cookie=viewer)
        self.assertEqual(scan.status, 403)
        self.assertEqual(scan.json()["need"], "user")
        body = {"nodes": {"sw:10.0.0.1": {"x": 1, "y": 2}}}
        self.assertEqual(
            call(server.app, "PATCH", "/api/layout", cookie=viewer,
                 json_body=body).status, 403,
        )
        self.assertEqual(
            call(server.app, "PATCH", "/api/layout", cookie=user,
                 json_body=body).status, 200,
        )
        reset = call(server.app, "DELETE", "/api/layout", cookie=user)
        self.assertEqual((reset.status, reset.json()["need"]), (403, "admin"))
        self.assertEqual(
            call(server.app, "DELETE", "/api/layout", cookie=admin).status, 200
        )

    def test_the_journal_names_who_did_it(self):
        self.account("anton", "admin")
        user = self.account("vera", "user")
        call(server.app, "PATCH", "/api/layout", cookie=user,
             json_body={"nodes": {"sw:10.0.0.1": {"x": 1, "y": 2}}})
        event = server.db.journal(1)[0]
        self.assertEqual((event["event"], event["user"]),
                         ("layout_pinned", "vera"))

    def test_origin(self):
        self.account("anton", "admin")
        user = self.account("vera", "user")
        body = {"nodes": {"sw:10.0.0.1": {"x": 1, "y": 2}}}
        # another site, a sandboxed page, and no Origin at all
        for origin in ("http://evil.example", "null", False):
            answer = call(server.app, "PATCH", "/api/layout", cookie=user,
                          json_body=body, origin=origin)
            self.assertEqual(answer.status, 403, origin)
            self.assertEqual(answer.json(), {"error": "bad_origin"})

    def test_health_says_nothing_about_the_network(self):
        self.account("anton", "admin")
        answer = call(server.app, "GET", "/api/health")
        self.assertEqual(answer.status, 200)
        self.assertEqual(set(answer.json()), {"status", "version"})

    def test_an_admin_without_a_second_factor_does_nothing_else(self):
        admin = self.account("anton", "admin", second_factor=False)
        answer = call(server.app, "GET", "/api/topology", cookie=admin)
        self.assertEqual(answer.status, 403)
        self.assertEqual(answer.json(),
                         {"error": "step_required", "step": "totp"})

    def test_a_disabled_account_is_signed_out(self):
        self.account("anton", "admin")
        user = self.account("vera", "user")
        with self.db._lock, self.db._conn:
            self.db._conn.execute(
                "UPDATE users SET disabled = 1 WHERE name = 'vera'"
            )
        self.assertEqual(
            call(server.app, "GET", "/api/topology", cookie=user).status, 401
        )


class OpenModeTest(unittest.TestCase):
    """Sign-in is off until the first administrator exists, and on from
    the next request after `python -m moonlan.users add` — which opens
    the database file separately, as it does beside a running service."""

    def setUp(self):
        ServiceTest.clean(self)

    def tearDown(self):
        ServiceTest.clean(self)

    @property
    def db(self):
        return server.accounts

    def cli(self, *argv):
        out = io.StringIO()
        with mock.patch("getpass.getpass",
                        return_value="correct horse battery"), \
                contextlib.redirect_stdout(out):
            self.assertEqual(users.main(list(argv)), 0, out.getvalue())

    def test_from_open_to_closed_without_a_restart(self):
        me = call(server.app, "GET", "/api/auth/me")
        self.assertEqual(me.status, 200)
        self.assertIs(me.json()["sign_in"], False)
        self.assertIn("--role admin", me.json()["create_admin"])
        self.assertEqual(call(server.app, "GET", "/api/topology").status, 200)

        self.cli("add", "wall", "--role", "viewer")
        # a viewer does not switch anything on
        self.assertEqual(call(server.app, "GET", "/api/topology").status, 200)

        self.cli("add", "anton", "--role", "admin")
        self.assertEqual(call(server.app, "GET", "/api/topology").status, 401)
        me = call(server.app, "GET", "/api/auth/me")
        self.assertEqual((me.status, me.json()["sign_in"]), (401, True))

    def test_accounts_are_never_open(self):
        with mock.patch.dict(access.RULES, {"GET /api/users": access.ACCOUNTS}):
            refusal = access.decide("GET /api/users", None, False)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.body, {"error": "sign_in_off"})

    def test_the_log_says_which(self):
        with self.assertLogs("moonlan", "WARNING") as logged:
            server.sign_in.log_state()
        self.assertIn("python -m moonlan.users add", logged.output[0])
        self.cli("add", "anton", "--role", "admin")
        with self.assertLogs("moonlan", "INFO") as logged:
            server.sign_in.log_state()
        self.assertIn("Sign-in: on", logged.output[0])


class JobOwnerTest(unittest.TestCase):
    """A ping's result is seen by who started it and by an administrator."""

    def setUp(self):
        self.job = server.probes.Job(
            id="job1", action="ping", targets=[], owner="vera"
        )
        server.jobs._jobs["job1"] = self.job
        self.addCleanup(server.jobs._jobs.pop, "job1", None)

    def ask(self, who):
        token = signin._current.set(who)
        try:
            import asyncio
            answer = asyncio.run(server.api_action_state("job1"))
        finally:
            signin._current.reset(token)
        return getattr(answer, "status_code", 200)

    def test_owner_admin_and_others(self):
        self.assertEqual(self.ask(Principal("vera", "user")), 200)
        self.assertEqual(self.ask(Principal("anton", "admin")), 200)
        self.assertEqual(self.ask(Principal("boris", "user")), 404)
        # sign-in off: nobody is anybody
        self.assertEqual(self.ask(None), 200)


if __name__ == "__main__":
    unittest.main()
