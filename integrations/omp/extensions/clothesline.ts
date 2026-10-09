import { spawn } from "node:child_process";

// Presence and inbox polling for Clothesline. Runs `clothesline hook` and relays any
// additionalContext it prints into the session as a custom message.
const POLL_MS = 60_000;

interface Context {
  cwd?: string;
  sessionManager?: { getSessionId?: () => unknown };
  setInterval(callback: () => void, ms: number): unknown;
}

interface Pi {
  on(event: string, handler: (event: unknown, ctx: Context) => unknown): void;
  sendMessage(message: Record<string, unknown>, options: { deliverAs: string }): void;
}

function hook(event: string, payload: Record<string, unknown>): Promise<string> {
  const { promise, resolve } = Promise.withResolvers<string>();
  let output = "";
  const child = spawn("clothesline", ["hook", "--harness", "omp", "--event", event], {
    stdio: ["pipe", "pipe", "ignore"],
    timeout: 5000,
  });
  child.stdout.on("data", (chunk) => (output += chunk));
  child.once("error", () => resolve(""));
  child.once("close", () => {
    try {
      const parsed: { hookSpecificOutput?: { additionalContext?: unknown } } = JSON.parse(output);
      const text = parsed?.hookSpecificOutput?.additionalContext;
      resolve(typeof text === "string" ? text : "");
    } catch {
      resolve("");
    }
  });
  child.stdin.on("error", () => resolve(""));
  child.stdin.end(JSON.stringify(payload));
  return promise;
}

export default function (pi: Pi) {
  // Last text delivered per session, so an unacknowledged message is not re-sent every minute.
  const delivered = new Map<string, string>();
  let current: Context | null = null;
  let timerStarted = false;

  const sessionId = (ctx: Context): string | null => {
    const id = ctx.sessionManager?.getSessionId?.();
    return typeof id === "string" && id ? id : null;
  };
  const check = async (event: string, ctx: Context): Promise<string | null> => {
    const id = sessionId(ctx);
    if (!id) return null;
    const text = await hook(event, { session_id: id, cwd: ctx.cwd });
    if (!text || delivered.get(id) === text) return null;
    delivered.set(id, text);
    return text;
  };
  const message = (text: string) => ({ customType: "clothesline", content: text, display: true });

  const begin = async (_event: unknown, ctx: Context) => {
    current = ctx;
    const start = await check("SessionStart", ctx);
    if (start) pi.sendMessage(message(start), { deliverAs: "nextTurn" });
    if (timerStarted) return; // One timer serves every session; it polls whichever is current.
    timerStarted = true;
    ctx.setInterval(async () => {
      const poll = current && (await check("Poll", current));
      if (poll) pi.sendMessage(message(poll), { deliverAs: "followUp" });
    }, POLL_MS);
  };
  const leave = async (_event: unknown, ctx: Context) => {
    const id = sessionId(ctx);
    if (!id) return;
    delivered.delete(id);
    await hook("SessionEnd", { session_id: id, cwd: ctx.cwd });
  };
  pi.on("session_start", begin);
  pi.on("session_switch", begin);

  pi.on("before_agent_start", async (_event, ctx) => {
    const turn = await check("UserPromptSubmit", ctx);
    if (turn) return { message: message(turn) };
  });

  // /new, /resume, /fork and branching replace the session without a shutdown.
  pi.on("session_before_switch", leave);
  pi.on("session_shutdown", async (event, ctx) => {
    timerStarted = false; // The host clears its timers on shutdown.
    current = null;
    await leave(event, ctx);
  });
}
