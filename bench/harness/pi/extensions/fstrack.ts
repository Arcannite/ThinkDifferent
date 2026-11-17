/**
 * fstrack — write filesystem-journal markers at pi turn/tool boundaries.
 *
 * The daemon (harness/fstrack/fstrackd.py) tails this file and creates a git
 * commit per boundary, with the tool name/id/turn as commit trailers. That is
 * what makes a snapshot map to a conversation turn for replay.
 *
 * This extension must never break the agent: every write is best-effort.
 */

import { type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { appendFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const OUT = process.env.FSTRACK_OUT || "/logs/artifacts/fstrack";
const EVENTS = join(OUT, "events.jsonl");

function summarizeInput(input: unknown): Record<string, unknown> {
  if (!input || typeof input !== "object") return {};
  const src = input as Record<string, unknown>;
  const out: Record<string, unknown> = {};
  for (const key of ["path", "file_path", "command", "pattern", "query", "url"]) {
    const value = src[key];
    if (typeof value === "string") out[key] = value.slice(0, 2000);
  }
  return out;
}

function emit(kind: string, data: Record<string, unknown> = {}): void {
  try {
    mkdirSync(OUT, { recursive: true });
    appendFileSync(
      EVENTS,
      JSON.stringify({ kind, ts: Date.now(), ...data }) + "\n",
    );
  } catch {
    // Tracking is observability; it must never interfere with the task.
  }
}

function sessionInfo(ctx: {
  sessionManager: {
    getSessionId(): string;
    getSessionFile(): string | null;
    getLeafId(): string | null;
  };
}): Record<string, unknown> {
  try {
    return {
      sessionId: ctx.sessionManager.getSessionId(),
      sessionFile: ctx.sessionManager.getSessionFile(),
      leafId: ctx.sessionManager.getLeafId(),
    };
  } catch {
    return {};
  }
}

export default function (pi: ExtensionAPI) {
  let turn = 0;

  pi.on("session_start", (event, ctx) => {
    emit("session_start", { reason: event.reason, ...sessionInfo(ctx) });
  });

  pi.on("turn_end", (_event, ctx) => {
    turn += 1;
    emit("turn_end", { turn, ...sessionInfo(ctx) });
  });

  pi.on("tool_call", (event, ctx) => {
    emit("tool_call", {
      turn,
      tool: event.toolName,
      toolCallId: event.toolCallId,
      input: summarizeInput(event.input),
      ...sessionInfo(ctx),
    });
  });;

  pi.on("tool_result", (event, ctx) => {
    emit("tool_result", {
      turn,
      tool: event.toolName,
      toolCallId: event.toolCallId,
      input: summarizeInput(event.input),
      isError: event.isError === true,
      ...sessionInfo(ctx),
    });
  });

  pi.on("agent_settled", (_event, ctx) => {
    emit("agent_settled", sessionInfo(ctx));
  });
}
