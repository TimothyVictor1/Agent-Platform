// Generates src/contracts/generated.ts from src/contracts/schema.json
// (written by scripts/gen_contracts.py). Run via `make contracts`.
// Usage: node scripts/gen-contracts.mjs [--schema path] [--out path]
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { compile } from "json-schema-to-typescript";

const here = path.dirname(fileURLToPath(import.meta.url));
const contractsDir = path.join(here, "..", "src", "contracts");

function arg(name, fallback) {
  const i = process.argv.indexOf(name);
  return i > -1 ? path.resolve(process.argv[i + 1]) : fallback;
}

const schemaPath = arg("--schema", path.join(contractsDir, "schema.json"));
const outPath = arg("--out", path.join(contractsDir, "generated.ts"));

const schema = JSON.parse(await readFile(schemaPath, "utf8"));

const banner = `/* eslint-disable */
/**
 * GENERATED FILE - DO NOT EDIT.
 * Source of truth: backend/src/agentplatform/contracts (Pydantic).
 * Regenerate with \`make contracts\`; CI fails if this file is stale.
 */`;

let ts = await compile(schema, "AgentPlatformContracts", {
  bannerComment: banner,
  additionalProperties: false,
  unreachableDefinitions: true,
  unknownAny: true,
  strictIndexSignatures: true,
  format: true,
  style: { singleQuote: false, semi: true, printWidth: 100 },
});

// The root schema is an empty object; drop its interface.
ts = ts.replace(/export interface AgentPlatformContracts \{\}\n?/, "");
// JsonValue is "any JSON value"; express it as unknown.
ts = ts.replace(/export interface JsonValue \{\}/, "export type JsonValue = unknown;");

await writeFile(outPath, ts);
console.log(`wrote ${path.relative(process.cwd(), outPath)}`);
