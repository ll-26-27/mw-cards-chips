"use client";

// Live card-game overlay: each player's cards, P(win) and chip bet, side by
// side at the top (or bottom) centre of a 1920x1080 frame. Data comes from the mw-cards-chips app
// (scripts/overhead_app.py, GET /state.json), which merges the overhead chip
// count with the card feeds from mw-cards-cv.
//
// Add to OBS as a Browser source (1920x1080) above the table camera:
//   http://localhost:3100/cards
// Query params: source=<chips app url> (default http://localhost:8765),
//   edge=top|bottom (default top), bg=transparent|green|black (default transparent).
// Drag a panel to move it (saved via /api/cards-layout, so OBS follows);
// double-click a panel to put both back in the default spot.

import { useEffect, useRef, useState } from "react";
// Fonts come from layout.tsx: --font-label is Inter Bold (card faces); body text uses the system face.
const FONT = { bold: "var(--font-label), system-ui, sans-serif", text: "ui-sans-serif, system-ui, -apple-system, sans-serif" };

const FRAME = { width: 1920, height: 1080 };
const MARGIN = 64;
// Space between the two players' panels, centred on the frame.
const GAP = 160;

type Odds = { win: number | null; tie: number | null; lose: number | null; n: number };
type PlayerCard = { card: string; card_label: string; odds: Odds; bet: number; ev_per_chip: number };
// Hold'em (control panel run with --game holdem): one hand read per player instead of per-card odds.
type Hand = { name: string | null; win: number; tie: number; lose: number; action: string; to_call: number; pot_odds: number };
type Player = { feed: string; cards: PlayerCard[]; hand?: Hand };
type Item = { key: string; name: string; color: string };
type Layout = Record<string, { x: number; y: number }>;
type State = {
  // Only operator-confirmed ("Yes, correct") values from the control panel.
  display?: {
    chips?: Record<string, number> | null;
    game?: { deck_left?: number; players?: Record<string, Player>; board?: string[]; stage?: string; error?: string | null } | null;
    mode?: string;
    round?: number | null;
    deck_left?: number | null;
  };
};

// Left to right on screen: Player 2 (green) on the left, Player 1 (red) on the right.
const PLAYERS = [
  { key: "player_2", name: "Player 2", color: "#30a46c" },
  { key: "player_1", name: "Player 1", color: "#e5484d" },
] as const;
const BOARD: Item = { key: "board", name: "Board", color: "#9a9a9a" };

const SUIT: Record<string, string> = { S: "♠", H: "♥", D: "♦", C: "♣" };

function useParams() {
  const [p, setP] = useState({ source: "http://localhost:8765", edge: "top", bg: "transparent" });
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    setP({
      source: (q.get("source") || "http://localhost:8765").replace(/\/$/, ""),
      edge: q.get("edge") === "bottom" ? "bottom" : "top",
      bg: q.get("bg") || "transparent",
    });
  }, []);
  return p;
}

function useState_(source: string) {
  const [state, setState] = useState<State>({});
  const [ok, setOk] = useState(false);
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const r = await fetch(`${source}/state.json`, { cache: "no-store" });
        const s = (await r.json()) as State;
        if (alive) { setState(s); setOk(true); }
      } catch {
        if (alive) setOk(false);
      }
    };
    tick();
    const id = setInterval(tick, 500);
    return () => { alive = false; clearInterval(id); };
  }, [source]);
  return { state, ok };
}

function Card({ code }: { code: string }) {
  const joker = code === "JK";
  const rank = joker ? "" : code.slice(0, -1);
  const suit = joker ? "★" : SUIT[code.slice(-1)] ?? "?";
  const red = !joker && (code.endsWith("H") || code.endsWith("D"));
  return (
    <div
      style={{
        width: 120, height: 168, borderRadius: 12, background: "#fafafa",
        color: red ? "#d42a2a" : "#111", position: "relative",
        boxShadow: "0 6px 18px rgba(0,0,0,.45)", fontFamily: FONT.bold,
      }}
    >
      <div style={{ position: "absolute", top: 10, left: 12, fontSize: 28, lineHeight: 1, textAlign: "center" }}>
        {rank}<br />{suit}
      </div>
      <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", fontSize: joker ? 26 : 64 }}>
        {joker ? "JOKER" : suit}
      </div>
      <div style={{ position: "absolute", bottom: 10, right: 12, fontSize: 28, lineHeight: 1, textAlign: "center", transform: "rotate(180deg)" }}>
        {rank}<br />{suit}
      </div>
    </div>
  );
}

