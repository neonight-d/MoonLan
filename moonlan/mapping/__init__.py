"""The map, piece by piece (v0.8.14).

What a scan's readings become on the map, taken out of server.py with
the poller (docs/ARCHITECTURE.md): the hosts and their places in
hosts.py, the switches and the spanning tree in switches.py, LLDP and
the bridges in bridges.py, a router drawn as one node in
router_nodes.py, the notes and saved positions kept in step with the
map in upkeep.py. poller._build_map calls them; start reading there.
The package imports none of its modules, and nothing here imports
moonlan.server or the poller.
"""
