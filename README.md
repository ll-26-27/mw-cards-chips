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
  --color player_2=green:60:18
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

## Overhead feed

Expected to be a Blackmagic Constellation 8K Program output (future). For now
the counter runs on still frames dropped into `samples/`. Wiring the switcher's
output into this counter — either via a capture device or the switcher's
built-in streaming encoder — is tracked separately.
