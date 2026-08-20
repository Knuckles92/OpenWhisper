/**
 * Bundle src/tools.test.ts with esbuild and run it under node:test.
 * Type-only Pi/RPC imports are erased, so this does not load the Pi SDK.
 */
import { spawnSync } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const outdir = await mkdtemp(path.join(tmpdir(), "ow-tools-"));
const outfile = path.join(outdir, "tools.test.mjs");
let status = 1;
try {
  await build({
    entryPoints: [path.join(root, "src", "tools.test.ts")],
    outfile,
    bundle: true,
    platform: "node",
    format: "esm",
  });
  const result = spawnSync(process.execPath, ["--test", outfile], {
    stdio: "inherit",
    cwd: root,
  });
  status = result.status ?? 1;
} finally {
  await rm(outdir, { recursive: true, force: true });
}
process.exit(status);
