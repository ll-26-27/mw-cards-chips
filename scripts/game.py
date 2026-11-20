"""Higher-card betting game: card tracking, odds and best bet.

Rules assumed:
  * One 54-card deck: 52 cards + 2 jokers. Ace high; jokers beat everything.
    Equal ranks tie (push).
  * Each player bets 0, 1 or 2 chips that their card is higher than the
    other's. Win +bet, lose -bet, tie 0.
  * Cards are not reshuffled: every card seen since the last "new deck" is
    out of play.

Each player's odds use only what that player knows: their own card and the
cards already out of the deck — not the opponent's current card.
"""

from __future__ import annotations

import threading
import time
from collections import Counter

RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
SUITS = ["S", "H", "D", "C"]
JOKER = "JK"
VALUE = {r: i + 2 for i, r in enumerate(RANKS)} | {JOKER: 15}
SUIT_SYMBOL = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}


def full_deck() -> Counter:
    deck = Counter(f"{r}{s}" for r in RANKS for s in SUITS)
    deck[JOKER] = 2
    return deck


def normalize(card: dict) -> str | None:
    """Turn a detector card ({code, rank, suit}) into a deck code like 'QH' or 'JK'."""
    code = str(card.get("code", "")).upper().strip()
    rank = str(card.get("rank", "")).upper().strip()
    suit = str(card.get("suit", "")).lower().strip()
    if "JOKER" in code or "JOKER" in rank or code in ("JK", "X", "XX"):
        return JOKER
    if rank == "1":
        rank = "A"
    s = {"spades": "S", "hearts": "H", "diamonds": "D", "clubs": "C"}.get(suit)
    if rank in VALUE and s:
        return f"{rank}{s}"
    # fall back to parsing the code: rank + suit letter
    if len(code) >= 2 and code[:-1] in VALUE and code[-1] in SUITS:
        return code
    return None


def pretty(code: str | None) -> str:
    if not code:
        return "—"
    if code == JOKER:
        return "Joker"
    return f"{code[:-1]}{SUIT_SYMBOL[code[-1]]}"


def odds(own: str | None, unknown: Counter) -> dict:
    """P(win/tie/lose) for `own` against one card drawn from `unknown`."""
    n = sum(unknown.values())
    if n == 0:
        return {"win": 0.0, "tie": 0.0, "lose": 0.0, "n": 0}
    if own is None:
        # No card yet: by symmetry nobody has an edge.
        return {"win": None, "tie": None, "lose": None, "n": n}
    v = VALUE[own if own == JOKER else own[:-1]]
    win = tie = 0
    for code, k in unknown.items():
        w = VALUE[code if code == JOKER else code[:-1]]
        if w < v:
            win += k
        elif w == v:
            tie += k
    return {"win": win / n, "tie": tie / n, "lose": (n - win - tie) / n, "n": n}


def best_bet(o: dict) -> dict:
    """Expected chips per chip bet = P(win) - P(lose). Linear payoff → bet max or nothing."""
    if o.get("win") is None:
        return {"bet": 0, "ev_per_chip": 0.0}
    edge = o["win"] - o["lose"]
    bet = 2 if edge > 0 else 0
    return {"bet": bet, "ev_per_chip": edge, "ev": edge * bet}


class Table:
    """Tracks the current card per player and every card seen this deck."""

    def __init__(self, feeds: dict[str, str]):
        self.feeds = feeds            # player -> camera feed name
        self.lock = threading.Lock()
        self.current: dict[str, list[str]] = {p: [] for p in feeds}
        self.seen: Counter = Counter()
        self.status = "starting"
        self.updated = 0.0

    def new_deck(self) -> None:
        with self.lock:
            self.seen = Counter()
            for cards in self.current.values():
                for c in cards:
                    self.seen[c] += 1

    def update(self, snapshot: dict) -> None:
        by_feed = {f.get("feed"): f.get("cards", []) for f in snapshot.get("feeds", [])}
        with self.lock:
            visible: Counter = Counter()
            for player, feed in self.feeds.items():
                # Every card in front of this camera, left to right as the detector sends them.
                codes = [code for code in (normalize(c) for c in by_feed.get(feed, [])) if code]
                self.current[player] = codes
                for code in codes:
                    visible[code] += 1
            # A card counts as out of the deck once it has been seen (jokers: up to 2).
            for code, k in visible.items():
                self.seen[code] = min(max(self.seen[code], k), 2 if code == JOKER else 1)
            self.status = "ok"
            self.updated = time.time()

    def cards_now(self) -> dict[str, list[str]]:
        with self.lock:
            return {p: list(c) for p, c in self.current.items()}

    def summary(self, cards: dict[str, list[str]] | None = None, used: Counter | None = None) -> dict:
        """Odds for the live cards, or for `cards` (e.g. the operator-confirmed ones).

        `used` is the pile of cards out of the deck (from rounds the operator
        closed). Without it, every card the cameras have seen counts as used.
        """
        with self.lock:
            current = self.current if cards is None else cards
            deck = full_deck()
            out = Counter(self.seen if used is None else used)
            for mine in current.values():          # cards on the table are out of the deck too
                for c in mine:
                    out[c] = max(out[c], Counter(sum(current.values(), []))[c])
            remaining = deck - out
            players = {}
            for p, mine in current.items():
                # What this player can't see: the remaining deck plus the opponent's
                # current cards (face-down to them but already counted as seen).
                unknown = Counter(remaining)
                for q, theirs in current.items():
                    if q != p:
                        for other in theirs:
                            unknown[other] += 1
                cards = []
                for own in mine:
                    o = odds(own, unknown)
                    cards.append({"card": own, "card_label": pretty(own), "odds": o, **best_bet(o)})
                players[p] = {"feed": self.feeds[p], "cards": cards}
            return {"status": self.status, "updated": self.updated,
                    "deck_left": sum(remaining.values()),
                    "used": sorted(pretty(c) for c in out.elements()),
                    "players": players}


def parse_code(text: str) -> str | None:
    """Operator-typed card → deck code: '7s', '10H', 'qh', 'T♠', 'joker' → '7S', '10H', 'QH', '10S', 'JK'."""
    t = text.strip().upper().replace("♠", "S").replace("♥", "H").replace("♦", "D").replace("♣", "C")
    if t in ("JK", "JOKER", "X"):
        return JOKER
    if len(t) < 2:
        return None
    rank, suit = t[:-1], t[-1]
    rank = {"T": "10", "1": "A"}.get(rank, rank)
    return f"{rank}{suit}" if rank in VALUE and rank != JOKER and suit in SUITS else None
