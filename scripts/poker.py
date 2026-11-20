"""Heads-up Texas Hold'em: hand evaluation, equity, and a simple betting read.

Inputs (codes like "AS", "10H", "7C"):
  hands  {"player_1": [2 hole cards], "player_2": [2 hole cards]}  (missing cards are dealt at random)
  board  [0, 3, 4 or 5 community cards]
  bets   {"player_1": chips in, "player_2": chips in}  (from the overhead chip count)

Equity is the broadcast view: both hands known, the rest of the board run out
from the 52-card deck (exactly when 0-2 board cards are missing after the flop,
by sampling otherwise). The betting read is a rule of thumb, not solver output:
facing a bet, call when equity beats the pot odds; raise/bet when well ahead.
"""

from __future__ import annotations

import itertools
import random
from collections import Counter
from functools import lru_cache

RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
SUITS = "SHDC"
VALUE = {r: i + 2 for i, r in enumerate(RANKS)}
DECK = [f"{r}{s}" for r in RANKS for s in SUITS]
NAMES = ["High card", "Pair", "Two pair", "Three of a kind", "Straight",
         "Flush", "Full house", "Four of a kind", "Straight flush"]
SAMPLES = 6000


def parse(code: str) -> tuple[int, str]:
    return VALUE[code[:-1]], code[-1]


def _straight_high(ranks: set[int]) -> int:
    """Highest card of the best 5-card run in `ranks` (ace can be low), else 0."""
    rs = ranks | ({1} if 14 in ranks else set())
    for top in range(14, 4, -1):
        if all(r in rs for r in range(top - 4, top + 1)):
            return top
    return 0


def best_hand(cards: list[str]) -> tuple:
    """Rank of the best 5-card hand in 5-7 cards; bigger tuple = better hand."""
    parsed = [parse(c) for c in cards]
    by_suit: dict[str, list[int]] = {}
    for v, s in parsed:
        by_suit.setdefault(s, []).append(v)
    flush = next((sorted(vs, reverse=True) for vs in by_suit.values() if len(vs) >= 5), None)
    if flush:
        sf = _straight_high(set(flush))
        if sf:
            return (8, sf)
    counts = Counter(v for v, _ in parsed)
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)  # by count, then rank
    ranks_desc = sorted(counts, reverse=True)
    if groups[0][1] == 4:
        quad = groups[0][0]
        return (7, quad, max(r for r in ranks_desc if r != quad))
    trips = [r for r, n in groups if n == 3]
    pairs = [r for r, n in groups if n == 2]
    if trips and (len(trips) > 1 or pairs):
        t = trips[0]
        p = max([x for x in trips[1:]] + pairs)
        return (6, t, p)
    if flush:
        return (5, *flush[:5])
    st = _straight_high(set(counts))
    if st:
        return (4, st)
    if trips:
        t = trips[0]
        return (3, t, *[r for r in ranks_desc if r != t][:2])
    if len(pairs) >= 2:
        hi, lo = pairs[0], pairs[1]
        return (2, hi, lo, max(r for r in ranks_desc if r not in (hi, lo)))
    if pairs:
        p = pairs[0]
        return (1, p, *[r for r in ranks_desc if r != p][:3])
    return (0, *ranks_desc[:5])


@lru_cache(maxsize=256)
def _equity(h1: tuple, h2: tuple, board: tuple) -> tuple[float, float, float, int]:
    """(P1 win, tie, P2 win, runouts) with random fill for any missing cards."""
    known = set(h1) | set(h2) | set(board)
    rest = [c for c in DECK if c not in known]
    need1, need2, needb = 2 - len(h1), 2 - len(h2), 5 - len(board)
    need = need1 + need2 + needb
    if need <= 2:
        runs = itertools.combinations(rest, need)
        total = None
    else:
        rng = random.Random(hash((h1, h2, board)))   # stable numbers between refreshes
        runs = (rng.sample(rest, need) for _ in range(SAMPLES))
        total = SAMPLES
    w1 = w2 = tie = n = 0
    for run in runs:
        run = list(run)
        a = list(h1) + run[:need1]
        b = list(h2) + run[need1:need1 + need2]
        brd = list(board) + run[need1 + need2:]
        r1, r2 = best_hand(a + brd), best_hand(b + brd)
        if r1 > r2:
            w1 += 1
        elif r2 > r1:
            w2 += 1
        else:
            tie += 1
        n += 1
    n = n or 1
    return w1 / n, tie / n, w2 / n, total or n


def read(equity: float, tie: float, mine: int, theirs: int) -> dict:
    """Rule-of-thumb action from equity vs. the price of calling."""
    share = equity + tie / 2
    to_call = max(0, theirs - mine)
    pot = mine + theirs
    if to_call:
        need = to_call / (pot + to_call)
        action = "Raise" if share > 0.65 else "Call" if share >= need else "Fold"
        return {"to_call": to_call, "pot_odds": need, "action": action}
    action = "Bet" if share > 0.6 else "Check"
    return {"to_call": 0, "pot_odds": 0.0, "action": action}


def stage(board: list[str]) -> str:
    return {0: "Preflop", 3: "Flop", 4: "Turn", 5: "River"}.get(len(board), f"{len(board)} board cards")


def summary(hands: dict[str, list[str]], board: list[str], bets: dict[str, int] | None,
            feeds: dict[str, str]) -> dict:
    """Same outer shape as game.Table.summary, plus board/stage, for the control panel."""
    bets = bets or {}
    clean = lambda cs: [c for c in cs if c[:-1] in VALUE and c[-1] in SUITS]
    h = {p: tuple(clean(hands.get(p, []))[:2]) for p in ("player_1", "player_2")}
    b = tuple(clean(board)[:5])
    dupes = [c for c, k in Counter(list(h["player_1"]) + list(h["player_2"]) + list(b)).items() if k > 1]
    out = {"mode": "holdem", "stage": stage(list(b)), "board": list(b), "deck_left": None,
           "players": {}, "error": f"card seen twice: {', '.join(dupes)}" if dupes else None}
    if dupes:
        return out
    w1, tie, w2, runs = _equity(h["player_1"], h["player_2"], b)
    for p, win, other in (("player_1", w1, "player_2"), ("player_2", w2, "player_1")):
        made = NAMES[best_hand(list(h[p]) + list(b))[0]] if len(h[p]) + len(b) >= 5 else None
        out["players"][p] = {
            "feed": feeds.get(p, ""),
            "cards": [{"card": c} for c in h[p]],
            "hand": {"name": made, "win": win, "tie": tie, "lose": 1 - win - tie, "runouts": runs,
                     **read(win, tie, int(bets.get(p, 0)), int(bets.get(other, 0)))},
        }
    return out