function Panel({ name, color, player, bet, align }: {
  name: string; color: string; player?: Player; bet?: number; align: "left" | "right";
}) {
  const cards = player?.cards ?? [];
  // One value, or "Q♥ 75% · K♥ 82%" when a player has several cards.
  const perCard = (f: (c: PlayerCard) => string) =>
    cards.length === 0 ? "–" : cards.length === 1 ? f(cards[0]) : cards.map((c) => `${c.card_label} ${f(c)}`).join(" · ");
  const text = { fontFamily: FONT.text, fontSize: 30, color: "#fff", lineHeight: 1.25 };
  return (
    <div
      style={{
        display: "flex", flexDirection: "column", gap: 16,
        alignItems: align === "left" ? "flex-start" : "flex-end",
        background: "rgba(10,10,10,.72)", borderRadius: 16, padding: "20px 24px",
        borderLeft: align === "left" ? `8px solid ${color}` : undefined,
        borderRight: align === "right" ? `8px solid ${color}` : undefined,
      }}
    >
      <div style={{ ...text, color }}>{name}</div>
      <div style={{ display: "flex", gap: 14, flexDirection: align === "left" ? "row" : "row-reverse" }}>
        {cards.length === 0 ? (
          <div style={{ width: 120, height: 168, borderRadius: 12, border: "3px dashed rgba(255,255,255,.35)" }} />
        ) : (
          cards.map((c, i) => <Card key={`${c.card}-${i}`} code={c.card} />)
        )}
      </div>
      {player?.hand ? (
        <div style={{ ...text, textAlign: align }}>
          <div>{player.hand.name ?? "–"}</div>
          <div>Equity {Math.round(player.hand.win * 100)}%</div>
          <div>Suggestion {player.hand.action}</div>
          <div>Actual bet {bet ?? "–"}</div>
        </div>
      ) : (
        <div style={{ ...text, textAlign: align }}>
          <div>P(win) {perCard((c) => (c.odds?.win == null ? "–" : `${Math.round(c.odds.win * 100)}%`))}</div>
          <div>Optimal bet {perCard((c) => String(c.bet))}</div>
          <div>Actual bet {bet ?? "–"}</div>
        </div>
      )}
    </div>
  );
}

function BoardPanel({ board, stage, error }: { board: string[]; stage?: string; error?: string | null }) {
  const text = { fontFamily: FONT.text, fontSize: 30, color: "#fff", lineHeight: 1.25 };
  return (
    <div
      style={{
        display: "flex", flexDirection: "column", alignItems: "center", gap: 16,
        background: "rgba(10,10,10,.72)", borderRadius: 16, padding: "20px 24px",
      }}
    >
      <div style={{ ...text, color: BOARD.color }}>{error ?? stage ?? "Board"}</div>
      <div style={{ display: "flex", gap: 14 }}>
        {Array.from({ length: 5 }, (_, i) =>
          board[i] ? <Card key={i} code={board[i]} /> : (
            <div key={i} style={{ width: 120, height: 168, borderRadius: 12, border: "3px dashed rgba(255,255,255,.35)" }} />
          ),
        )}
      </div>
    </div>
  );
}

