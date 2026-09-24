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
import json
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

    def test_what_nobody_pinned_is_dropped(self):
        """A position the physics engine left behind is not kept."""
        self.db.save_layout({
            CORE: {"x": 1, "y": 1, "pinned": True},
            HOST: {"x": 2, "y": 2, "pinned": False},
            PSEUDO: {"x": 3, "y": 3, "pinned": False},
        })
        self.assertEqual(self.db.drop_unpinned_positions(), 2)
        self.assertEqual(list(self.db.layout()), [CORE])
        # …and there is nothing left to drop the next time
        self.assertEqual(self.db.drop_unpinned_positions(), 0)

    def test_the_last_reset_comes_from_the_journal(self):
        self.assertEqual(self.db.layout_cleared_at(), 0.0)
        self.db.add_event(100.0, "layout_cleared", "", "3 node(s)")
        self.db.add_event(200.0, "layout_saved", "", "3 node(s)")
        self.db.add_event(150.0, "layout_cleared", "", "3 node(s)")
        self.assertEqual(self.db.layout_cleared_at(), 150.0)


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

    def test_a_v072_layout_keeps_only_its_pins(self):
        """What v0.7.2 wrote: a few pins and a position for every other
        node, written once. The pins survive the upgrade, the rest goes
        at the first start."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v072.db"
            db = Database(path)
            db.save_layout({
                CORE: {"x": 10, "y": 20, "pinned": True},
                HOST: {"x": -500, "y": 900, "pinned": False},
                PSEUDO: {"x": -480, "y": 870, "pinned": False},
            })
            db._conn.close()

            reopened = Database(path)
            self.assertEqual(reopened.drop_unpinned_positions(), 2)
            saved = reopened.layout()
            self.assertEqual(list(saved), [CORE])
            self.assertEqual((saved[CORE]["x"], saved[CORE]["y"]), (10.0, 20.0))
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

    def test_an_open_page_is_told_about_a_reset(self):
        """A reset is announced, not inferred from rows going missing:
        a node released by an older page or forgotten by age takes its
        row with it just the same."""
        server.db.set_node_position(CORE, 1, 1, pinned=True)
        before = asyncio.run(server.api_layout())["cleared_at"]
        answer = asyncio.run(server.api_clear_layout())
        after = asyncio.run(server.api_layout())["cleared_at"]
        self.assertGreater(after, before)
        # the page that reset it gets the same moment, so it does not
        # take its own reset for somebody else's
        self.assertEqual(answer["cleared_at"], after)

    def test_forgetting_one_node_is_not_a_reset(self):
        server.db.set_node_position(CORE, 1, 1, pinned=True)
        before = asyncio.run(server.api_layout())["cleared_at"]
        asyncio.run(server.api_delete_node_position(CORE))
        self.assertEqual(asyncio.run(server.api_layout())["cleared_at"], before)

    def test_a_plain_save_still_writes_everything(self):
        """The old form stays for compatibility and for diag."""
        server.db.set_node_position(CORE, 1, 1, pinned=True)
        answer = self._put({CORE: {"x": 7, "y": 8}}, only_new=False)
        self.assertEqual(answer["stored"], [CORE])
        saved = server.db.layout()[CORE]
        self.assertEqual((saved["x"], saved["y"]), (7.0, 8.0))
        # …and still does not un-place what was put by hand
        self.assertTrue(saved["pinned"])


class HandPlacedTest(unittest.TestCase):
    """Pinning and releasing: one action, one journal line, no lost row."""

    def setUp(self):
        server.db.clear_layout()
        self._switches = server.state.switches
        server.state.switches = [{"ip": "10.0.0.10"}, {"ip": "10.0.0.21"}]

    def tearDown(self):
        server.db.clear_layout()
        server.state.switches = self._switches

    def _patch(self, nodes):
        body = server.LayoutBody(
            nodes={k: server.NodePosition(**v) for k, v in nodes.items()}
        )
        return asyncio.run(server.api_patch_positions(body))

    def _entries(self, event, since):
        return [
            e for e in server.db.journal(1000)
            if e["event"] == event and e["id"] > since
        ]

    def _last_id(self):
        rows = server.db.journal(1)
        return rows[0]["id"] if rows else 0

    def test_a_selection_pinned_at_once_is_one_entry(self):
        since = self._last_id()
        self._patch({
            CORE: {"x": 1, "y": 1},
            "sw:10.0.0.21": {"x": 2, "y": 2},
            HOST: {"x": 3, "y": 3},
        })
        entries = self._entries("layout_pinned", since)
        self.assertEqual(len(entries), 1)
        details = json.loads(entries[0]["details"])
        self.assertEqual(details["n"], 3)
        self.assertEqual(set(details["ids"]), {CORE, "sw:10.0.0.21", HOST})

    def test_a_long_list_is_counted_not_spelled_out(self):
        since = self._last_id()
        self._patch({f"host:aa:00:00:00:00:{n:02x}": {"x": n, "y": n}
                     for n in range(12)})
        details = json.loads(
            self._entries("layout_pinned", since)[0]["details"]
        )
        self.assertEqual(details["n"], 12)
        self.assertEqual(len(details["ids"]), server.LAYOUT_EVENT_IDS)

    def test_released_is_forgotten(self):
        """Only what a person placed is kept (v0.7.3). v0.7.2 kept the
        row with the pin cleared; that position went stale as soon as
        anything around the node moved. Released, the node is laid out
        from the pinned ones on the next load."""
        self._patch({CORE: {"x": 1, "y": 1}})
        since = self._last_id()
        self._patch({CORE: {"x": 40, "y": 50, "pinned": False}})
        self.assertNotIn(CORE, server.db.layout())
        self.assertEqual(len(self._entries("layout_released", since)), 1)
        self.assertEqual(self._entries("layout_pinned", since), [])

    def test_a_pin_and_a_release_in_one_action(self):
        self._patch({CORE: {"x": 1, "y": 1}})
        self._patch({
            CORE: {"x": 1, "y": 1, "pinned": False},
            "sw:10.0.0.21": {"x": 2, "y": 2},
        })
        self.assertEqual(list(server.db.layout()), ["sw:10.0.0.21"])

    def test_an_older_page_releasing_by_delete_is_journalled_too(self):
        self._patch({CORE: {"x": 1, "y": 1}})
        since = self._last_id()
        asyncio.run(server.api_delete_node_position(CORE))
        self.assertEqual(len(self._entries("layout_released", since)), 1)


if __name__ == "__main__":
    unittest.main()
