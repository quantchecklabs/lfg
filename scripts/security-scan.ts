#!/usr/bin/env bun
// Daily security sweep CLI — thin wrapper over the codified probe battery in
// src/agents/collectors/security.ts so there is ONE source of truth for the
// host + supply-chain checks. The `security-audit` agent shells out to this:
//
//   bun run /home/dev/repos/lfg/scripts/security-scan.ts
//
// It prints host probes (access / accounts / network / persistence / rootkit /
// integrity) plus a supply-chain audit of the target repo, then exits. All
// probes are read-only and hardcoded in the collector — this script adds no new
// commands, it only renders the report.
//
// Repo selection for the supply-chain section (first match wins):
//   --repo=<path>  CLI flag
//   $LFG_SECURITY_REPO / $LFG_REPO  env
//   /home/dev/repos/vibes  (default — the app fleet we ship)
//   process.cwd()  (last resort)

import { existsSync } from "node:fs";
import { collectSecurityScan } from "../src/agents/collectors/security.ts";

function resolveRepo(): string {
  const flag = process.argv
    .slice(2)
    .find((a) => a.startsWith("--repo="))
    ?.slice("--repo=".length);
  if (flag) return flag;
  if (process.env.LFG_SECURITY_REPO) return process.env.LFG_SECURITY_REPO;
  if (process.env.LFG_REPO) return process.env.LFG_REPO;
  const defaultVibes = "/home/dev/repos/vibes";
  if (existsSync(defaultVibes)) return defaultVibes;
  return process.cwd();
}

const sectionsArg = process.argv
  .slice(2)
  .find((a) => a.startsWith("--sections="))
  ?.slice("--sections=".length);
const sections = sectionsArg
  ? sectionsArg.split(",").map((s) => s.trim()).filter(Boolean)
  : undefined;

const result = await collectSecurityScan({
  kind: "security_scan",
  repo: resolveRepo(),
  ...(sections ? { sections } : {}),
});

console.log(`# ${result.title}\n`);
console.log(result.body);
if (result.warning) console.error(`\n[warning] ${result.warning}`);
process.exit(result.ok ? 0 : 1);
