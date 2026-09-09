import type { TextModelMetadata } from "./text-provider";

export type ParamSpec =
  | { type: "string"; description?: string }
  | { type: "number"; description?: string }
  | { type: "boolean"; description?: string }
  | { type: "array"; items: ParamSpec; description?: string; minItems?: number }
  | ObjectSpec;

export interface ObjectSpec {
  type: "object";
  description?: string;
  properties?: Record<string, ParamSpec>;
  required?: string[];
  additionalProperties?: boolean;
}

export interface MeetingToolDef {
  name: string;
  label: string;
  description: string;
  parameters: ObjectSpec;
  execute: (params: Record<string, any>) => Promise<{ text: string; details?: unknown }>;
}

export interface TurnResult {
  aborted: boolean;
  /** Best-effort token usage for the turn; empty when unavailable. */
  usage: Record<string, unknown>;
}

export interface HarnessSession {
  runTurn(userMessage: string, context?: TurnContext): Promise<TurnResult>;
  isBusy(): boolean;
  abort(): Promise<void>;
  dispose(): Promise<void>;
}

export interface CreateSessionOptions {
  provider: string;
  modelId: string;
  apiKey?: string;
  baseUrl?: string;
  kind?: string;
  modelMetadata?: TextModelMetadata;
  headers?: Record<string, string>;
  /** The ONLY tools the session gets; built-ins are disabled structurally. */
  tools: MeetingToolDef[];
  log: (level: "debug" | "info" | "warning" | "error", msg: string) => void;
  /** Actual SDK activity; a busy flag alone does not prove a provider is making progress. */
  onEvent?: (info: SessionProgress) => void;
}

export interface SessionProgress {
  type: string;
  delta?: string;
  toolName?: string;
}


export interface TurnContext { requestId: string; systemPrompt: string; }
