# mw-cards-chips

Overhead chip counter for the higher-card betting game.

Takes an overhead photo of the table, finds the red (player 1) and green
(player 2) chips, and reports how many each player has in. Runs fully local
— OpenCV + NumPy, no API calls.

## Pipeline

1. Auto-crop to the bright wood tabletop (Otsu threshold → biggest bright blob).
2. HSV color masks for each configured chip color; morphology to clean speckle
   and fill each chip's inner white badge.
3. Connected components → per-color count, gated by area and aspect ratio.

## Run

```sh
pip3 install opencv-python-headless numpy
python3 scripts/chip_count.py samples
```

Prints a JSON report on stdout and a `file: P1=red P2=green` summary on stderr.
Debug crops and annotated detections land in `samples/out/`.

### Configure a different pair of colors

Colors are HSV hue targets; override per-player on the command line:

```sh
python3 scripts/chip_count.py samples \
  --color player_1=red:0:12 \
  --color player_2=green:52:22
```

Format: `player=name:hue:tolerance[:min_s[:min_v]]`. Hue is in OpenCV scale
(0–180). Tolerance is max hue distance, wrapping around 180.

## Samples

`samples/` holds five overhead shots with ground-truth counts:

| file         | red | green |
| ------------ | --- | ----- |
| IMG_1054.jpg | 1   | 0     |
| IMG_1055.jpg | 1   | 2     |
| IMG_1056.jpg | 2   | 2     |
| IMG_1057.jpg | 0   | 0     |
| IMG_1058.jpg | 0   | 1     |

## Live control panel

`scripts/overhead_app.py` runs the whole game on one machine (the one with OBS):

```sh
python3 -m venv .venv && .venv/bin/pip install opencv-python-headless numpy obsws-python
.venv/bin/python scripts/overhead_app.py --source obs --obs-source a.04
open http://localhost:8765
```

- **Chips:** grabs the overhead (`a.04` in OBS, via the OBS WebSocket on port 4455;
  the password is read from OBS's own config) and counts red and green chips.
- **Cards:** polls the `mw-cards-cv` camera computer (`--cards http://10.250.155.29:3000`)
  for `/api/cards`, and shows its camera frames from `/api/frame?feed=…`.
  Player 1 (red) is `camera-2`, Player 2 (green) is `camera-1` (`--p1-feed`, `--p2-feed`).
- **Check tab:** the live overhead and cameras with what was detected. Correct the
  numbers or cards if needed, then **Yes, correct** holds them; **Check again** goes back
  to live. Displays only ever show held values.
- **Rounds:** **Start game** gives a fresh 54-card deck; **Next round** moves the held
  cards to the used pile. Game state is kept in `.game-state.json` across restarts.
- **Info tab / `GET /state.json`:** held bets, cards, P(win) and best bet per card.

### Odds (`scripts/game.py`)

54 cards (52 + 2 jokers), ace high, jokers top, equal ranks tie. Each player's
P(win) uses only their own card against everything not yet used (the opponent's
card counts as unknown to them). Payout assumed to be ±bet, so the best bet is 2
when P(win) > P(lose) and 0 otherwise.

### Heads-up Texas Hold'em (`--game holdem`, `scripts/poker.py`)

```sh
.venv/bin/python scripts/overhead_app.py --game holdem
```

- **Inputs:** each player's two hole cards from their camera, the board typed in the
  panel's **Board** box (or a `board` feed from mw-cards-cv, `--board-feed`), and the
  overhead chip count as what each player has put in this hand. All can be corrected
  before **Yes, correct**. **Next hand** starts the next deal (no deck tracking across hands).
- **Outputs:** made hand, equity with both hands known (exact from the turn, 6,000
  sampled run-outs before), and a rule-of-thumb suggestion from equity vs. pot odds
  (Bet/Check, or Call/Raise/Fold facing a bet). Not solver strategy.
- Each mode saves its own game (`.game-state.json`, `.game-state-holdem.json`).
- `?tab=info` / `?tab=check` opens the panel on that tab.

### OBS overlay (`overlay/`)

A copy of the `/cards` page from `graphic-tools-for-blue` (`src/app/cards/page.tsx`,
`src/app/api/cards-layout/route.ts`): both players' held cards, P(win) and bet (in hold'em: the board in the middle and each player's hand, equity and suggestion),
Player 2 on the left and Player 1 on the right, draggable (positions shared through
`/api/cards-layout`). Run graphic-tools-for-blue with `pnpm dev --port 3200` and add
`http://localhost:3200/cards` as a 1920×1080 (or 4K) Browser source above `a.03`.

## Overhead feed

Expected to be a Blackmagic Constellation 8K Program output (future). For now
the counter runs on still frames dropped into `samples/`. Wiring the switcher's
output into this counter — either via a capture device or the switcher's
built-in streaming encoder — is tracked separately.
