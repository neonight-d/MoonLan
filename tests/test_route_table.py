"""The routes of the app, frozen (v0.8.13).

Stage 3 of taking server.py apart moved 43 of its 46 routes into
moonlan/routes/, stage 4 (v0.8.14) the other three, and server.py
declares none. What the app serves did not change, and must not by
accident: the same methods, paths and handlers as before the move
(a071646), each its own entry in app.routes — the guard finds the
rights of a request by the path of the route that will serve it
(signin.route_key), and an included router would be one opaque entry —
and the static page last, so that it shadows no /api/* route.

The modules are put into the app one after the other, so the order of
routes between two of them is not the order server.py had. That order
decides nothing as long as no path of one module could be served by a
route of another — which is held here too.

Run with:  python -m unittest discover -s tests
"""

import ast
import importlib
import pkgutil
import re
import sys
import unittest
from pathlib import Path

from starlette.routing import Mount

sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402,F401

from moonlan import routes, server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# method, path, handler — every route of the app at a071646 (v0.8.12)
FROZEN = """
GET    /                                    index_page
GET    /api/about                           api_about
GET    /api/about/license                   api_about_license
GET    /api/actions                         api_actions_state
POST   /api/actions                         api_start_action
GET    /api/actions/{job_id}                api_action_state
GET    /api/alarms                          api_alarms
POST   /api/alarms/{alarm_id}/clear         api_clear_alarm
POST   /api/auth/login                      auth_login
POST   /api/auth/logout                     auth_logout
GET    /api/auth/me                         auth_me
POST   /api/auth/passkey/begin              auth_passkey_begin
POST   /api/auth/passkey/finish             auth_passkey_finish
POST   /api/auth/passkeys/begin             auth_passkeys_begin
POST   /api/auth/passkeys/finish            auth_passkeys_finish
DELETE /api/auth/passkeys/{key_id}          auth_passkeys_remove
POST   /api/auth/password                   auth_password
POST   /api/auth/totp                       auth_totp
POST   /api/auth/totp/confirm               auth_totp_confirm
POST   /api/auth/totp/setup                 auth_totp_setup
GET    /api/health                          api_health
PATCH  /api/host/{mac}                      api_patch_host
GET    /api/journal                         api_journal
DELETE /api/layout                          api_clear_layout
GET    /api/layout                          api_layout
PATCH  /api/layout                          api_patch_positions
PUT    /api/layout                          api_put_layout
DELETE /api/layout/{node_id:path}           api_delete_node_position
PATCH  /api/layout/{node_id:path}           api_patch_node_position
GET    /api/me/notifications                api_my_notifications
POST   /api/me/notifications/email          api_my_email
POST   /api/me/notifications/email/confirm  api_my_email_confirm
GET    /api/me/notifications/telegram       api_my_telegram_state
POST   /api/me/notifications/telegram       api_my_telegram
DELETE /api/me/notifications/{channel}      api_my_notifications_remove
PATCH  /api/me/notifications/{channel}      api_my_notifications_change
POST   /api/me/notifications/{channel}/test api_my_notifications_test
GET    /api/node-menu                       api_node_menu
GET    /api/notes                           api_notes
GET    /api/notes/export                    api_export_notes
POST   /api/notes/import                    api_import_notes
DELETE /api/notes/{node_id:path}            api_delete_note
PUT    /api/notes/{node_id:path}            api_put_note
POST   /api/notes/{node_id:path}/move       api_move_note
GET    /api/polling                         api_polling
POST   /api/scan                            api_scan
GET    /api/search                          api_search
GET    /api/settings                        api_settings
PUT    /api/settings                        api_settings_save
POST   /api/settings/test/{channel}         api_settings_test
DELETE /api/settings/{key}                  api_settings_reset
GET    /api/skipped-oids                    api_skipped_oids
GET    /api/status                          api_status
GET    /api/stp                             api_stp
GET    /api/switch/{ip}/ports               api_switch_ports
GET    /api/topology                        api_topology
GET    /api/users                           users_list
POST   /api/users                           users_add
DELETE /api/users/{name}                    users_delete
PATCH  /api/users/{name}                    users_change
POST   /api/users/{name}/logout             users_logout
DELETE /api/users/{name}/notifications      api_user_notifications_remove
DELETE /api/users/{name}/passkeys/{key_id}  users_remove_passkey
POST   /api/users/{name}/password           users_password
POST   /api/users/{name}/reset-passkeys     users_reset_passkeys
POST   /api/users/{name}/reset-totp         users_reset_totp
POST   /api/users/{name}/unlock             users_unlock
GET    /docs                                swagger_ui_html
HEAD   /docs                                swagger_ui_html
GET    /docs/oauth2-redirect                swagger_ui_redirect
HEAD   /docs/oauth2-redirect                swagger_ui_redirect
GET    /index.html                          index_page
GET    /openapi.json                        openapi
HEAD   /openapi.json                        openapi
GET    /redoc                               redoc_html
HEAD   /redoc                               redoc_html
"""

