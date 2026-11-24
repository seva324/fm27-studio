"""Print the build's rating for a player, for checking against the original."""
import sys

sys.path.insert(0, ".")

from fmrte import ratings
from fmrte.db import Snapshot

uid = int(sys.argv[1]) if len(sys.argv) > 1 else 92074020
snap = Snapshot("data/fm_snapshot.db")
player = snap.player(uid)
print("name    ", player.get("name"))
print("role    ", ratings.role_for(player))
print("rating  ", ratings.role_rating(player))
print("pref    ", ratings.preferable_rating(player))
print("weighted", ratings.weighted_rating(player))
