"""
server.py - the authoritative auction server (a Banyan component).

Only this program touches the database. Clients send requests as Banyan
messages; the server validates them and answers on a private reply topic
("reply.<username>.") or broadcasts public events.

Run:  python server.py [backplane_ip]
"""
import secrets
import sys
import time

from python_banyan.banyan_base import BanyanBase
from auction_db import AuctionDB, USERNAME_RE


class AuctionServer(BanyanBase):
    def __init__(self, ip=None, db_path="auction.db"):
        super().__init__(back_plane_ip_address=ip, process_name="AuctionServer")
        self.store = AuctionDB(db_path)
        self.sessions = {}            # token -> (username, role)
        self._last_check = 0.0

        self.handlers = {
            "register_request": self.on_register,
            "login_request": self.on_login,
            "list_request": self.on_list,
            "list_item": self.on_list_item,
            "bid": self.on_bid,
            "admin_query": self.on_admin_query,
        }
        for topic in self.handlers:
            self.set_subscriber_topic(topic)

        # Called by Banyan's receive loop whenever no message is waiting.
        # Used for the auction countdown (closing expired auctions).
        self.receive_loop_idle_addition = self.idle

        print("Auction server running. Waiting for clients...")
        self.receive_loop()

    # ------------------------------------------------------------ helpers
    def incoming_message_processing(self, topic, payload):
        handler = self.handlers.get(topic)
        if handler is None or not isinstance(payload, dict):
            return
        try:
            handler(payload)
        except Exception as exc:          # never let one bad message kill the server
            print(f"Error handling '{topic}': {exc!r}")

    def reply(self, username, kind, ok, msg, **extra):
        """Private answer to one user (topic ends with '.' so that
        'reply.alice.' can never prefix-match 'reply.alice2.')."""
        if not isinstance(username, str) or not USERNAME_RE.match(username):
            return
        body = {"type": kind, "ok": ok, "msg": msg, "server_time": time.time()}
        body.update(extra)
        self.publish_payload(body, f"reply.{username}.")

    def auth(self, p, kind, roles):
        """Check session token and role. Returns username or None."""
        user = p.get("username")
        session = self.sessions.get(p.get("token"))
        if session is None or session[0] != user:
            self.reply(user, kind, False, "Please log in first")
            return None
        if session[1] not in roles:
            self.reply(user, kind, False,
                       "Only " + "/".join(roles) + " accounts can do this")
            return None
        return user

    # ----------------------------------------------------------- handlers
    def on_register(self, p):
        role = p.get("role")
        if role not in ("bidder", "seller"):      # admins are created by script
            return self.reply(p.get("username"), "register", False,
                              "Role must be bidder or seller")
        ok, msg = self.store.add_user(p.get("username"), p.get("password"), role)
        self.reply(p.get("username"), "register", ok, msg)

    def on_login(self, p):
        username = p.get("username")
        role = self.store.check_login(username, p.get("password"))
        if role is None:
            return self.reply(username, "login", False,
                              "Wrong username or password")
        token = secrets.token_hex(16)
        self.sessions[token] = (username, role)
        print(f"{username} logged in as {role}")
        self.reply(username, "login", True, f"Welcome, {username}!",
                   role=role, token=token)

    def on_list(self, p):
        user = self.auth(p, "auctions", ("admin", "seller", "bidder"))
        if user:
            self.reply(user, "auctions", True, "",
                       auctions=self.store.list_open())

    def on_list_item(self, p):
        user = self.auth(p, "list_item", ("seller",))
        if not user:
            return
        auction_id, msg = self.store.create_auction(
            user, p.get("item"), p.get("start_price"), p.get("minutes"),
            p.get("min_increment", 1.0))
        self.reply(user, "list_item", auction_id is not None, msg)
        if auction_id is not None:
            print(f"{user} listed auction #{auction_id}")
            self.publish_payload(
                {"event": "new", "auction": self.store.view(auction_id),
                 "server_time": time.time()}, "auction_update")

    def on_bid(self, p):
        user = self.auth(p, "bid", ("bidder",))
        if not user:
            return
        auction_id = p.get("auction_id")
        ok, msg = self.store.place_bid(auction_id, user, p.get("amount"))
        self.reply(user, "bid", ok, msg)
        print(f"bid by {user} on #{auction_id}: {'OK' if ok else msg}")
        if ok:
            self.publish_payload(
                {"event": "bid", "auction": self.store.view(auction_id),
                 "server_time": time.time()}, "auction_update")

    def on_admin_query(self, p):
        user = self.auth(p, "log", ("admin",))
        if user:
            self.reply(user, "log", True, "",
                       log=self.store.transaction_log(),
                       closed=self.store.closed_auctions())

    # -------------------------------------------------- countdown / closing
    def idle(self):
        """Runs between messages. Closes expired auctions once per second."""
        now = time.time()
        if now - self._last_check < 1.0:
            return
        self._last_check = now
        for auction in self.store.close_expired(now):
            print(f"Auction #{auction['id']} closed. Winner: "
                  f"{auction['winner']} at {auction['final_price']}")
            self.publish_payload(
                {"event": "closed", "auction": auction, "server_time": now},
                "auction_closed")


if __name__ == "__main__":
    AuctionServer(ip=sys.argv[1] if len(sys.argv) > 1 else None)
