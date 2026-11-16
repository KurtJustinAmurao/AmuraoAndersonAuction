"""
auction_db.py - all auction rules and storage (SQLite).

No Banyan code lives here, so this file can be tested on its own.
The server calls these functions; clients never touch the database.
"""
import hashlib
import hmac
import math
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    salt          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK(role IN ('admin','seller','bidder'))
);
CREATE TABLE IF NOT EXISTS auctions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    seller        TEXT NOT NULL,
    item          TEXT NOT NULL,
    start_price   REAL NOT NULL,
    min_increment REAL NOT NULL DEFAULT 1,
    closes_at     REAL NOT NULL,              -- SERVER time, epoch seconds
    status        TEXT NOT NULL DEFAULT 'open',   -- open / closed
    winner        TEXT,
    final_price   REAL
);
CREATE TABLE IF NOT EXISTS bids (             -- persistent bid history
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id INTEGER NOT NULL,
    bidder     TEXT NOT NULL,
    amount     REAL NOT NULL,
    placed_at  REAL NOT NULL,                 -- SERVER time
    accepted   INTEGER NOT NULL,              -- 1 accepted, 0 rejected
    note       TEXT                           -- reason when rejected
);
"""

# No dots allowed: usernames are used inside Banyan topic names.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,20}$")


class AuctionDB:
    def __init__(self, path="auction.db"):
        # isolation_level=None -> we control transactions ourselves (see _tx)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    @contextmanager
    def _tx(self):
        """One atomic transaction. BEGIN IMMEDIATE takes the write lock up
        front, so 'read the top bid, then insert' can never interleave with
        another bid. Commits on success, rolls back on any error."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------ users
    @staticmethod
    def _hash(password, salt_hex):
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), 100_000).hex()

    def add_user(self, username, password, role):
        if not isinstance(username, str) or not USERNAME_RE.match(username):
            return False, "Username must be 3-20 letters, digits, _ or -"
        if role not in ("admin", "seller", "bidder"):
            return False, "Invalid role"
        if not isinstance(password, str) or len(password) < 4:
            return False, "Password must be at least 4 characters"
        salt = secrets.token_hex(16)
        try:
            with self._tx():
                self.db.execute(
                    "INSERT INTO users VALUES (?,?,?,?)",
                    (username, salt, self._hash(password, salt), role))
        except sqlite3.IntegrityError:
            return False, "Username already taken"
        return True, "Account created"

    def check_login(self, username, password):
        """Return the user's role if the credentials are right, else None."""
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        row = self.db.execute(
            "SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row is None:
            return None
        if hmac.compare_digest(self._hash(password, row["salt"]),
                               row["password_hash"]):
            return row["role"]
        return None

    # --------------------------------------------------------- auctions
    def create_auction(self, seller, item, start_price, minutes,
                       min_increment=1.0, now=None):
        now = time.time() if now is None else now
        item = (item or "").strip() if isinstance(item, str) else ""
        if not item or len(item) > 80:
            return None, "Item name is required (max 80 characters)"
        try:
            start_price = float(start_price)
            minutes = float(minutes)
            min_increment = float(min_increment)
        except (TypeError, ValueError):
            return None, "Price, minutes and increment must be numbers"
        nums = (start_price, minutes, min_increment)
        if not all(math.isfinite(x) and x > 0 for x in nums):
            return None, "Price, minutes and increment must be positive"
        with self._tx():
            cur = self.db.execute(
                "INSERT INTO auctions(seller,item,start_price,min_increment,"
                "closes_at) VALUES (?,?,?,?,?)",
                (seller, item, round(start_price, 2),
                 round(min_increment, 2), now + minutes * 60))
        return cur.lastrowid, "Item listed"

    def _top_bid(self, auction_id):
        """Highest accepted bid. TIE-BREAK RULE: if two accepted bids ever
        had the same amount, the lower bids.id (processed first) wins."""
        return self.db.execute(
            "SELECT bidder, amount FROM bids WHERE auction_id=? AND accepted=1 "
            "ORDER BY amount DESC, id ASC LIMIT 1", (auction_id,)).fetchone()

    def view(self, auction_id):
        """Auction as a plain dict, including current high bid."""
        a = self.db.execute(
            "SELECT * FROM auctions WHERE id=?", (auction_id,)).fetchone()
        if a is None:
            return None
        top = self._top_bid(auction_id)
        d = dict(a)
        d["high_bid"] = top["amount"] if top else None
        d["high_bidder"] = top["bidder"] if top else None
        d["minimum_bid"] = (round(top["amount"] + a["min_increment"], 2)
                            if top else a["start_price"])
        return d

    def list_open(self, now=None):
        now = time.time() if now is None else now
        ids = [r["id"] for r in self.db.execute(
            "SELECT id FROM auctions WHERE status='open' AND closes_at > ? "
            "ORDER BY closes_at", (now,))]
        return [self.view(i) for i in ids]

    # ------------------------------------------------------------- bids
    def place_bid(self, auction_id, bidder, amount, now=None):
        """Validate and record one bid atomically. Returns (ok, message).
        Every attempt, accepted or rejected, is written to the bid history."""
        now = time.time() if now is None else now      # SERVER clock decides
        try:
            amount = round(float(amount), 2)
        except (TypeError, ValueError):
            return False, "Bid must be a number"
        if not math.isfinite(amount) or amount <= 0:
            return False, "Bid must be a positive number"

        with self._tx():
            a = self.db.execute(
                "SELECT * FROM auctions WHERE id=?", (auction_id,)).fetchone()
            if a is None:
                return False, "No such auction"
            reason = None
            if a["status"] != "open" or now >= a["closes_at"]:
                reason = "Auction is closed"
            elif a["seller"] == bidder:
                reason = "Sellers cannot bid on their own item"
            else:
                top = self._top_bid(auction_id)
                minimum = (round(top["amount"] + a["min_increment"], 2)
                           if top else a["start_price"])
                if amount < minimum:
                    reason = f"Bid too low. Minimum is {minimum:.2f}"
            self.db.execute(
                "INSERT INTO bids(auction_id,bidder,amount,placed_at,accepted,note)"
                " VALUES (?,?,?,?,?,?)",
                (auction_id, bidder, amount, now, int(reason is None), reason))
        if reason:
            return False, reason
        return True, "Bid accepted"

    def close_expired(self, now=None):
        """Close every auction whose deadline passed; record the winner.
        Returns the list of auctions closed by this call."""
        now = time.time() if now is None else now
        closed = []
        with self._tx():
            rows = self.db.execute(
                "SELECT id FROM auctions WHERE status='open' AND closes_at <= ?",
                (now,)).fetchall()
            for r in rows:
                top = self._top_bid(r["id"])
                self.db.execute(
                    "UPDATE auctions SET status='closed', winner=?, "
                    "final_price=? WHERE id=?",
                    (top["bidder"] if top else None,
                     top["amount"] if top else None, r["id"]))
                closed.append(r["id"])
        return [self.view(i) for i in closed]

    # ------------------------------------------------------------ admin
    def transaction_log(self, limit=30):
        rows = self.db.execute(
            "SELECT b.id, b.auction_id, a.item, b.bidder, b.amount, "
            "b.placed_at, b.accepted, b.note FROM bids b "
            "JOIN auctions a ON a.id = b.auction_id "
            "ORDER BY b.id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def closed_auctions(self, limit=20):
        rows = self.db.execute(
            "SELECT id, item, seller, winner, final_price FROM auctions "
            "WHERE status='closed' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
