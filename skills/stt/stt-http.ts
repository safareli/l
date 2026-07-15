#!/usr/bin/env bun

import { mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { extname, join } from "node:path";

type Lang = "auto" | "en" | "ka";

class HttpError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

const DEFAULT_HOST = process.env.STT_HTTP_HOST ?? "0.0.0.0";
const DEFAULT_PORT = Number(process.env.STT_HTTP_PORT ?? "6770");
const DEFAULT_TMPDIR = process.env.STT_HTTP_TMPDIR ?? "/tmp/stt-http";
const DEFAULT_MAX_BYTES = Number(process.env.STT_HTTP_MAX_BYTES ?? String(50 * 1024 * 1024));
const DEFAULT_STT_BIN = process.env.STT_BIN ?? `${process.env.HOME ?? ""}/.local/bin/stt`;

const CORS_HEADERS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const MIME_EXT: Record<string, string> = {
  "audio/wav": ".wav",
  "audio/x-wav": ".wav",
  "audio/mpeg": ".mp3",
  "audio/mp3": ".mp3",
  "audio/ogg": ".ogg",
  "audio/webm": ".webm",
  "audio/mp4": ".m4a",
  "audio/x-m4a": ".m4a",
  "audio/flac": ".flac",
  "audio/x-flac": ".flac",
  "audio/aac": ".aac",
  "application/ogg": ".ogg",
  "application/octet-stream": ".bin",
};

interface Config {
  host: string;
  port: number;
  tmpDir: string;
  maxBytes: number;
  sttBin: string;
}

interface ParseResult {
  inputArg: string;
  lang: Lang;
  whisper: boolean;
  timestamps: boolean;
}

function usage(): string {
  return [
    "stt-http - HTTP wrapper for the stt CLI",
    "",
    "Usage:",
    "  bun run skills/stt/stt-http.ts [--host 0.0.0.0] [--port 6770] [--tmpdir /tmp/stt-http] [--max-bytes 52428800] [--stt-bin /path/to/stt]",
    "",
    "Endpoints:",
    "  GET  /health",
    "  POST /transcribe",
    "",
    "POST /transcribe accepts:",
    "  1) multipart/form-data with file field 'audio' (or any file field)",
    "     optional fields: lang=auto|en|ka, whisper=true|false, timestamps=true|false",
    "  2) raw audio bytes in request body (query params: lang/whisper/timestamps/filename)",
    "  3) application/json body with { url, lang?, whisper?, timestamps? }",
  ].join("\n");
}

function parseArgs(argv: string[]): Config {
  let host = DEFAULT_HOST;
  let port = DEFAULT_PORT;
  let tmpDir = DEFAULT_TMPDIR;
  let maxBytes = DEFAULT_MAX_BYTES;
  let sttBin = DEFAULT_STT_BIN;

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];

    if (arg === "--help" || arg === "-h") {
      console.log(usage());
      process.exit(0);
    }

    if (arg === "--host" && argv[i + 1]) {
      host = argv[++i];
      continue;
    }

    if (arg === "--port" && argv[i + 1]) {
      port = Number(argv[++i]);
      continue;
    }

    if (arg === "--tmpdir" && argv[i + 1]) {
      tmpDir = argv[++i];
      continue;
    }

    if (arg === "--max-bytes" && argv[i + 1]) {
      maxBytes = Number(argv[++i]);
      continue;
    }

    if (arg === "--stt-bin" && argv[i + 1]) {
      sttBin = argv[++i];
      continue;
    }

    throw new Error(`Unknown argument: ${arg}`);
  }

  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(`Invalid --port value: ${port}`);
  }

  if (!Number.isFinite(maxBytes) || maxBytes <= 0) {
    throw new Error(`Invalid --max-bytes value: ${maxBytes}`);
  }

  return { host, port, tmpDir, maxBytes, sttBin };
}

function jsonResponse(status: number, payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      ...CORS_HEADERS,
      "Content-Type": "application/json; charset=utf-8",
    },
  });
}

function textResponse(status: number, text: string): Response {
  return new Response(text, {
    status,
    headers: {
      ...CORS_HEADERS,
      "Content-Type": "text/plain; charset=utf-8",
    },
  });
}

function parseBoolean(value: unknown): boolean {
  if (typeof value === "boolean") {
    return value;
  }

  if (typeof value !== "string") {
    return false;
  }

  const normalized = value.trim().toLowerCase();
  return normalized === "1" || normalized === "true" || normalized === "yes" || normalized === "on";
}

