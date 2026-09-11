/**
 * Bundle src/tools.test.ts with esbuild and run it under node:test.
 * Includes the real Pi SDK and production runner against a local fake provider.
 */
import { spawnSync } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const outdir = await mkdtemp(path.join(root, "node_modules", ".ow-tests-"));
const testNames = ["tools.test", "text-provider.test", "pi-integration.test"];
const outfiles = testNames.map((name) => path.join(outdir, name + ".mjs"));
let status = 1;
try {
  await build({
    entryPoints: testNames.map((name) => path.join(root, "src", name + ".ts")),
    outdir,
    outExtension: { ".js": ".mjs" },
    bundle: true,
    packages: "external",
    platform: "node",
    format: "esm",
  });
  await build({
    entryPoints: [path.join(root, "src", "main.ts")],
    outfile: path.join(outdir, "main.mjs"), bundle: true,
    packages: "external", platform: "node", format: "esm",
  });
  const result = spawnSync(process.execPath, ["--test", ...outfiles], {
    stdio: "inherit",
    cwd: root,
    env: { ...process.env, OPENWHISPER_TEST_SIDECAR_ENTRY: path.join(outdir, "main.mjs") },
  });
  status = result.status ?? 1;
} finally {
  await rm(outdir, { recursive: true, force: true });
}
process.exit(status);
