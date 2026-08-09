/**
 * Bundle the sidecar into a single self-contained CommonJS file.
 *
 *   node build.mjs   ->   dist/bundle.cjs
 *
 * Everything (Pi SDK included) is bundled; the deployed payload is just
 * node.exe + bundle.cjs with no node_modules. The installed Pi SDK version is
 * baked in as __PI_VERSION__ for the hello handshake.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import * as path from "node:path";
import { build } from "esbuild";

const here = path.dirname(fileURLToPath(import.meta.url));

let piVersion = "unknown";
try {
  const pkgPath = path.join(
    here,
    "node_modules",
    "@earendil-works",
    "pi-coding-agent",
    "package.json",
  );
  piVersion = JSON.parse(readFileSync(pkgPath, "utf8")).version ?? "unknown";
} catch {
  console.warn("warning: could not read Pi SDK version (is npm install done?)");
}

await build({
  entryPoints: [path.join(here, "src", "main.ts")],
  outfile: path.join(here, "dist", "bundle.cjs"),
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "node20",
  external: [],
  // The Pi SDK is ESM-only and evaluates `fileURLToPath(import.meta.url)` at
  // module scope. esbuild's CJS output would otherwise emit `import_meta = {}`,
  // making `import.meta.url` undefined and killing the bundle at load with
  // ERR_INVALID_ARG_TYPE before a single byte reaches stdout. `define` values
  // must be entity names or literals, so the expression lives in the banner and
  // `define` only points at it.
  banner: {
    js: [
      "const __IMPORT_META_URL__ = require('url').pathToFileURL(__filename).href;",
      "const __IMPORT_META_RESOLVE__ = (specifier) =>",
      "  require('url').pathToFileURL(require.resolve(specifier)).href;",
    ].join("\n"),
  },
  define: {
    __PI_VERSION__: JSON.stringify(piVersion),
    "import.meta.url": "__IMPORT_META_URL__",
    "import.meta.resolve": "__IMPORT_META_RESOLVE__",
    "import.meta.dirname": "__dirname",
    "import.meta.filename": "__filename",
  },
  sourcemap: false,
  legalComments: "none",
  logLevel: "info",
});

console.log(`built dist/bundle.cjs (pi ${piVersion})`);
