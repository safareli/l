#!/usr/bin/env bun
//
// yt-transcript.ts - Download YouTube transcript and format it
//
// Usage: bun run yt-transcript.ts <youtube_url> [output_dir] [--lang <code>]
//
// Outputs (in output_dir, default: /tmp/yt-transcript):
//   <video_id>-<video_title>_timestamped.txt - Transcript with timestamps
//   <video_id>-<video_title>_text.txt        - Plain text transcript
//   <video_id>-<video_title>.<lang>.srt      - Original SRT subtitle file
//

import { mkdir } from "node:fs/promises";
import { join } from "node:path";

// --- Constants ---

const MAX_TITLE_LENGTH = 80;
const YOUTUBE_URL_RE =
  /(?:youtube\.com\/(?:watch\?.*v=|embed\/|shorts\/)|youtu\.be\/)([\w-]{11})/;

// --- Helpers ---

async function which(bin: string): Promise<boolean> {
  const proc = Bun.spawn(["which", bin], {
    stdout: "ignore",
    stderr: "ignore",
  });
  return (await proc.exited) === 0;
}

async function run(cmd: string[], opts?: { quiet?: boolean }): Promise<string> {
  const proc = Bun.spawn(cmd, {
    stdout: "pipe",
    stderr: opts?.quiet ? "ignore" : "pipe",
  });

  if (opts?.quiet) {
    const stdout = await new Response(proc.stdout).text();
    await proc.exited; // ignore exit code in quiet mode
    return stdout.trim();
  }

  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr!).text(),
    proc.exited,
  ]);

  if (exitCode !== 0) {
    throw new Error(
      `Command failed (${exitCode}): ${cmd.join(" ")}\n${stderr}`,
    );
  }
  return stdout.trim();
}

function extractVideoId(url: string): string {
  const match = url.match(YOUTUBE_URL_RE);
  if (!match) throw new Error(`Could not extract video ID from: ${url}`);
  return match[1];
}

function sanitizeTitle(title: string): string {
  return (
    title
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "") // strip combining diacritics
      .replace(/[^\p{L}\p{N} _-]/gu, "") // keep letters, digits, space, _, -
      .replace(/ +/g, "_")
      .slice(0, MAX_TITLE_LENGTH) || "untitled"
  );
}

// --- SRT parsing ---

interface SrtEntry {
  startTs: string; // "HH:MM:SS"
  endTs: string; // "HH:MM:SS"
  startSec: number;
  endSec: number;
  text: string;
}

function tsToSeconds(ts: string): number {
  const [h, m, s] = ts.split(":").map(Number);
  return h * 3600 + m * 60 + s;
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
        startTs: tsMatch[1],
        endTs: tsMatch[2],
        startSec: tsToSeconds(tsMatch[1]),
        endSec: tsToSeconds(tsMatch[2]),
        text: textLines.join(" "),
      });
    }
  }

  return entries;
}

interface VideoInfo {
  title: string;
  channel: string;
  url: string;
}

function formatTimestamped(info: VideoInfo, entries: SrtEntry[]): string {
  const header = `# ${info.channel} :: ${info.title}\n# ${info.url}\n\n`;
  return (
    header + entries.map((e) => `${e.startTs}-${e.endTs} ${e.text}`).join("\n")
  );
}

function formatPlainText(info: VideoInfo, entries: SrtEntry[]): string {
  const header = `# ${info.channel} :: ${info.title}\n# ${info.url}\n\n`;
  const body = entries
    .map((e) => e.text)
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
  return header + body;
}

// --- Arg parsing ---

interface Args {
  videoUrl: string;
  outDir: string;
  lang: string;
}

function parseArgs(argv: string[]): Args | null {
  const positional: string[] = [];
  let lang = "en";

  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--lang" && i + 1 < argv.length) {
      lang = argv[++i];
    } else if (!argv[i].startsWith("--")) {
      positional.push(argv[i]);
    }
  }

  if (positional.length === 0) return null;

  return {
    videoUrl: positional[0],
    outDir: positional[1] ?? "/tmp/yt-transcript",
    lang,
  };
}