def table() -> set[tuple[str, str, str]]:
    return {
        (method, route.path, route.name)
        for route in server.app.routes if not isinstance(route, Mount)
        for method in route.methods
    }


def sample(path: str) -> str:
    """A path the route answers: each parameter given as "x"."""
    return re.sub(r"\{[^}]+\}", "x", path)


class RouteTableTest(unittest.TestCase):
    def test_the_same_routes_as_before(self):
        frozen = {tuple(line.split()) for line in FROZEN.strip().splitlines()}
        self.assertEqual(len(frozen), 76)
        self.assertEqual(table(), frozen)

    def test_each_route_is_its_own_entry(self):
        for route in server.app.routes[:-1]:
            with self.subTest(route=repr(route)[:80]):
                self.assertTrue(getattr(route, "path", None), "no path of its own")
                self.assertTrue(callable(getattr(route, "endpoint", None)))

    def test_the_static_page_comes_last(self):
        last = server.app.routes[-1]
        self.assertIsInstance(last, Mount)
        self.assertEqual((last.path, last.name), ("", "web"))
        self.assertEqual(
            [r for r in server.app.routes if isinstance(r, Mount)], [last]
        )
        self.assertEqual(server.WEB_DIR, ROOT / "web")

    def test_no_route_is_declared_in_server(self):
        tree = ast.parse((ROOT / "moonlan" / "server.py").read_text(encoding="utf-8"))
        declared = set()
        for node in ast.walk(tree):
            for deco in getattr(node, "decorator_list", []):
                if (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                        and isinstance(deco.func.value, ast.Name)
                        and deco.func.value.id == "app"):
                    declared.add((deco.func.attr.upper(), deco.args[0].value))
        self.assertEqual(declared, set())

    def test_every_module_of_routes_is_in_the_app(self):
        served = {id(route) for route in server.app.routes}
        modules = 0
        for info in pkgutil.iter_modules(routes.__path__):
            module = importlib.import_module(f"moonlan.routes.{info.name}")
            if not hasattr(module, "router"):
                continue
            modules += 1
            with self.subTest(module=info.name):
                self.assertTrue(module.router.routes)
                for route in module.router.routes:
                    self.assertIn(id(route), served, route.path)
                    self.assertEqual(route.endpoint.__module__, module.__name__)
        self.assertEqual(modules, 8)

    def test_the_order_between_modules_decides_nothing(self):
        """No path a route of one module answers is matched by a route
        of another, whatever the method: neither which route serves a
        request nor which one answers 405 can depend on their order."""
        found = [r for r in server.app.routes if not isinstance(r, Mount)]
        for one in found:
            for other in found:
                if one.endpoint.__module__ == other.endpoint.__module__:
                    continue
                with self.subTest(one=one.path, other=other.path):
                    self.assertIsNone(other.path_regex.match(sample(one.path)))


if __name__ == "__main__":
    unittest.main()
