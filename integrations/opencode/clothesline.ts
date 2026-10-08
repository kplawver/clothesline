import { spawn, spawnSync } from "node:child_process";

// OpenCode plugin: runs `clothesline hook` on a session's first and later prompts and
// appends any additionalContext it prints to the user's message. OpenCode has no timer
// or idle-context hook, so waiting messages surface on the next prompt.
function hook(event: string, payload: Record<string, unknown>): Promise<string> {
  const { promise, resolve } = Promise.withResolvers<string>();
  let output = "";
  const child = spawn("clothesline", ["hook", "--harness", "opencode", "--event", event], {
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

interface SessionEvent {
  type?: string;
  properties?: { info?: { id?: unknown }; sessionID?: unknown };
}

export const ClotheslinePresence = async (ctx: { directory?: string; worktree?: string }) => {
  const cwd = ctx.directory ?? ctx.worktree;
  const active = new Set<string>();

  // OpenCode never announces that the process is closing, so sign off what is still online.
  process.once("exit", () => {
    for (const session_id of active) {
      spawnSync("clothesline", ["hook", "--harness", "opencode", "--event", "SessionEnd"], {
        input: JSON.stringify({ session_id, cwd }),
        timeout: 2000,
      });
    }
  });

  return {
    event: async ({ event }: { event: SessionEvent }) => {
      if (event?.type !== "session.deleted") return;
      const session_id = event.properties?.info?.id ?? event.properties?.sessionID;
      if (typeof session_id !== "string" || !active.delete(session_id)) return;
      await hook("SessionEnd", { session_id, cwd });
    },
    "chat.message": async (input: { sessionID?: string }, output: { parts: unknown[] }) => {
      const session_id = input?.sessionID;
      if (!session_id) return;
      const event = active.has(session_id) ? "UserPromptSubmit" : "SessionStart";
      active.add(session_id);
      const text = await hook(event, { session_id, cwd });
      if (text) output.parts.push({ type: "text", text });
    },
  };
};
