"""
client.py - one client for all roles (bidder, seller, admin).
The role returned by the server at login decides which menu you get.

Run:  python client.py <backplane_ip> <username>

Threading note: ZeroMQ sockets must stay in one thread. The keyboard thread
therefore never publishes directly. It puts messages in a queue, and the main
(Banyan) thread sends them from its idle callback.
"""
import getpass
import queue
import sys
import threading
import time

from python_banyan.banyan_base import BanyanBase


class AuctionClient(BanyanBase):
    def __init__(self, ip, username):
        super().__init__(back_plane_ip_address=ip,
                         process_name=f"client-{username}")
        self.username = username
        self.token = None
        self.role = None
        self.clock_offset = 0.0           # server time minus local time
        self.outbox = queue.Queue()       # keyboard thread -> main thread
        self.reply_event = threading.Event()
        self.last_reply = None
        self.quit_flag = False

        self.set_subscriber_topic(f"reply.{username}.")   # private answers
        self.set_subscriber_topic("auction_update")       # public bid events
        self.set_subscriber_topic("auction_closed")       # public results

        self.receive_loop_idle_addition = self.flush_outbox
        threading.Thread(target=self.ui, daemon=True).start()
        self.receive_loop()

    # ------------------------------------------------ main thread (Banyan)
    def flush_outbox(self):
        while True:
            try:
                topic, payload = self.outbox.get_nowait()
            except queue.Empty:
                break
            self.publish_payload(payload, topic)
        if self.quit_flag:
            self.clean_up()
            sys.exit(0)

    def incoming_message_processing(self, topic, payload):
        if "server_time" in payload:
            self.clock_offset = payload["server_time"] - time.time()
        if topic.startswith("reply."):
            self.last_reply = payload
            self.reply_event.set()
        elif not self.token:
            return                                    # ignore broadcasts until login
        elif topic == "auction_update":
            a = payload["auction"]
            if payload["event"] == "new":
                print(f"\n[NEW ITEM] #{a['id']} {a['item']} - starts at "
                      f"{a['start_price']:.2f}, closes in {self.remaining(a)}")
            else:
                print(f"\n[NEW BID] #{a['id']} {a['item']}: "
                      f"{a['high_bid']:.2f} by {a['high_bidder']} "
                      f"({self.remaining(a)} left)")
        elif topic == "auction_closed":
            a = payload["auction"]
            if a["winner"]:
                print(f"\n[CLOSED] #{a['id']} {a['item']} sold to "
                      f"{a['winner']} for {a['final_price']:.2f}")
            else:
                print(f"\n[CLOSED] #{a['id']} {a['item']} ended with no bids")

    def remaining(self, a):
        """Countdown for DISPLAY only. The server decides whether a bid is on
        time. Uses the server's clock (offset) so wrong PC clocks don't matter."""
        secs = int(a["closes_at"] - (time.time() + self.clock_offset))
        if secs <= 0:
            return "ended"
        return f"{secs // 60:02d}:{secs % 60:02d}"

    # --------------------------------------------- keyboard thread (menus)
    def request(self, topic, **data):
        """Send a request and wait up to 5 s for the server's private reply."""
        self.reply_event.clear()
        self.outbox.put((topic, data))
        if not self.reply_event.wait(5):
            print("No reply from the server. Is it running?")
            return None
        return self.last_reply

    def creds(self):
        return {"username": self.username, "token": self.token}

    def quit(self):
        self.quit_flag = True

    def ui(self):
        time.sleep(1)                      # let the connections settle
        while not self.token:
            choice = input("\n1) Login  2) Register  0) Quit > ").strip()
            if choice == "0":
                return self.quit()
            if choice not in ("1", "2"):
                continue
            password = getpass.getpass("Password: ")
            if choice == "2":
                role = input("Role (bidder/seller): ").strip().lower()
                r = self.request("register_request", username=self.username,
                                 password=password, role=role)
            else:
                r = self.request("login_request", username=self.username,
                                 password=password)
                if r and r["ok"]:
                    self.token, self.role = r["token"], r["role"]
            if r:
                print(r["msg"])

        menus = {
            "bidder": [("List open auctions", self.do_list),
                       ("Place a bid", self.do_bid)],
            "seller": [("List open auctions", self.do_list),
                       ("List a new item", self.do_list_item)],
            "admin": [("List open auctions", self.do_list),
                      ("Show transaction log", self.do_log)],
        }[self.role]
        while True:
            print(f"\n--- {self.role} menu ---")
            for i, (label, _) in enumerate(menus, 1):
                print(f"{i}) {label}")
            print("0) Quit")
            choice = input("> ").strip()
            if choice == "0":
                return self.quit()
            if choice.isdigit() and 1 <= int(choice) <= len(menus):
                menus[int(choice) - 1][1]()

    def do_list(self):
        r = self.request("list_request", **self.creds())
        if not r:
            return
        if not r["auctions"]:
            return print("No open auctions.")
        print(f"\n{'ID':<4}{'Item':<24}{'Seller':<12}{'High bid':<12}"
              f"{'Min next':<10}Left")
        for a in r["auctions"]:
            high = f"{a['high_bid']:.2f}" if a["high_bid"] else "-"
            print(f"{a['id']:<4}{a['item'][:22]:<24}{a['seller']:<12}"
                  f"{high:<12}{a['minimum_bid']:<10.2f}{self.remaining(a)}")

    def do_bid(self):
        try:
            auction_id = int(input("Auction ID: "))
            amount = float(input("Your bid: "))
        except ValueError:
            return print("Please enter numbers.")
        r = self.request("bid", auction_id=auction_id, amount=amount,
                         **self.creds())
        if r:
            print(("ACCEPTED: " if r["ok"] else "REJECTED: ") + r["msg"])

    def do_list_item(self):
        item = input("Item name: ").strip()
        try:
            price = float(input("Starting price: "))
            minutes = float(input("Duration (minutes): "))
            inc = input("Minimum increment [1]: ").strip()
            inc = float(inc) if inc else 1.0
        except ValueError:
            return print("Please enter numbers.")
        r = self.request("list_item", item=item, start_price=price,
                         minutes=minutes, min_increment=inc, **self.creds())
        if r:
            print(r["msg"])

    def do_log(self):
        r = self.request("admin_query", **self.creds())
        if not r:
            return
        if not r["ok"]:
            return print(r["msg"])
        print("\nRecent bids (newest first):")
        for b in r["log"]:
            t = time.strftime("%H:%M:%S", time.localtime(b["placed_at"]))
            status = "OK" if b["accepted"] else f"REJECTED ({b['note']})"
            print(f"  {t} #{b['auction_id']} {b['item'][:18]:<18} "
                  f"{b['bidder']:<10} {b['amount']:>10.2f}  {status}")
        print("\nClosed auctions:")
        for c in r["closed"]:
            res = (f"{c['winner']} at {c['final_price']:.2f}"
                   if c["winner"] else "no bids")
            print(f"  #{c['id']} {c['item'][:22]:<22} seller {c['seller']:<10} -> {res}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: python client.py <backplane_ip> <username>")
    AuctionClient(sys.argv[1], sys.argv[2])
