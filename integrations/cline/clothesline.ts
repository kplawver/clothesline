import type { AgentPlugin } from "@cline/sdk";
import { spawn, spawnSync } from "node:child_process";

// Cline plugin: runs `clothesline hook` before each run and appends any additionalContext
// it prints as run-start context (needs an SDK with `beforeRun` appendContext, 0.0.84+).
// Cline has no session-end hook, so the process exit signs the session off.
function hook(event: string, payload: Record<string, unknown>): Promise<string> {
  const { promise, resolve } = Promise.withResolvers<string>();
  let output = "";
  const child = spawn("clothesline", ["hook", "--harness", "cline", "--event", event], {
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

interface SetupContext {
  workspaceInfo?: { rootPath?: string };
  session?: { sessionId?: string };
}

function createClotheslinePlugin(): AgentPlugin {
  let cwd = process.cwd();
  let session_id = `cline-${process.pid}`;
  let started = false;
  return {
    name: "clothesline-presence",
    manifest: { capabilities: ["hooks"] },
    setup(_api, ctx: SetupContext) {
      cwd = ctx?.workspaceInfo?.rootPath ?? cwd;
      session_id = ctx?.session?.sessionId ?? session_id;
      process.once("exit", () => {
        if (!started) return;
        spawnSync("clothesline", ["hook", "--harness", "cline", "--event", "SessionEnd"], {
          input: JSON.stringify({ session_id, cwd }),
          timeout: 2000,
        });
      });
    },
    hooks: {
      async beforeRun() {
        const event = started ? "UserPromptSubmit" : "SessionStart";
        started = true;
        const text = await hook(event, { session_id, cwd });
        if (text) return { appendContext: text };
      },
    },
  };
}

export const clotheslinePresence = createClotheslinePlugin();
export default clotheslinePresence;
