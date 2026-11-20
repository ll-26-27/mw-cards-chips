#!/usr/bin/env python3
"""Live overhead chip counter web app.

Grabs frames from the overhead feed, runs the chip_count.py pipeline on each,
and serves a page showing the raw overhead, the annotated table crop, and the
per-player chip counts.

Frame sources (--source):
  obs              OBS websocket screenshot of a named source (default "a.04",
                   the decklink.a.04 overhead input on this machine). Needs
                   OBS → Tools → WebSocket Server Settings → Enable.
                   Password via --obs-password or OBS_WS_PASSWORD env var.
  camera:N         AVFoundation/OpenCV camera index N (e.g. OBS Virtual Camera
                   with a.04 in program).
  path/to/file     A video file (loops) or image, or a directory of images
                   (cycles) — handy for testing with samples/.

Run:
  .venv/bin/python scripts/overhead_app.py --source obs --obs-source a.04
  open http://localhost:8765
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
import urllib.parse
from collections import Counter
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chip_count import DEFAULT_COLORS, ColorSpec, analyze, parse_color_arg  # noqa: E402
from game import Table, parse_code  # noqa: E402


# ------------- frame sources -------------------------------------------------

class ObsSource:
    def __init__(self, host: str, port: int, password: str | None, name: str, width: int):
        import obsws_python as obs
        self.client = obs.ReqClient(host=host, port=port, password=password or "", timeout=5)
        self.name = name
        self.width = width
        self.label = f"OBS source '{name}'"

    def read(self) -> np.ndarray | None:
        resp = self.client.get_source_screenshot(self.name, "jpg", self.width, None, 90)
        data = resp.image_data.split(",", 1)[1]
        buf = np.frombuffer(base64.b64decode(data), np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)


class CaptureSource:
    def __init__(self, target: int | str, loop: bool):
        self.target = target
        self.loop = loop
        self.cap = cv2.VideoCapture(target)
        if not self.cap.isOpened():
            raise RuntimeError(f"could not open {target!r}")
        self.label = f"camera {target}" if isinstance(target, int) else Path(target).name

    def read(self) -> np.ndarray | None:
        ok, frame = self.cap.read()
        if not ok and self.loop:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        return frame if ok else None


class ImageDirSource:
    def __init__(self, path: Path, hold: float):
        self.files = sorted(p for p in (path.iterdir() if path.is_dir() else [path])
                            if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
        if not self.files:
            raise RuntimeError(f"no images in {path}")
        self.hold = hold
        self.t0 = time.time()
        self.cache: dict[Path, np.ndarray] = {}
        self.label = str(path)

    def read(self) -> np.ndarray | None:
        f = self.files[int((time.time() - self.t0) / self.hold) % len(self.files)]
        if f not in self.cache:
            self.cache[f] = cv2.imread(str(f))
        self.label = f.name
        return self.cache[f]


def local_obs_password() -> str | None:
    """Fallback: read the websocket password from this machine's OBS config."""
    cfg = Path.home() / "Library/Application Support/obs-studio/plugin_config/obs-websocket/config.json"
    try:
        d = json.loads(cfg.read_text())
        return d.get("server_password") if d.get("auth_required") else ""
    except Exception:
        return None


def make_source(args) -> object:
    s = args.source
    if s == "obs":
        pw = args.obs_password or os.environ.get("OBS_WS_PASSWORD") or local_obs_password()
        return ObsSource(args.obs_host, args.obs_port, pw, args.obs_source, args.obs_width)
    if s.startswith("camera:"):
        return CaptureSource(int(s.split(":", 1)[1]), loop=False)
    p = Path(s)
    if p.is_dir() or p.suffix.lower() in {".jpg", ".jpeg", ".png"}:
        return ImageDirSource(p, args.hold)
    return CaptureSource(str(p), loop=True)


# ------------- worker --------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.raw_jpg: bytes | None = None
        self.ann_jpg: bytes | None = None
        self.report: dict = {"status": "starting"}
        self.seq = 0


def encode(img: np.ndarray, max_w: int = 1280) -> bytes:
    h, w = img.shape[:2]
    if w > max_w:
        img = cv2.resize(img, (max_w, int(h * max_w / w)), interpolation=cv2.INTER_AREA)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()


