"""The routes of the API, by subject (v0.8.13).

Taken out of server.py word for word, the third stage of taking it
apart (docs/ARCHITECTURE.md); /api/topology, /api/polling and /api/scan
followed with the poller in the fourth. Each module has its own
APIRouter, and server.py includes them in one place, after the sign-in
routes and before the static page. Nothing here imports
moonlan.server: what a route needs of the process it reads from rt
(runtime.py) when it runs; only routes/polling.py needs the poller.

The package imports none of its modules (v0.8.14): server.py imports
the ones it includes, and importing one module of routes loads only
what that module needs.
"""


def include(app, router) -> None:
    """The routes of a module, each its own entry in app.routes.

    Not app.include_router: an included router is one opaque entry in
    app.routes, and the guard — like the test that every route is in
    the rights table — has to see each route by its own path (so
    signin.add_routes adds its routes to the app itself). The routes are
    the router's own, made by its decorators; the order is kept.
    """
    app.router.routes.extend(router.routes)
