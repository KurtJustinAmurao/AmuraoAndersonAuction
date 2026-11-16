"""
create_admin.py - create the administrator account (run once, on the server PC).
Admins cannot self-register over the network; this is deliberate.

Usage:  python create_admin.py <username>
"""
import getpass
import sys

from auction_db import AuctionDB

if len(sys.argv) != 2:
    sys.exit("Usage: python create_admin.py <username>")

ok, msg = AuctionDB().add_user(sys.argv[1], getpass.getpass("Admin password: "),
                               "admin")
print(msg)