def worker(args, colors: dict[str, ColorSpec], state: State) -> None:
    source = None
    period = 1.0 / args.fps
    while True:
        t = time.time()
        try:
            if source is None:
                source = make_source(args)
            frame = source.read()
            if frame is None:
                raise RuntimeError("no frame")
            report, _, annotated = analyze(frame, colors)
            report.update(status="ok", source=source.label,
                          ms=int((time.time() - t) * 1000), ts=time.time())
            raw, ann = encode(frame), encode(annotated)
            with state.lock:
                state.raw_jpg, state.ann_jpg, state.report = raw, ann, report
                state.seq += 1
        except Exception as e:  # keep serving; retry the source
            msg = f"{type(e).__name__}: {e}"
            with state.lock:
                state.report = {**{k: v for k, v in state.report.items()
                                   if k in ("counts", "colors")},
                                "status": "error", "error": msg, "ts": time.time()}
            print(f"[overhead] {msg}", file=sys.stderr)
            source = None
            time.sleep(2)
        time.sleep(max(0.0, period - (time.time() - t)))


# ------------- cards (from the mw-cards-cv camera computer) ------------------

def cards_worker(url: str, table: Table) -> None:
    """Poll the camera computer's card list and feed it to the game table."""
    while True:
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/api/cards", timeout=3) as r:
                table.update(json.load(r))
        except Exception as e:
            with table.lock:
                table.status = f"cards: {type(e).__name__}: {e}"
        time.sleep(0.5)


# ------------- held (operator-confirmed) values ------------------------------

class Holds:
    """What the display shows: values the operator confirmed with "Yes, correct".

    Each section (chips, cards) is either held (showing its confirmed value) or
    being checked (the operator sees live detections; the display keeps the last
    confirmed value until they press Yes again).
    """

    FILE = Path(__file__).resolve().parent.parent / ".game-state.json"

    def __init__(self):
        self.lock = threading.Lock()
        self.chips: dict | None = None            # {"player_1": n, "player_2": n}
        self.cards: dict | None = None            # {"player_1": [codes], ...}
        self.at = {"chips": 0.0, "cards": 0.0}
        self.checking = {"chips": True, "cards": True}
        # Rounds: the deck only loses the cards confirmed in rounds you close.
        self.started = False
        self.round = 0
        self.used: Counter = Counter()
        self.history: list[dict] = []
        self.load()

    # Saved to disk so restarting the app keeps the game going.
    def load(self):
        try:
            d = json.loads(self.FILE.read_text())
        except Exception:
            return
        self.chips, self.cards = d.get("chips"), d.get("cards")
        self.at = d.get("at", self.at)
        self.checking = d.get("checking", self.checking)
        self.started, self.round = d.get("started", False), d.get("round", 0)
        self.used, self.history = Counter(d.get("used", {})), d.get("history", [])

    def save(self):
        d = {"chips": self.chips, "cards": self.cards, "at": self.at, "checking": self.checking,
             "started": self.started, "round": self.round, "used": dict(self.used), "history": self.history}
        try:
            self.FILE.write_text(json.dumps(d, indent=2))
        except Exception as e:
            print(f"[overhead] could not save game state: {e}", file=sys.stderr)

    def _clear_round(self):
        self.chips = self.cards = None
        self.at = {"chips": 0.0, "cards": 0.0}
        self.checking = {"chips": True, "cards": True}

    def start(self):
        with self.lock:
            self.started, self.round = True, 1
            self.used, self.history = Counter(), []
            self._clear_round()
            self.save()

    def next_round(self):
        with self.lock:
            if self.cards:
                for codes in self.cards.values():
                    self.used.update(codes)
            self.history.append({"round": self.round, "chips": self.chips, "cards": self.cards})
            self.round += 1
            self._clear_round()
            self.save()

    def snapshot(self) -> dict:
        with self.lock:
            return {"chips": self.chips, "cards": self.cards,
                    "at": dict(self.at), "checking": dict(self.checking),
                    "started": self.started, "round": self.round,
                    "used": Counter(self.used), "history": list(self.history)}