export default function CardsOverlay() {
  const { source, edge, bg } = useParams();
  const { state, ok } = useState_(source);
  const box = useRef<HTMLDivElement>(null);
  const [fit, setFit] = useState(1);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) =>
      setFit(Math.min(e.contentRect.width / FRAME.width, e.contentRect.height / FRAME.height)),
    );
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const background = bg === "green" ? "#00b140" : bg === "black" ? "#000" : "transparent";
  const counts = state.display?.chips ?? {};
  const players = state.display?.game?.players ?? {};
  const holdem = state.display?.mode === "holdem";
  // Left to right: Player 2, (board,) Player 1.
  const items: Item[] = holdem ? [PLAYERS[0], BOARD, PLAYERS[1]] : [...PLAYERS];
  const vertical = edge === "bottom" ? { bottom: MARGIN } : { top: MARGIN };

  // ---- dragging: positions live on the server so OBS follows a drag made in Chrome ----
  const frame = useRef<HTMLDivElement>(null);
  const panels = useRef<Record<string, HTMLDivElement | null>>({});
  const [layout, setLayout] = useState<Layout>({});
  const layoutRef = useRef(layout);
  layoutRef.current = layout;
  const dragging = useRef<{ player: string; sx: number; sy: number; ox: number; oy: number } | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (dragging.current) return;
      try {
        const l = (await (await fetch("/api/cards-layout", { cache: "no-store" })).json()) as Layout;
        if (alive && !dragging.current) setLayout(l);
      } catch {}
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  const measure = (key: string) => {
    const el = panels.current[key], fr = frame.current;
    if (!el || !fr) return { x: 0, y: 0 };
    const a = el.getBoundingClientRect(), b = fr.getBoundingClientRect();
    return { x: (a.left - b.left) / fit, y: (a.top - b.top) / fit };
  };
  const save = (body: object) =>
    fetch("/api/cards-layout", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }).catch(() => {});

  const onDown = (key: string) => (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    // Pin both panels where they are, so moving one doesn't re-centre the other.
    const next: Layout = { ...layoutRef.current };
    for (const p of items) next[p.key] ??= measure(p.key);
    setLayout(next);
    dragging.current = { player: key, sx: e.clientX, sy: e.clientY, ox: next[key].x, oy: next[key].y };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragging.current;
    if (!d) return;
    const x = Math.max(0, Math.min(FRAME.width - 100, d.ox + (e.clientX - d.sx) / fit));
    const y = Math.max(0, Math.min(FRAME.height - 100, d.oy + (e.clientY - d.sy) / fit));
    setLayout((l) => ({ ...l, [d.player]: { x, y } }));
  };
  const onUp = async () => {
    if (!dragging.current) return;
    dragging.current = null;
    for (const p of items) {
      const pos = layoutRef.current[p.key];
      if (pos) await save({ player: p.key, ...pos });
    }
  };
  const onReset = async () => {
    for (const p of [...PLAYERS, BOARD]) await save({ player: p.key, reset: true });
    setLayout({});
  };

  const panel = (p: Item, pos?: { x: number; y: number }) => (
    <div
      key={p.key}
      ref={(el) => { panels.current[p.key] = el; }}
      onPointerDown={onDown(p.key)}
      onPointerMove={onMove}
      onPointerUp={onUp}
      onPointerCancel={onUp}
      onDoubleClick={onReset}
      title="Drag to move · double-click to reset both"
      style={{
        ...(pos ? { position: "absolute", left: pos.x, top: pos.y } : {}),
        pointerEvents: "auto", cursor: "grab", userSelect: "none", touchAction: "none",
      }}
    >
      {p.key === BOARD.key ? (
        <BoardPanel board={state.display?.game?.board ?? []} stage={state.display?.game?.stage} error={state.display?.game?.error} />
      ) : (
        <Panel name={p.name} color={p.color} player={players[p.key]} bet={counts[p.key]} align={p.key === PLAYERS[0].key ? "left" : "right"} />
      )}
    </div>
  );

  return (
    <>
      {/* The app's globals paint the body dark; a browser source needs it clear. */}
      <style>{`html,body{background:${background} !important;margin:0;overflow:hidden}`}</style>
      <div ref={box} style={{ position: "fixed", inset: 0, background }}>
        <div
          ref={frame}
          style={{
            position: "absolute", left: "50%", top: "50%", width: FRAME.width, height: FRAME.height,
            transform: `translate(-50%, -50%) scale(${fit})`,
          }}
        >
          {ok && (
            <div
              style={{
                position: "absolute", ...vertical, left: 0, right: 0,
                display: "flex", justifyContent: "center", alignItems: "flex-start", gap: holdem ? 40 : GAP,
                pointerEvents: "none",
              }}
            >
              {items.filter((p) => !layout[p.key]).map((p) => panel(p))}
            </div>
          )}
          {ok && items.filter((p) => layout[p.key]).map((p) => panel(p, layout[p.key]))}
        </div>
      </div>
    </>
  );
}