// --- Main ---

async function main() {
  // Check dependency
  if (!(await which("yt-dlp"))) {
    console.error("Error: yt-dlp is not installed or not on PATH.");
    console.error("Install it with: nix-shell -p yt-dlp");
    process.exit(1);
  }

  const args = parseArgs(Bun.argv.slice(2));

  if (!args) {
    console.error(
      "Usage: yt-transcript <youtube_url> [output_dir] [--lang <code>]",
    );
    console.error(
      "Example: yt-transcript 'https://www.youtube.com/watch?v=ABC123'",
    );
    console.error(
      "Example: yt-transcript 'https://www.youtube.com/watch?v=ABC123' ./out --lang fr",
    );
    process.exit(1);
  }

  const { videoUrl, outDir, lang } = args;

  await mkdir(outDir, { recursive: true });

  const videoId = extractVideoId(videoUrl);

  // Get video metadata (title + channel) in one call
  const metadata = await run(
    ["yt-dlp", "--print", "%(title)s\n%(channel)s", videoUrl],
    { quiet: true },
  );
  const [rawTitle, channel] = metadata.split("\n");
  const videoTitle = sanitizeTitle(rawTitle);
  const prefix = join(outDir, `${videoId}-${videoTitle}`);

  console.log(`=== Downloading transcript for: ${videoUrl} ===`);
  console.log(`Output prefix: ${prefix}\n`);

  // [1/3] Download subtitles
  console.log(`[1/3] Downloading subtitles (lang=${lang})...`);

  let dlStdout: string;
  try {
    dlStdout = await run([
      "yt-dlp",
      "--write-auto-sub",
      "--sub-lang",
      lang,
      "--sub-format",
      "srt",
      "--skip-download",
      "-o",
      prefix,
      videoUrl,
    ]);
  } catch (err) {
    console.error(`Error downloading subtitles: ${err}`);
    process.exit(1);
  }

  for (const line of dlStdout.split("\n")) {
    if (/Downloading|Writing|error/i.test(line)) console.log(`   ${line}`);
  }

  const srtFile = `${prefix}.${lang}.srt`;
  const srtBunFile = Bun.file(srtFile);
  if (!(await srtBunFile.exists())) {
    // Try to list available subtitle languages for a helpful error
    const listOut = await run(
      ["yt-dlp", "--list-subs", "--skip-download", videoUrl],
      { quiet: true },
    );
    console.error(`Error: No subtitles found for language '${lang}'.`);
    if (listOut) {
      console.error("\nAvailable subtitles:");
      console.error(listOut);
    }
    process.exit(1);
  }
  console.log(`   Downloaded: ${srtFile}`);

  // [2/3] Process transcript
  console.log("[2/3] Processing transcript...");
  const srtContent = await srtBunFile.text();
  const entries = parseSrt(srtContent);

  const timestampedPath = `${prefix}_timestamped.txt`;
  const textPath = `${prefix}_text.txt`;

  const videoInfo: VideoInfo = { title: rawTitle, channel, url: videoUrl };
  const timestampedContent = formatTimestamped(videoInfo, entries);
  const plainTextContent = formatPlainText(videoInfo, entries);

  await Promise.all([
    Bun.write(timestampedPath, timestampedContent),
    Bun.write(textPath, plainTextContent),
  ]);

  console.log(`   Timestamped: ${timestampedPath} (${entries.length} lines)`);
  console.log(`   Plain text:  ${textPath} (${plainTextContent.length} chars)`);

  // [3/3] Done
  console.log(`\n[3/3] Done! Output files:`);
  console.log(`   ${srtFile}  - Original SRT subtitles`);
  console.log(`   ${timestampedPath}  - Transcript with timestamps`);
  console.log(`   ${textPath}  - Plain text transcript`);
}

main().catch((err) => {
  console.error(`Fatal: ${err.message ?? err}`);
  process.exit(1);
});
