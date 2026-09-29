// Generic MCP-stdio client benchmark driver.
//
// Spawns the target MCP server as a child, performs the standard MCP
// handshake (initialize → notifications/initialized), then fires N
// `tools/call` requests for a given tool, cycling through one or more URLs.
// Identical client code for every implementation under test.
//
// Output (one JSON line on stdout):
//   cold:  { ms, ok, bytes }                     spawn → handshake → first result
//   e2e:   { samples, ok, bytes, rssKib }        N sequential calls in one process
//
//   ms/samples  wall-clock per call, measured here (client side)
//   ok          false if the server answered with a JSON-RPC error or isError:true
//   bytes       UTF-8 bytes of the text content returned to the agent
//   rssKib      child's peak RSS (/proc/<pid>/status VmHWM) after the run
//
// Usage:
//   node drive.js <cold|e2e> <iters> <toolName> <url[,url...]>
//                 [--arg-name <name>] [--extra '<json>'] [--timeout-ms N] -- <child argv...>

import { spawn } from "node:child_process";
import { performance } from "node:perf_hooks";
import * as fs from "node:fs";

const args = process.argv.slice(2);
const dashIdx = args.indexOf("--");
if (dashIdx < 0) {
  console.error("usage: drive.js <cold|e2e> <iters> <toolName> <url[,url...]> [--arg-name n] [--extra json] [--timeout-ms N] -- <child argv...>");
  process.exit(2);
}
const head = args.slice(0, dashIdx);
const [mode, itersStr, toolName, urlList] = head.slice(0, 4);
const urls = urlList.split(",");
let urlArgName = "url";
let extra = {};
let timeoutMs = 120000;
for (let i = 4; i < head.length; i++) {
  if (head[i] === "--arg-name") urlArgName = head[++i];
  else if (head[i] === "--extra") extra = JSON.parse(head[++i]);
  else if (head[i] === "--timeout-ms") timeoutMs = parseInt(head[++i], 10);
}
const childArgv = args.slice(dashIdx + 1);
const iters = parseInt(itersStr, 10);

function readChildVmHwmKib(pid) {
  try {
    const m = fs.readFileSync(`/proc/${pid}/status`, "utf8").match(/^VmHWM:\s+(\d+)/m);
    return m ? parseInt(m[1], 10) : 0;
  } catch {
    return 0;
  }
}

function startServer() {
  const child = spawn(childArgv[0], childArgv.slice(1), { stdio: ["pipe", "pipe", "pipe"] });
  let buf = "";
  const pending = new Map();
  child.stdout.on("data", (chunk) => {
    buf += chunk.toString();
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 1);
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); } catch { continue; } // non-JSON log noise
      if (msg.id !== undefined && pending.has(msg.id)) {
        pending.get(msg.id)(msg);
        pending.delete(msg.id);
      }
    }
  });
  child.stderr.on("data", () => {}); // discard server logs
  let nextId = 0;
  return {
    child,
    notify(method) {
      child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method }) + "\n");
    },
    request(method, params) {
      const id = ++nextId;
      return new Promise((resolve, reject) => {
        const t = setTimeout(() => reject(new Error(`${method} timed out after ${timeoutMs} ms`)), timeoutMs);
        pending.set(id, (msg) => { clearTimeout(t); resolve(msg); });
        child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
      });
    },
  };
}

async function handshake(srv) {
  const r = await srv.request("initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "bench-driver", version: "0.1.0" },
  });
  if (!r.result) throw new Error("initialize failed: " + JSON.stringify(r));
  srv.notify("notifications/initialized");
}

async function callTool(srv, i) {
  const resp = await srv.request("tools/call", {
    name: toolName,
    arguments: { [urlArgName]: urls[i % urls.length], ...extra },
  });
  const content = resp.result?.content ?? [];
  const text = content.filter((c) => c.type === "text").map((c) => c.text).join("");
  const ok = !resp.error && !resp.result?.isError && text.length > 0;
  return { ok, bytes: Buffer.byteLength(text, "utf8") };
}

async function runCold() {
  const t0 = performance.now();
  const srv = startServer();
  await handshake(srv);
  const r = await callTool(srv, 0);
  const ms = performance.now() - t0;
  srv.child.kill();
  console.log(JSON.stringify({ mode: "cold", ms, ok: r.ok, bytes: r.bytes }));
}

async function runE2E() {
  const srv = startServer();
  await handshake(srv);
  const samples = [], ok = [], bytes = [];
  for (let i = 0; i < iters; i++) {
    const t0 = performance.now();
    const r = await callTool(srv, i);
    samples.push(performance.now() - t0);
    ok.push(r.ok);
    bytes.push(r.bytes);
  }
  await new Promise((r) => setTimeout(r, 250)); // let RSS settle
  const rssKib = readChildVmHwmKib(srv.child.pid);
  srv.child.kill();
  console.log(JSON.stringify({ mode: "e2e", iters, samples, ok, bytes, rssKib }));
}

(async () => {
  if (mode === "cold") await runCold();
  else if (mode === "e2e") await runE2E();
  else { console.error("unknown mode " + mode); process.exit(2); }
  process.exit(0);
})().catch((e) => { console.error(e.message ?? e); process.exit(1); });