function parseLang(value: unknown): Lang {
  if (value == null || value === "") {
    return "auto";
  }

  if (typeof value !== "string") {
    throw new HttpError(400, "lang must be a string (auto|en|ka)");
  }

  const normalized = value.trim().toLowerCase();
  if (normalized === "auto" || normalized === "en" || normalized === "ka") {
    return normalized;
  }

  throw new HttpError(400, `Invalid lang '${value}'. Allowed: auto, en, ka`);
}

function pickExtension(fileName: string | null, contentType: string | null): string {
  if (fileName) {
    const ext = extname(fileName).toLowerCase();
    if (ext && ext.length <= 10) {
      return ext;
    }
  }

  if (contentType) {
    const mime = contentType.split(";")[0].trim().toLowerCase();
    if (MIME_EXT[mime]) {
      return MIME_EXT[mime];
    }
  }

  return ".bin";
}

function enforceContentLength(request: Request, maxBytes: number): void {
  const value = request.headers.get("content-length");
  if (!value) {
    return;
  }

  const contentLength = Number(value);
  if (Number.isFinite(contentLength) && contentLength > maxBytes) {
    throw new HttpError(413, `Payload too large (${contentLength} bytes > ${maxBytes} bytes)`);
  }
}

async function parseInput(request: Request, requestDir: string, maxBytes: number): Promise<ParseResult> {
  enforceContentLength(request, maxBytes);

  const url = new URL(request.url);
  const contentType = (request.headers.get("content-type") ?? "").toLowerCase();

  if (contentType.includes("multipart/form-data")) {
    const form = await request.formData();

    const lang = parseLang(form.get("lang"));
    const whisper = parseBoolean(form.get("whisper"));
    const timestamps = parseBoolean(form.get("timestamps"));

    const remoteUrl = form.get("url");
    if (typeof remoteUrl === "string" && remoteUrl.trim() !== "") {
      return {
        inputArg: remoteUrl.trim(),
        lang,
        whisper,
        timestamps,
      };
    }

    const explicitAudio = form.get("audio") ?? form.get("file");
    let file: File | null = explicitAudio instanceof File ? explicitAudio : null;

    if (!file) {
      for (const value of form.values()) {
        if (value instanceof File) {
          file = value;
          break;
        }
      }
    }

    if (!file) {
      throw new HttpError(400, "Missing audio upload. Use multipart/form-data with 'audio' file field.");
    }

    if (file.size === 0) {
      throw new HttpError(400, "Uploaded audio file is empty");
    }

    if (file.size > maxBytes) {
      throw new HttpError(413, `Uploaded audio file is too large (${file.size} bytes > ${maxBytes} bytes)`);
    }

    const extension = pickExtension(file.name, file.type || null);
    const localPath = join(requestDir, `upload${extension}`);
    const buffer = new Uint8Array(await file.arrayBuffer());
    await writeFile(localPath, buffer);

    return {
      inputArg: localPath,
      lang,
      whisper,
      timestamps,
    };
  }

  if (contentType.includes("application/json")) {
    let payload: unknown;
    try {
      payload = await request.json();
    } catch {
      throw new HttpError(400, "Invalid JSON payload");
    }

    if (typeof payload !== "object" || payload == null) {
      throw new HttpError(400, "JSON payload must be an object");
    }

    const body = payload as Record<string, unknown>;
    const remoteUrl = typeof body.url === "string" ? body.url.trim() : "";
    if (!remoteUrl) {
      throw new HttpError(400, "JSON payload must include non-empty 'url'");
    }

    return {
      inputArg: remoteUrl,
      lang: parseLang(body.lang),
      whisper: parseBoolean(body.whisper),
      timestamps: parseBoolean(body.timestamps),
    };
  }

  const lang = parseLang(url.searchParams.get("lang"));
  const whisper = parseBoolean(url.searchParams.get("whisper"));
  const timestamps = parseBoolean(url.searchParams.get("timestamps"));

  const body = new Uint8Array(await request.arrayBuffer());
  if (body.byteLength === 0) {
    throw new HttpError(400, "Request body is empty");
  }

  if (body.byteLength > maxBytes) {
    throw new HttpError(413, `Uploaded audio file is too large (${body.byteLength} bytes > ${maxBytes} bytes)`);
  }

  const extension = pickExtension(url.searchParams.get("filename"), contentType || null);
  const localPath = join(requestDir, `upload${extension}`);
  await writeFile(localPath, body);

  return {
    inputArg: localPath,
    lang,
    whisper,
    timestamps,
  };
}

