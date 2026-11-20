import { promises as fs } from "fs";
import path from "path";
import { NextRequest, NextResponse } from "next/server";

// Where the /cards overlay panels sit, shared by every browser showing the
// overlay (a Chrome tab where you drag them, and the OBS browser source).
// Positions are the panel's top-left corner in 1920x1080 frame pixels.

export const dynamic = "force-dynamic";

type Layout = Record<string, { x: number; y: number }>;
const FILE = path.join(process.cwd(), ".cards-layout.json");

async function read(): Promise<Layout> {
  try {
    return JSON.parse(await fs.readFile(FILE, "utf8")) as Layout;
  } catch {
    return {};
  }
}

export async function GET() {
  return NextResponse.json(await read(), { headers: { "Cache-Control": "no-store" } });
}

/** `{ player, x, y }` moves one panel; `{ player, reset: true }` puts it back. */
export async function POST(req: NextRequest) {
  const body = (await req.json().catch(() => ({}))) as { player?: unknown; x?: unknown; y?: unknown; reset?: unknown };
  if (typeof body.player !== "string" || !/^(player_[12]|board)$/.test(body.player)) {
    return NextResponse.json({ error: "player must be player_1, player_2 or board" }, { status: 400 });
  }
  const layout = await read();
  if (body.reset) {
    delete layout[body.player];
  } else if (typeof body.x === "number" && typeof body.y === "number" && Number.isFinite(body.x) && Number.isFinite(body.y)) {
    layout[body.player] = { x: Math.round(body.x), y: Math.round(body.y) };
  } else {
    return NextResponse.json({ error: "expected numeric x and y" }, { status: 400 });
  }
  await fs.writeFile(FILE, JSON.stringify(layout, null, 2));
  return NextResponse.json(layout);
}
