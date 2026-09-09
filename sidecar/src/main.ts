import { startSidecar } from "./runner";
import { createSession, piVersion } from "./pi-adapter";

startSidecar({ harness: "pi", info: () => ({ pi_version: piVersion() }), createSession });
