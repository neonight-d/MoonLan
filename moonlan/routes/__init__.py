"""The routes of the API, by subject (v0.8.13).

Taken out of server.py word for word, the third stage of taking it
apart (docs/ARCHITECTURE.md). Each module has its own APIRouter, and
server.py includes them in one place, after the sign-in routes and
before the static page. The routes that need the poller —
/api/topology, /api/polling, /api/scan — stay in server.py until the
poller leaves it. Nothing here imports moonlan.server: what a route
needs of the process it reads from rt (runtime.py) when it runs.
"""