# ------------- web -----------------------------------------------------------

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chips and Cards</title>
<style>
:root{--bg:#111;--fg:#eee;--muted:#8a8a8a;--line:#2a2a2a;--red:#e5484d;--green:#30a46c;--ok:#2f6f4f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px system-ui,sans-serif}
nav{display:flex;gap:4px;padding:12px 16px;border-bottom:1px solid var(--line);align-items:center}
nav button{background:none;border:0;color:var(--muted);font:inherit;padding:6px 12px;border-radius:6px;cursor:pointer}
nav button.on{background:#262626;color:var(--fg)}
nav .sp{flex:1}
#deck{color:var(--muted)}
button.act{font:inherit;padding:10px 18px;border-radius:8px;border:1px solid var(--line);background:#262626;color:var(--fg);cursor:pointer}
button.yes{background:var(--ok);border-color:var(--ok)}
#newdeck{border:1px solid var(--line);color:var(--fg)}
section.chk{padding:16px;border-bottom:1px solid var(--line)}
section.chk h2{font:inherit;color:var(--muted);margin:0 0 12px}
.row{display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin:12px 0}
.imgs{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:800px){.imgs{grid-template-columns:1fr}}
.imgs figure{margin:0}
.imgs img{width:100%;display:block;background:#000;border-radius:8px;min-height:100px}
.imgs figcaption{color:var(--muted);padding:6px 0}
.state{color:var(--muted)}
input.fix{font:inherit;width:120px;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:#1b1b1b;color:var(--fg);margin-left:6px}
input.fix:disabled{opacity:.6}
input.fix.edited{border-color:#c9a227}
.held{color:#7fd3a8}
table{border-collapse:collapse;width:100%;max-width:900px;margin:8px 16px}
th,td{text-align:left;padding:12px 16px 12px 0;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums;vertical-align:top}
th{color:var(--muted);font-weight:400;width:110px}
td{font-weight:400}
td small{display:block;font-size:inherit;color:var(--muted)}
.p1{color:var(--red)}.p2{color:var(--green)}
.pc{display:inline-flex;margin:0 6px 6px 0;vertical-align:top;flex-direction:column;justify-content:space-between;width:76px;height:110px;padding:6px 8px;font-size:15px;border-radius:8px;background:#fafafa;color:#111;box-shadow:0 1px 3px #0008;line-height:1}
.pc.red{color:#d42a2a}
.pc .pip{font-size:36px;text-align:center}
.pc .c2{transform:rotate(180deg)}
.pc.joker .pip{font-size:15px;letter-spacing:1px}
.pc.empty{background:none;border:1px dashed #444;box-shadow:none}
#status{color:var(--muted);padding:12px 16px}
.err{color:#ff8a8a}
</style></head><body>
<nav>
  <button data-tab="check" class="on">Check</button>
  <button data-tab="info">Info</button>
  <span class="sp"></span><span id="deck"></span>
  <button class="act" id="next" title="Confirmed cards go to the used pile; next round">Next round</button>
  <button class="act" id="start" title="Fresh 54-card deck, round 1">Start game</button>
</nav>

<div id="tab-check">
  <section class="chk">
    <h2>Chips (overhead)</h2>
    <div class="imgs"><figure><img id="raw" alt="overhead"><figcaption>Overhead</figcaption></figure>
      <figure><img id="ann" alt="chip detections"><figcaption>Detected chips</figcaption></figure></div>
    <div class="row"><span id="chips-live"></span></div>
    <div class="row">
      <label class="p2">Player 2 (green) <input class="fix" id="fix-chips-2" type="number" min="0" step="1"></label>
      <label class="p1">Player 1 (red) <input class="fix" id="fix-chips-1" type="number" min="0" step="1"></label>
    </div>
    <div class="row"><button class="act" id="chips-btn"></button><span id="chips-state" class="state"></span></div>
  </section>
  <section class="chk">
    <h2>Cards (camera computer)</h2>
    <div class="imgs">
      <figure><img id="feed2" alt=""><figcaption id="feed2c"></figcaption></figure>
      <figure><img id="feed1" alt=""><figcaption id="feed1c"></figcaption></figure>
    </div>
    <div class="row"><span id="cards-live"></span></div>
    <div class="row">
      <label class="p2">Player 2 <input class="fix" id="fix-cards-2" placeholder="e.g. 7S"></label>
      <label class="p1">Player 1 <input class="fix" id="fix-cards-1" placeholder="e.g. QH 10D"></label>
      <span class="state">ranks 2–10 J Q K A, suits S H D C, JK = joker</span>
    </div>
    <div class="row"><button class="act" id="cards-btn"></button><span id="cards-state" class="state"></span></div>
  </section>
</div>

<div id="tab-info">
<table>
  <tr><th></th><td class="p2">Player 2 (green)</td><td class="p1">Player 1 (red)</td></tr>
  <tr><th>Bets</th><td id="bet2">–</td><td id="bet1">–</td></tr>
  <tr><th>Cards</th><td id="card2">–</td><td id="card1">–</td></tr>
  <tr><th>Odds</th><td id="odds2">–</td><td id="odds1">–</td></tr>
  <tr><th>Best bet</th><td id="best2">–</td><td id="best1">–</td></tr>
</table>
</div>
<div id="status"></div>

<script>
const $=id=>document.getElementById(id);
const pct=x=>Math.round(x*100)+'%';
const SYM={S:'\\u2660',H:'\\u2665',D:'\\u2666',C:'\\u2663'};
const LABEL=c=>c==='JK'?'Joker':c.slice(0,-1)+SYM[c.slice(-1)];
function cardHTML(code){
  if(!code)return '<span class="pc empty"></span>';
  if(code==='JK')return '<span class="pc joker"><span>&#9733;</span><span class="pip">JOKER</span><span class="c2">&#9733;</span></span>';
  const r=code.slice(0,-1),s=code.slice(-1),red=(s==='H'||s==='D')?' red':'';
  return `<span class="pc${red}"><span>${r}<br>${SYM[s]}</span><span class="pip">${SYM[s]}</span><span class="c2">${r}<br>${SYM[s]}</span></span>`;}
const chipsText=c=>c?`green ${c.player_2} · red ${c.player_1}`:'–';
const cardsText=c=>c?`Player 2: ${(c.player_2||[]).map(LABEL).join(' ')||'none'} · Player 1: ${(c.player_1||[]).map(LABEL).join(' ')||'none'}`:'–';
const ago=t=>t?new Date(t*1000).toLocaleTimeString():'';

let tab='check';try{tab=localStorage.getItem('tab2')||tab}catch(e){}
function show(t){tab=t;document.querySelectorAll('nav [data-tab]').forEach(b=>b.classList.toggle('on',b.dataset.tab===t));
  $('tab-check').style.display=t==='check'?'':'none';$('tab-info').style.display=t==='info'?'':'none';
  try{localStorage.setItem('tab2',t)}catch(e){}}
document.querySelectorAll('nav [data-tab]').forEach(b=>b.onclick=()=>show(b.dataset.tab));show(tab);
$('start').onclick=()=>{if(!last.held||!last.held.started||confirm('Start a new game? The deck goes back to 54.'))fetch('/game/start',{method:'POST'}).then(tick)};
$('next').onclick=()=>fetch('/game/next',{method:'POST'}).then(tick);
let last={};
// Correction boxes: filled from live detection until you type in them.
const edited={chips:false,cards:false};
document.querySelectorAll('input.fix').forEach(el=>el.addEventListener('input',()=>{
  const w=el.id.split('-')[1];edited[w]=true;el.classList.add('edited');}));
const val=(w,k)=>$(`fix-${w}-${k}`).value;
async function act(what){
  const confirming=last.held&&last.held.checking[what];
  const body={action:confirming?'confirm':'recheck'};
  if(confirming)body.values=what==='chips'
    ?{player_1:+val('chips',1)||0,player_2:+val('chips',2)||0}
    :{player_1:val('cards',1),player_2:val('cards',2)};
  const r=await fetch('/hold/'+what,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  if(!r.ok){const e=await r.json().catch(()=>({}));$(what+'-state').textContent=e.error||'could not save';$(what+'-state').className='state err';return;}
  edited[what]=false;document.querySelectorAll(`input[id^=fix-${what}]`).forEach(el=>el.classList.remove('edited'));
  tick();
}
$('chips-btn').onclick=()=>act('chips');$('cards-btn').onclick=()=>act('cards');

let seq=-1,fseq=0;
async function tick(){
  try{
    const r=await (await fetch('/state.json',{cache:'no-store'})).json();last=r;
    const c=r.chips||{},g=r.game||{},h=r.held||{},d=r.display||{};
    // ---- check tab
    const live={player_1:[],player_2:[]};
    for(const p of ['player_1','player_2'])live[p]=((g.players||{})[p]||{cards:[]}).cards.map(x=>x.card);
    for(const [what,liveVal,text] of [['chips',c.counts,chipsText],['cards',live,cardsText]]){
      const checking=h.checking&&h.checking[what], held=h[what];
      $(what+'-live').innerHTML=checking?`Detected now: ${text(liveVal)}`:`<span class="held">Held: ${text(held)}</span>`;
      $(what+'-btn').textContent=checking?'Yes, correct':'Check again';
      $(what+'-btn').className='act'+(checking?' yes':'');
      // Fill the correction boxes: live values while checking (unless edited), held values otherwise.
      const src=checking?liveVal:held, fmt=what==='chips'?(v=>v==null?'':String(v)):(v=>(v||[]).join(' '));
      for(const k of [1,2]){const el=$(`fix-${what}-${k}`);el.disabled=!checking;
        if(!(checking&&edited[what])&&document.activeElement!==el)el.value=src?fmt(src['player_'+k]):'';}
      $(what+'-state').className='state';
      $(what+'-state').textContent=checking?(held?`Display still shows: ${text(held)}`:'Display shows nothing yet'):`Held since ${ago(h.at[what])}`;
    }
    if(tab==='check'){
      if(c.seq!==undefined&&c.seq!==seq){seq=c.seq;$('raw').src='/raw.jpg?'+seq;$('ann').src='/annotated.jpg?'+seq;}
      fseq++;const P=g.players||{};
      [['player_1',1],['player_2',2]].forEach(([p,k])=>{const f=(P[p]||{}).feed;
        if(f){$('feed'+k).src=`/feed/${encodeURIComponent(f)}.jpg?${fseq}`;
          $('feed'+k+'c').textContent=`${k===1?'Player 1':'Player 2'} · ${f}`;}});
    }
    // ---- info tab (held values only)
    const P=(d.game||{}).players||{},counts=d.chips;
    const n=k=>counts&&counts[k]!=null?counts[k]+(counts[k]===1?' chip':' chips'):'–';
    $('bet1').textContent=n('player_1');$('bet2').textContent=n('player_2');
    ['player_1','player_2'].forEach((p,i)=>{const x=P[p]||{},cs=x.cards||[];const k=i+1;
      $('card'+k).innerHTML=(cs.length?cs.map(c=>cardHTML(c.card)).join(' '):cardHTML(null))+
        `<small>${cs.map(c=>c.card_label).join(' · ')}${cs.length?' · ':''}${x.feed||''}</small>`;
      const multi=cs.length>1,tag=c=>multi?c.card_label+': ':'';
      $('odds'+k).innerHTML=cs.length?cs.map(c=>{const o=c.odds;
        return `${tag(c)}${pct(o.win)} win<small>${pct(o.tie)} tie · ${pct(o.lose)} lose · ${o.n} unseen cards</small>`}).join(''):'–';
      $('best'+k).innerHTML=cs.length?cs.map(c=>
        `${tag(c)}${c.bet}<small>${c.ev_per_chip>=0?'+':''}${c.ev_per_chip.toFixed(2)} chips expected per chip bet</small>`).join(''):'–';
    });
    $('deck').textContent=h.started?`Round ${h.round} · ${d.deck_left} of 54 left · ${(h.history||[]).length} rounds played`:'Game not started';
    $('next').style.display=h.started?'':'none';
    $('start').textContent=h.started?'New game':'Start game';
    const errs=[c.status==='error'?'overhead: '+c.error:'',g.status&&g.status!=='ok'?g.status:''].filter(Boolean);
    $('status').className=errs.length?'err':'';
    $('status').textContent=errs.join(' · ')||`cards from ${r.cards_url}`;
  }catch(e){$('status').className='err';$('status').textContent='app not reachable';}
}
setInterval(tick,500);tick();
</script></body></html>"""


def fetch_frame(cards_url: str, feed: str) -> bytes | None:
    q = urllib.parse.quote(feed)
    try:
        with urllib.request.urlopen(f"{cards_url.rstrip('/')}/api/frame?feed={q}", timeout=3) as r:
            if r.status == 200 and r.headers.get_content_type() == "image/jpeg":
                return r.read()
    except Exception:
        pass
    return None


def make_handler(state: State, table: Table, holds: Holds, cards_url: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, ctype, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")  # read by the graphics overlay
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def body_json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return {}

        def do_POST(self):
            if self.path == "/game/start":
                holds.start()
                return self.send(200, "application/json", b'{"ok":true}')
            if self.path == "/game/next":
                holds.next_round()
                return self.send(200, "application/json", b'{"ok":true}')
            if self.path in ("/hold/chips", "/hold/cards"):
                what = self.path.rsplit("/", 1)[1]
                req = self.body_json()
                action = req.get("action")
                # Values the operator typed (corrections); otherwise take the live detection.
                values = req.get("values")
                if action == "confirm" and isinstance(values, dict):
                    try:
                        if what == "chips":
                            values = {p: max(0, int(values.get(p, 0))) for p in ("player_1", "player_2")}
                        else:
                            parsed = {}
                            for p in ("player_1", "player_2"):
                                words = str(values.get(p, "")).replace(",", " ").split()
                                codes = [parse_code(w) for w in words]
                                if None in codes:
                                    bad = words[codes.index(None)]
                                    return self.send(400, "application/json",
                                                     json.dumps({"error": f"not a card: {bad}"}).encode())
                                parsed[p] = codes
                            values = parsed
                    except (TypeError, ValueError):
                        return self.send(400, "application/json", b'{"error":"bad values"}')
                else:
                    values = None
                with state.lock:
                    live_counts = dict(state.report.get("counts") or {})
                with holds.lock:
                    if action == "confirm":
                        if what == "chips":
                            holds.chips = values or live_counts
                        else:
                            holds.cards = values or table.cards_now()
                        holds.at[what] = time.time()
                        holds.checking[what] = False
                    elif action == "recheck":
                        holds.checking[what] = True
                    else:
                        return self.send(400, "application/json", b'{"error":"action must be confirm or recheck"}')
                    holds.save()
                return self.send(200, "application/json", b'{"ok":true}')
            self.send(404, "text/plain", b"not found")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            with state.lock:
                raw, ann, rep, seq = state.raw_jpg, state.ann_jpg, state.report, state.seq
            if path == "/":
                self.send(200, "text/html; charset=utf-8", PAGE.encode())
            elif path in ("/state.json", "/counts.json"):
                held = holds.snapshot()
                used = held.pop("used") if held["started"] else None
                game = table.summary(None, used)       # live cards
                shown = table.summary(held["cards"], used) if held["cards"] is not None else None
                body = {
                    "chips": {**rep, "seq": seq},          # live overhead detection
                    "game": game,
                    "held": {k: v for k, v in held.items() if k != "used"},
                    # What displays show: only operator-confirmed values.
                    "display": {"chips": held["chips"], "game": shown,
                                "round": held["round"] if held["started"] else None,
                                "deck_left": (shown or game)["deck_left"] if held["started"] else None},
                    "cards_url": cards_url,
                }
                self.send(200, "application/json", json.dumps(body).encode())
            elif path in ("/raw.jpg", "/annotated.jpg"):
                img = raw if path == "/raw.jpg" else ann
                if img is None:
                    self.send(503, "text/plain", b"no frame yet")
                else:
                    self.send(200, "image/jpeg", img)
            elif path.startswith("/feed/") and path.endswith(".jpg"):
                feed = urllib.parse.unquote(path[len("/feed/"):-len(".jpg")])
                jpg = fetch_frame(cards_url, feed)
                if jpg is None:
                    self.send(503, "text/plain", b"no frame from camera computer")
                else:
                    self.send(200, "image/jpeg", jpg)
            else:
                self.send(404, "text/plain", b"not found")
    return H


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="obs", help="obs | camera:N | video/image/dir path")
    ap.add_argument("--obs-host", default="localhost")
    ap.add_argument("--obs-port", type=int, default=4455)
    ap.add_argument("--obs-password", default=None)
    ap.add_argument("--obs-source", default="a.04", help="OBS source name (default a.04)")
    ap.add_argument("--obs-width", type=int, default=1920)
    ap.add_argument("--fps", type=float, default=4.0, help="analysis rate")
    ap.add_argument("--hold", type=float, default=3.0, help="seconds per image for dir sources")
    ap.add_argument("--cards", default="http://10.250.155.29:3000",
                    help="mw-cards-cv camera computer (serves /api/cards and /api/frame)")
    ap.add_argument("--p1-feed", default="camera-2", help="card feed for player 1 (red)")
    ap.add_argument("--p2-feed", default="camera-1", help="card feed for player 2 (green)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--color", action="append", default=[],
                    help="player=name:hue:tol[:min_s[:min_v]] (see chip_count.py)")
    args = ap.parse_args(argv)

    colors = dict(DEFAULT_COLORS)
    for s in args.color:
        p, c = parse_color_arg(s)
        colors[p] = c

    state = State()
    table = Table({"player_1": args.p1_feed, "player_2": args.p2_feed})
    holds = Holds()
    threading.Thread(target=worker, args=(args, colors, state), daemon=True).start()
    threading.Thread(target=cards_worker, args=(args.cards, table), daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(state, table, holds, args.cards))
    print(f"[overhead] serving http://{args.host}:{args.port}  source={args.source}  cards={args.cards}",
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
