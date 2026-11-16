"""
test_auction_db.py - evidence for the 'evaluate' part of the project.
Run:  python -m unittest test_auction_db -v
"""
import os
import tempfile
import threading
import unittest

from auction_db import AuctionDB


class AuctionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "test.db")
        self.db = AuctionDB(self.path)
        self.db.add_user("seller1", "pass1", "seller")
        for name in ("alice", "bob", "carol"):
            self.db.add_user(name, "pass1", "bidder")
        # 100.00 start, +5 increment, 10 minutes, created at t=1000
        self.aid, _ = self.db.create_auction(
            "seller1", "Camera", 100, 10, 5, now=1000)

    def tearDown(self):
        self.dir.cleanup()

    # ---- accounts
    def test_login(self):
        self.assertEqual(self.db.check_login("alice", "pass1"), "bidder")
        self.assertIsNone(self.db.check_login("alice", "wrong"))
        self.assertIsNone(self.db.check_login("nobody", "pass1"))

    def test_duplicate_and_bad_usernames(self):
        self.assertFalse(self.db.add_user("alice", "pass1", "bidder")[0])
        self.assertFalse(self.db.add_user("a.b", "pass1", "bidder")[0])

    # ---- bid rules
    def test_bid_below_start_rejected(self):
        self.assertFalse(self.db.place_bid(self.aid, "alice", 99, now=1001)[0])

    def test_minimum_increment(self):
        self.assertTrue(self.db.place_bid(self.aid, "alice", 100, now=1001)[0])
        self.assertFalse(self.db.place_bid(self.aid, "bob", 104, now=1002)[0])
        self.assertTrue(self.db.place_bid(self.aid, "bob", 105, now=1003)[0])

    def test_seller_cannot_bid_own_item(self):
        self.assertFalse(self.db.place_bid(self.aid, "seller1", 200, now=1001)[0])

    def test_bad_input(self):
        for bad in ("abc", None, -5, 0, float("nan"), float("inf")):
            self.assertFalse(self.db.place_bid(self.aid, "alice", bad)[0])

    # ---- tie-break
    def test_equal_simultaneous_bids_first_processed_wins(self):
        self.assertTrue(self.db.place_bid(self.aid, "alice", 150, now=1001)[0])
        self.assertFalse(self.db.place_bid(self.aid, "bob", 150, now=1001)[0])
        closed = self.db.close_expired(now=1000 + 601)
        self.assertEqual(closed[0]["winner"], "alice")

    # ---- server clock / closing
    def test_late_bid_rejected_by_server_clock(self):
        ok, msg = self.db.place_bid(self.aid, "alice", 150, now=1000 + 600)
        self.assertFalse(ok)
        self.assertIn("closed", msg)

    def test_close_records_winner_and_blocks_more_bids(self):
        self.db.place_bid(self.aid, "alice", 120, now=1010)
        self.db.place_bid(self.aid, "bob", 130, now=1020)
        self.assertEqual(self.db.close_expired(now=1500), [])   # not yet
        closed = self.db.close_expired(now=1601)
        self.assertEqual((closed[0]["winner"], closed[0]["final_price"]),
                         ("bob", 130))
        self.assertFalse(self.db.place_bid(self.aid, "carol", 500, now=1602)[0])
        self.assertEqual(self.db.close_expired(now=1700), [])   # only once

    def test_no_bids_means_no_winner(self):
        closed = self.db.close_expired(now=1601)
        self.assertIsNone(closed[0]["winner"])

    # ---- history
    def test_history_keeps_rejected_bids(self):
        self.db.place_bid(self.aid, "alice", 120, now=1010)
        self.db.place_bid(self.aid, "bob", 50, now=1011)
        log = self.db.transaction_log()
        self.assertEqual([b["accepted"] for b in log], [0, 1])
        self.assertIn("too low", log[0]["note"])

    # ---- simultaneous bidders (separate connections = separate clients)
    def test_concurrent_bids_stay_consistent(self):
        def worker(name, amounts):
            conn = AuctionDB(self.path)
            for amt in amounts:
                conn.place_bid(self.aid, name, amt)

        threads = [threading.Thread(
            target=worker,
            args=(f"u{i}", [100 + 5 * ((i * 7 + k * 3) % 60) for k in range(25)]))
            for i in range(8)]
        # bids use the real clock; auction was created at t=1000, so make a live one
        self.aid, _ = self.db.create_auction("seller1", "Live", 100, 10, 5)
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        accepted = self.db.db.execute(
            "SELECT amount FROM bids WHERE auction_id=? AND accepted=1 "
            "ORDER BY id", (self.aid,)).fetchall()
        amounts = [r["amount"] for r in accepted]
        self.assertTrue(amounts, "some bids should be accepted")
        for prev, cur in zip(amounts, amounts[1:]):          # never goes down,
            self.assertGreaterEqual(cur, prev + 5)           # always +increment
        view = self.db.view(self.aid)
        self.assertEqual(view["high_bid"], max(amounts))
        total = self.db.db.execute(
            "SELECT COUNT(*) FROM bids WHERE auction_id=?",
            (self.aid,)).fetchone()[0]
        self.assertEqual(total, 8 * 25, "no bid attempt may be lost")


if __name__ == "__main__":
    unittest.main()
