import { startSidecar } from "../../sidecar/src/runner";
import { SDK_VERSION, BUN_VERSION } from "./versions";

// This module does not import OpenCode until initialize, so hello is immediate.
if (Bun.version !== BUN_VERSION) throw new Error("Install the tested OpenCode runtime from Downloads.");
startSidecar({
  harness: "opencode",
  info: () => ({ harness_version: SDK_VERSION, runtime_version: BUN_VERSION }),
  createSession: async options => {
    try { return await (await import("./adapter")).createSession(options); }
    catch { throw new Error("OpenCode initialization failed. Check the selected provider and reinstall the component if needed."); }
  },
});
