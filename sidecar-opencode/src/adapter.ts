/** All OpenCode SDK/plugin integration lives here. The Python host owns meeting state. */
import { OpenCode } from "@opencode/sdk";
import { Plugin } from "@opencode/plugin";
import type { CreateSessionOptions, HarnessSession, TurnContext } from "../../sidecar/src/session";
import { providerConfig } from "./provider";
import { SDK_VERSION } from "./versions";

type Active = { id: string; requestId: string; systemPrompt: string; canceled: boolean };
export async function createSession(options: CreateSessionOptions): Promise<HarnessSession> {
  const directory = process.env.OPENWHISPER_OPENCODE_ROOT;
  if (!directory) throw new Error("OpenCode requires an isolated runtime directory");
  let active: Active | null = null;
  let closed = false;
  const names = new Set(options.tools.map(t => t.name));
  const plugin = Plugin.define({
    id: "openwhisper.meeting",
    async setup(ctx) {
      await ctx.tool.transform(editor => {
        for (const tool of editor.list()) editor.remove(tool.id);
        for (const tool of options.tools) {
          editor.add({
            name: tool.name,
            description: tool.description,
            input: { ...tool.parameters } as import("effect").JsonSchema.JsonSchema,
            options: { codemode: false },
            execute: async (input, context) => {
              const request = active;
              if (!request || request.canceled || request.id !== context.sessionID) {
                throw new Error("Meeting request is no longer active");
              }
              options.onEvent?.({ type: "tool_execution_start", toolName: tool.name });
              const result = await tool.execute(input as Record<string, unknown>);
              if (active !== request || request.canceled) throw new Error("Meeting request is no longer active");
              options.onEvent?.({ type: "tool_execution_end", toolName: tool.name });
              return { content: result.text };
            },
          });
        }
      });
      await ctx.agent.transform(editor => {
        for (const agent of editor.list()) if (String(agent.id) !== "meeting") editor.remove(String(agent.id));
        editor.default("meeting");
      });
      await ctx.catalog.transform(editor => {
        for (const provider of editor.provider.list()) if (String(provider.provider.id) !== "openwhisper") editor.provider.remove(String(provider.provider.id));
      });
      await ctx.session.hook("context", event => {
        if (!active || active.canceled || active.id !== event.sessionID) throw new Error("Inactive meeting request");
        // Replace the coding charter and ambient instructions with the host's pass charter.
        event.system = [{ type: "text", text: active.systemPrompt }];
        for (const name of Object.keys(event.tools)) if (!names.has(name)) delete event.tools[name];
        if (Object.keys(event.tools).length !== names.size) throw new Error("OpenCode meeting tool registration is incomplete");
        event.generation.maxTokens = options.modelMetadata?.max_output_tokens ?? 4096;
      });
      await ctx.session.hook("retry", event => {
        // Python owns retries and deadlines; never hide a provider failure in an unbounded loop.
        event.decision = { retry: false };
      });
    },
  });
  const host = await OpenCode.create({
    database: { path: ":memory:" },
    events: { persist: false },
    config: {
      directory,
      project: false,
      content: JSON.stringify({
        update: "disable", warming: false, share: "disabled", snapshots: false,
        compaction: { auto: false }, default_agent: "meeting",
        agents: {
          meeting: {
            mode: "primary", system: "Follow the meeting host instructions.",
            permissions: [{ action: "*", resource: "*", effect: "allow" }],
          },
          title: { disabled: true },
        },
        providers: { openwhisper: providerConfig(options) },
      }),
    },
    models: { fetch: false, snapshot: false },
    fs: { filewatcher: false, fff: false },
    plugins: [plugin],
    // SDK diagnostics can include requests and credentials. Publish only our own safe lifecycle events.
    log: { level: "fatal", emit: () => {} },
  });
  return {
    isBusy: () => active !== null,
    async runTurn(text: string, context?: TurnContext) {
      if (closed || active || !context) throw new Error("OpenCode is closed, busy, or missing request context");
      const request: Active = { id: "", requestId: context.requestId, systemPrompt: context.systemPrompt, canceled: false };
      active = request;
      const controller = new AbortController();
      let pump: Promise<void> | undefined;
      let streamError: unknown;
      try {
        const session = await host.sessions.create({
          title: "Meeting pass", agent: "meeting", model: { providerID: "openwhisper", id: options.modelId },
          location: { directory },
        });
        request.id = session.id;
        if (request.canceled) return { aborted: true, usage: {} };
        const iterator = host.events.subscribe({ signal: controller.signal })[Symbol.asyncIterator]();
        await iterator.next(); // Wait for the connected marker before submitting work.
        pump = (async () => {
          while (!controller.signal.aborted) {
            const next = await iterator.next();
            if (next.done) {
              if (!controller.signal.aborted) throw new Error("OpenCode activity stream disconnected");
              break;
            }
            const event = next.value as unknown as Record<string, any>;
            if (event.data?.sessionID !== session.id) continue;
            const delta = ({
              "session.text.started": "text_delta",
              "session.text.delta": "text_delta",
              "session.reasoning.started": "thinking_delta",
              "session.reasoning.delta": "thinking_delta",
              "session.tool.input.started": "toolcall_delta",
              "session.tool.input.delta": "toolcall_delta",
              "session.tool.input.ended": "toolcall_delta",
            } as Record<string, string>)[event.type];
            if (delta) options.onEvent?.({ type: "message_update", delta });
          }
        })().catch(error => {
          if (!controller.signal.aborted) {
            streamError = error;
            void host.sessions.interrupt({ sessionID: session.id, continue: false }).catch(() => {});
          }
        });
        options.onEvent?.({ type: "agent_start" });
        await host.sessions.prompt({ sessionID: session.id, text });
        await host.sessions.wait({ sessionID: session.id });
        if (request.canceled) return { aborted: true, usage: {} };
        if (streamError) throw new Error("OpenCode activity stream failed");
        const messages = await host.sessions.context({ sessionID: session.id });
        const assistants = messages.filter(m => m.type === "assistant");
        const last = assistants.at(-1);
        if (!last || last.error || ["error", "length", "content-filter"].includes(last.finish ?? "")) {
          throw new Error("OpenCode model turn failed" + (last?.error?.status ? " (HTTP " + last.error.status + ")" : ""));
        }
        const usage = { harness: "opencode", sdk_version: SDK_VERSION, input_tokens: 0, output_tokens: 0, reasoning_tokens: 0 };
        for (const message of assistants) {
          usage.input_tokens += message.tokens?.input ?? 0;
          usage.output_tokens += message.tokens?.output ?? 0;
          usage.reasoning_tokens += message.tokens?.reasoning ?? 0;
        }
        options.onEvent?.({ type: "agent_end" });
        return { aborted: false, usage };
      } catch (error) {
        if (request.canceled) return { aborted: true, usage: {} };
        if (error instanceof Error && error.message.startsWith("OpenCode ")) throw error;
        throw new Error("OpenCode request failed");
      } finally {
        controller.abort();
        await pump;
        if (request.id) {
          await host.sessions.interrupt({ sessionID: request.id, continue: false }).catch(() => {});
          await host.sessions.remove({ sessionID: request.id }).catch(() => {});
        }
        active = null;
      }
    },
    async abort() {
      const request = active;
      if (!request) return;
      request.canceled = true;
      if (request.id) {
        await host.sessions.interrupt({ sessionID: request.id, continue: false });
        await host.sessions.wait({ sessionID: request.id });
      }
    },
    async dispose() {
      closed = true;
      if (active) { active.canceled = true; if (active.id) await host.sessions.interrupt({ sessionID: active.id, continue: false }); }
      await host.close();
    },
  };
}