async function runStt(
  sttBin: string,
  inputArg: string,
  requestDir: string,
  lang: Lang,
  whisper: boolean,
  timestamps: boolean,
): Promise<{ transcript: string; stderr: string }> {
  const args = [
    sttBin,
    inputArg,
    "--outdir",
    requestDir,
    "--lang",
    lang,
  ];

  if (whisper) {
    args.push("--whisper");
  }

  if (timestamps) {
    args.push("--timestamps");
  }

  const proc = Bun.spawn(args, {
    stdout: "pipe",
    stderr: "pipe",
    env: {
      ...process.env,
      STT_HTTP_REQUEST_DIR: requestDir,
    },
  });

  const [exitCode, stdout, stderr] = await Promise.all([
    proc.exited,
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
  ]);

  if (exitCode !== 0) {
    throw new HttpError(
      500,
      `stt failed (exit ${exitCode})\n${(stderr || stdout || "no output").trim()}`,
    );
  }

  const files = (await readdir(requestDir)).filter((name) => name.endsWith(".txt"));
  if (files.length === 0) {
    const fallback = stdout.trim();
    if (!fallback) {
      throw new HttpError(500, `stt finished without transcript output\n${stderr.trim()}`);
    }
    return { transcript: fallback, stderr };
  }

  files.sort();
  const transcriptPath = join(requestDir, files[files.length - 1]);
  const transcript = (await readFile(transcriptPath, "utf8")).trim();

  if (!transcript) {
    throw new HttpError(500, `stt produced an empty transcript\n${stderr.trim()}`);
  }

  return { transcript, stderr };
}

const config = parseArgs(process.argv.slice(2));
await mkdir(config.tmpDir, { recursive: true });

const server = Bun.serve({
  hostname: config.host,
  port: config.port,
  idleTimeout: 120,
  async fetch(request): Promise<Response> {
    try {
      if (request.method === "OPTIONS") {
        return new Response(null, {
          status: 204,
          headers: CORS_HEADERS,
        });
      }

      const url = new URL(request.url);

      if (request.method === "GET" && url.pathname === "/health") {
        return jsonResponse(200, {
          ok: true,
          service: "stt-http",
          maxBytes: config.maxBytes,
        });
      }

      if (request.method === "GET" && url.pathname === "/") {
        return textResponse(200, "stt-http is running. Use POST /transcribe with an audio file.");
      }

      if (url.pathname !== "/transcribe") {
        return textResponse(404, "Not Found");
      }

      if (request.method !== "POST") {
        return textResponse(405, "Method Not Allowed. Use POST /transcribe");
      }

      const start = Date.now();
      const requestId = randomUUID();
      const requestDir = join(config.tmpDir, requestId);
      await mkdir(requestDir, { recursive: true });

      try {
        const input = await parseInput(request, requestDir, config.maxBytes);
        const { transcript, stderr } = await runStt(
          config.sttBin,
          input.inputArg,
          requestDir,
          input.lang,
          input.whisper,
          input.timestamps,
        );

        const tookMs = Date.now() - start;
        const backend = input.whisper ? "whisper" : "nemo";

        console.log(
          `[${new Date().toISOString()}] request=${requestId} backend=${backend} lang=${input.lang} took=${tookMs}ms`,
        );

        if (stderr.trim()) {
          console.log(`[${new Date().toISOString()}] request=${requestId} stt-stderr:\n${stderr.trim()}`);
        }

        return textResponse(200, transcript + "\n");
      } finally {
        await rm(requestDir, { recursive: true, force: true });
      }
    } catch (error) {
      if (error instanceof HttpError) {
        return textResponse(error.status, error.message + "\n");
      }

      const message = error instanceof Error ? error.message : String(error);
      console.error(`[${new Date().toISOString()}] unexpected-error: ${message}`);
      return textResponse(500, `Internal server error\n${message}\n`);
    }
  },
});

console.log(
  `stt-http listening on http://${server.hostname}:${server.port} (tmpDir=${config.tmpDir}, maxBytes=${config.maxBytes})`,
);
