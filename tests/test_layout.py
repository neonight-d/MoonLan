"""The map keeps its shape.

A network map is a shared object. Two people looking at one network
have to see one picture, or "the switch at the bottom left" stops
meaning anything — which is why coordinates live in the database and
not in somebody's localStorage, next to their language and their
freeze toggle.

Three rules this pins down, all of them easy to get backwards:

- a full save records where everything is and must not quietly
  un-place what somebody put by hand;
- a node that vanished from the network keeps its position. A device
  switched off for the night was not taken away, and coming back it
  belongs where it was;
- so the only thing that forgets a position is age, and only for a
  node the map no longer has.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

from moonlan.db import Database

CORE = "sw:10.0.0.10"
HOST = "host:aa:bb:cc:00:00:01"
# four of the seven node-id kinds carry a port name, slash and all
PSEUDO = "pseudo:10.0.0.21:Gi0/14"


class LayoutStoreTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def test_an_empty_database_has_an_empty_layout(self):
        self.assertEqual(self.db.layout(), {})
        self.assertEqual(self.db.layout_saved_at(), 0.0)

    def test_a_snapshot_round_trips(self):
        self.db.save_layout({
            CORE: {"x": 10, "y": -20},
            PSEUDO: {"x": 1.5, "y": 2.5, "pinned": True},
        })
        saved = self.db.layout()
        self.assertEqual((saved[CORE]["x"], saved[CORE]["y"]), (10.0, -20.0))
        self.assertFalse(saved[CORE]["pinned"])
        self.assertTrue(saved[PSEUDO]["pinned"])
        self.assertGreater(self.db.layout_saved_at(), 0)

    def test_a_full_save_does_not_unpin_what_was_placed_by_hand(self):
        """The button says "save the layout", not "forget my work"."""
        self.db.set_node_position(CORE, 5, 5)
        self.assertTrue(self.db.layout()[CORE]["pinned"])
        self.db.save_layout({CORE: {"x": 7, "y": 8}})
        saved = self.db.layout()[CORE]
        self.assertEqual((saved["x"], saved["y"]), (7.0, 8.0))
        self.assertTrue(saved["pinned"])

    def test_a_save_can_unpin_deliberately(self):
        self.db.set_node_position(CORE, 5, 5)
        self.db.save_layout({CORE: {"x": 5, "y": 5, "pinned": False}})
        self.assertFalse(self.db.layout()[CORE]["pinned"])

    def test_forgetting_one_node(self):
        self.db.set_node_position(CORE, 1, 1)
        self.assertTrue(self.db.forget_node_position(CORE))
        self.assertNotIn(CORE, self.db.layout())
        # …and saying so when there was nothing to forget
        self.assertFalse(self.db.forget_node_position(CORE))

    def test_only_new_leaves_a_pinned_node_alone(self):
        """The race from the v0.7.1 addendum, closed in the database.

        A page opened before a node appeared thinks the node is new and
        sends it with pinned: false. Somebody else has pinned it in the
        meantime. Neither the pin nor the place may change.
        """
        self.db.set_node_position(HOST, 1234, -567, pinned=True)
        stored, existing = self.db.add_new_positions(
            {HOST: {"x": 125, "y": -27, "pinned": False}}
        )
        self.assertEqual(stored, [])
        saved = self.db.layout()[HOST]
        self.assertEqual((saved["x"], saved["y"]), (1234.0, -567.0))
        self.assertTrue(saved["pinned"])
        # …and the caller is told what is there instead
        self.assertEqual(
            (existing[HOST]["x"], existing[HOST]["y"],
             existing[HOST]["pinned"]),
            (1234.0, -567.0, True),
        )

    def test_only_new_stores_what_is_really_new(self):
        self.db.set_node_position(CORE, 5, 5, pinned=False)
        stored, existing = self.db.add_new_positions({
            CORE: {"x": 9, "y": 9, "pinned": False},
            HOST: {"x": 1, "y": 2, "pinned": False},
        })
        self.assertEqual(stored, [HOST])
        self.assertEqual(list(existing), [CORE])
        # an unpinned row is somebody's position too: not overwritten
        self.assertEqual(self.db.layout()[CORE]["x"], 5.0)
        self.assertEqual(self.db.layout()[HOST]["y"], 2.0)

    def test_clearing_everything(self):
        self.db.save_layout({CORE: {"x": 1, "y": 1}, HOST: {"x": 2, "y": 2}})
        self.assertEqual(self.db.clear_layout(), 2)
        self.assertEqual(self.db.layout(), {})


class PurgeTest(unittest.TestCase):
    """What forgets a position, and what must not."""

    def setUp(self):
        self.db = Database(":memory:")
        self.db.save_layout({
            CORE: {"x": 1, "y": 1},
            HOST: {"x": 2, "y": 2},
            PSEUDO: {"x": 3, "y": 3},
        })
        # …placed a hundred days ago
        old = time.time() - 100 * 86400
        with self.db._lock, self.db._conn:
            self.db._conn.execute("UPDATE layout SET updated_at = ?", (old,))

    def test_a_node_still_on_the_map_keeps_its_place_however_old(self):
        gone = self.db.purge_layout(90, {CORE, HOST, PSEUDO})
        self.assertEqual(gone, [])
        self.assertEqual(len(self.db.layout()), 3)

    def test_only_old_and_gone_is_forgotten(self):
        gone = self.db.purge_layout(90, {CORE})
        self.assertEqual(sorted(gone), sorted([HOST, PSEUDO]))
        self.assertEqual(list(self.db.layout()), [CORE])

    def test_a_device_off_for_the_night_comes_back_to_its_place(self):
        """Gone, but nowhere near old enough to forget."""
        self.db.set_node_position(HOST, 9, 9)  # placed just now
        gone = self.db.purge_layout(90, {CORE})
        self.assertNotIn(HOST, gone)
        self.assertEqual(self.db.layout()[HOST]["x"], 9.0)

    def test_zero_days_switches_the_housekeeping_off(self):
        self.assertEqual(self.db.purge_layout(0, set()), [])
        self.assertEqual(len(self.db.layout()), 3)


class MigrationTest(unittest.TestCase):
    """An old database file opens and gains the table."""

    def test_a_database_without_the_table_gets_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            db = Database(path)
            db.save_layout({CORE: {"x": 1, "y": 2}})
            db._conn.close()
            # the shape of a database written by v0.6.15
            conn = sqlite3.connect(path)
            conn.execute("DROP TABLE layout")
            conn.commit()
            conn.close()

            reopened = Database(path)
            self.assertEqual(reopened.layout(), {})
            reopened.save_layout({CORE: {"x": 3, "y": 4}})
            self.assertEqual(reopened.layout()[CORE]["x"], 3.0)
            reopened._conn.close()


# One shared configuration for every test that imports the service —
# see tests/service_fixture.py for why it cannot be per-module.
# `unittest discover -s tests` puts this directory on sys.path;
# `python -m unittest tests.test_layout` does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402,F401

from moonlan import server  # noqa: E402


class NodeIdTest(unittest.TestCase):
    """Which nodes the map actually draws.

    A device that answers LLDP and is also in somebody's MAC table is
    drawn AS the bridge node, not beside it — so it has no node of its
    own and no position of its own. Counting it left two entries in
    "not in the saved layout" that nobody could ever place.
    """

    def setUp(self):
        self._switches = server.state.switches
        self._hosts = server.state.hosts
        self._bridges = server.state.bridges
        server.state.switches = [{"ip": "10.0.0.10"}]
        server.state.bridges = [{"id": "bridge:aa:bb:cc:00:00:02"}]
        server.state.hosts = [
            {"mac": "aa:bb:cc:00:00:01"},
            {"mac": "aa:bb:cc:00:00:02",
             "merged_into": "bridge:aa:bb:cc:00:00:02"},
        ]

    def tearDown(self):
        server.state.switches = self._switches
        server.state.hosts = self._hosts
        server.state.bridges = self._bridges

    def test_a_host_drawn_as_a_bridge_is_not_a_node_of_its_own(self):
        ids = server._layout_node_ids()
        self.assertIn("sw:10.0.0.10", ids)
        self.assertIn("host:aa:bb:cc:00:00:01", ids)
        self.assertIn("bridge:aa:bb:cc:00:00:02", ids)
        self.assertNotIn("host:aa:bb:cc:00:00:02", ids)


class OnlyNewApiTest(unittest.TestCase):
    """PUT /api/layout with only_new, through the handler itself."""

    def setUp(self):
        server.db.clear_layout()

    def tearDown(self):
        server.db.clear_layout()

    def _put(self, nodes, only_new):
        body = server.LayoutBody(
            nodes={k: server.NodePosition(**v) for k, v in nodes.items()},
            only_new=only_new,
        )
        return asyncio.run(server.api_put_layout(body))

    def test_the_automatic_save_cannot_unpin(self):
        server.db.set_node_position(HOST, 1234, -567, pinned=True)
        answer = self._put(
            {HOST: {"x": 125, "y": -27, "pinned": False}}, only_new=True
        )
        self.assertEqual(answer["stored"], [])
        self.assertTrue(answer["existing"][HOST]["pinned"])
        saved = server.db.layout()[HOST]
        self.assertEqual(
            (saved["x"], saved["y"], saved["pinned"]), (1234.0, -567.0, True)
        )

    def test_nothing_stored_is_not_a_journal_entry(self):
        server.db.set_node_position(HOST, 1, 1, pinned=True)
        before = len(server.db.journal(1000))
        self._put({HOST: {"x": 2, "y": 2, "pinned": False}}, only_new=True)
        self.assertEqual(len(server.db.journal(1000)), before)

    def test_a_plain_save_still_writes_everything(self):
        """The old form stays for compatibility and for diag."""
        server.db.set_node_position(CORE, 1, 1, pinned=True)
        answer = self._put({CORE: {"x": 7, "y": 8}}, only_new=False)
        self.assertEqual(answer["stored"], [CORE])
        saved = server.db.layout()[CORE]
        self.assertEqual((saved["x"], saved["y"]), (7.0, 8.0))
        # …and still does not un-place what was put by hand
        self.assertTrue(saved["pinned"])


if __name__ == "__main__":
    unittest.main()
