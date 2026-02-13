#!/usr/bin/env bun
//
// yt_transcript.ts - Download YouTube transcript and format it
//
// Usage: bun run yt_transcript.ts <youtube_url> [output_dir]
//
// Outputs (in output_dir, default: /tmp/yt-transcript):
//   <video_id>-<video_title>_timestamped.txt - Transcript with timestamps
//   <video_id>-<video_title>_text.txt        - Plain text transcript
//   <video_id>-<video_title>.en.srt          - Original SRT subtitle file
//

import { mkdir } from "node:fs/promises";
import { join } from "node:path";

// --- Helpers ---

async function run(cmd: string[], opts?: { quiet?: boolean }): Promise<string> {
  const proc = Bun.spawn(cmd, {
    stdout: "pipe",
    stderr: opts?.quiet ? "ignore" : "pipe",
  });
  const stdout = await new Response(proc.stdout).text();
  const exitCode = await proc.exited;
  if (exitCode !== 0 && !opts?.quiet) {
    const stderr = proc.stderr ? await new Response(proc.stderr).text() : "";
    throw new Error(
      `Command failed (${exitCode}): ${cmd.join(" ")}\n${stderr}`,
    );
  }
  return stdout.trim();
}

function extractVideoId(url: string): string {
  const match = url.match(/(?:v=|\/)([\w-]{11})/);
  if (!match) throw new Error(`Could not extract video ID from: ${url}`);
  return match[1];
}

function sanitizeTitle(title: string): string {
  return title
    .replace(/[^\w -]/g, "")
    .replace(/ /g, "_")
    .slice(0, 80);
}

// --- SRT parsing ---

interface SrtEntry {
  start: string;
  end: string;
  text: string;
}

function parseSrt(content: string): SrtEntry[] {
  const entries: SrtEntry[] = [];
  const blocks = content.trim().split(/\n\n+/);

  for (const block of blocks) {
    const lines = block.trim().split("\n");
    if (lines.length < 2) continue;

    let tsLine: string | null = null;
    const textLines: string[] = [];

    for (const line of lines) {
      if (line.includes("-->")) {
        tsLine = line.trim();
      } else if (!/^\d+$/.test(line.trim())) {
        textLines.push(line.trim());
      }
    }

    if (!tsLine || textLines.length === 0) continue;

    const tsMatch = tsLine.match(
      /(\d{2}:\d{2}:\d{2}),\d+\s*-->\s*(\d{2}:\d{2}:\d{2}),\d+/,
    );
    if (tsMatch) {
      entries.push({
        start: tsMatch[1],
        end: tsMatch[2],
        text: textLines.join(" "),
      });
    }
  }

  return entries;
}

function formatTimestamped(entries: SrtEntry[]): string {
  return entries.map((e) => `${e.start}-${e.end} ${e.text}`).join("\n");
}

function formatPlainText(entries: SrtEntry[]): string {
  return entries
    .map((e) => e.text)
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

// --- Main ---

const [videoUrl, outDirArg] = Bun.argv.slice(2);

if (!videoUrl) {
  console.error("Usage: bun run yt_transcript.ts <youtube_url> [output_dir]");
  console.error(
    "Example: bun run yt_transcript.ts 'https://www.youtube.com/watch?v=ABC123'",
  );
  console.error(
    "Example: bun run yt_transcript.ts 'https://www.youtube.com/watch?v=ABC123' ./my_output",
  );
  process.exit(1);
}

const outDir = outDirArg ?? `/tmp/yt-transcript`;
await mkdir(outDir, { recursive: true });

const videoId = extractVideoId(videoUrl);

// Get video title
const rawTitle = await run(["yt-dlp", "--get-title", videoUrl], {
  quiet: true,
});
const videoTitle = sanitizeTitle(rawTitle);
const prefix = join(outDir, `${videoId}-${videoTitle}`);

console.log(`=== Downloading transcript for: ${videoUrl} ===`);
console.log(`Output prefix: ${prefix}\n`);

// [1/3] Download subtitles
console.log("[1/3] Downloading subtitles...");
const dlProc = Bun.spawn(
  [
    "yt-dlp",
    "--write-auto-sub",
    "--sub-lang",
    "en",
    "--sub-format",
    "srt",
    "--skip-download",
    "-o",
    prefix,
    videoUrl,
  ],
  { stdout: "pipe", stderr: "pipe" },
);
const dlOut = await new Response(dlProc.stdout).text();
for (const line of dlOut.split("\n")) {
  if (/Downloading|Writing|error/i.test(line)) console.log(line);
}
await dlProc.exited;

const srtFile = `${prefix}.en.srt`;
const srtBunFile = Bun.file(srtFile);
if (!(await srtBunFile.exists())) {
  console.error("Error: Could not download subtitles");
  process.exit(1);
}
console.log(`   Downloaded: ${srtFile}`);

// [2/3] Process transcript
console.log("[2/3] Processing transcript...");
const srtContent = await srtBunFile.text();
const entries = parseSrt(srtContent);

const timestampedPath = `${prefix}_timestamped.txt`;
const textPath = `${prefix}_text.txt`;

const timestampedContent = formatTimestamped(entries);
const plainTextContent = formatPlainText(entries);

await Bun.write(timestampedPath, timestampedContent);
await Bun.write(textPath, plainTextContent);

console.log(`   Timestamped: ${timestampedPath} (${entries.length} lines)`);
console.log(`   Plain text:  ${textPath} (${plainTextContent.length} chars)`);

// [3/3] Done
console.log(`\n[3/3] Done! Output files:`);
console.log(`   ${srtFile}  - Original SRT subtitles`);
console.log(`   ${timestampedPath}  - Transcript with timestamps`);
console.log(`   ${textPath}  - Plain text transcript`);
