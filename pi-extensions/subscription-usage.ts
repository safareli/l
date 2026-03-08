import type { ExtensionAPI, ExtensionContext } from "@mariozechner/pi-coding-agent";
import { Text } from "@mariozechner/pi-tui";

const WIDGET_ID = "subscription-usage";
const CODEX_USAGE_URL = "https://chatgpt.com/backend-api/codex/usage";
const CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage";
const REQUEST_TIMEOUT_MS = 12_000;

type JsonObject = Record<string, unknown>;

type WindowMetric = {
	label: string;
	percent?: number;
	reset?: string;
};

type ProviderSummary = {
	provider: "codex" | "claude";
	ok: boolean;
	error?: string;
	windows: {
		fiveHour: WindowMetric;
		sevenDay: WindowMetric;
	};
};

function asObject(value: unknown): JsonObject | undefined {
	if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
	return value as JsonObject;
}

function asString(value: unknown): string | undefined {
	return typeof value === "string" ? value : undefined;
}

function asNumber(value: unknown): number | undefined {
	return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function compactError(text: string): string {
	return text.replace(/\s+/g, " ").trim().slice(0, 220);
}

function formatDuration(seconds: number | undefined): string | undefined {
	if (typeof seconds !== "number" || !Number.isFinite(seconds)) return undefined;

	const total = Math.max(0, Math.round(seconds));
	const days = Math.floor(total / 86_400);
	const hours = Math.floor((total % 86_400) / 3_600);
	const mins = Math.floor((total % 3_600) / 60);

	if (days > 0) return `${days}d${hours}h`;
	if (hours > 0) return `${hours}h${mins}m`;
	if (mins > 0) return `${mins}m`;
	return "<1m";
}

function formatDurationFromEpochSeconds(unixSeconds: number | undefined): string | undefined {
	if (typeof unixSeconds !== "number" || !Number.isFinite(unixSeconds)) return undefined;
	const delta = Math.round(unixSeconds - Date.now() / 1000);
	return formatDuration(delta);
}

function formatDurationFromIso(iso: string | undefined): string | undefined {
	if (!iso) return undefined;
	const targetMs = Date.parse(iso);
	if (!Number.isFinite(targetMs)) return undefined;
	const deltaSeconds = Math.round((targetMs - Date.now()) / 1000);
	return formatDuration(deltaSeconds);
}

function formatPercentDense(value: number | undefined): string {
	if (typeof value !== "number" || !Number.isFinite(value)) return "??%";
	const rounded = Math.max(0, Math.round(value));
	if (rounded >= 100) return `${rounded}%`;
	return `${String(rounded).padStart(2, "0")}%`;
}

function formatWindowLabel(limitSeconds: number | undefined, fallback: string): string {
	if (typeof limitSeconds !== "number" || !Number.isFinite(limitSeconds)) return fallback;
	if (limitSeconds === 18_000) return "5h";
	if (limitSeconds === 604_800) return "7d";
	if (limitSeconds % 86_400 === 0) return `${Math.round(limitSeconds / 86_400)}d`;
	if (limitSeconds % 3_600 === 0) return `${Math.round(limitSeconds / 3_600)}h`;
	return fallback;
}

function parseJwtAccountId(token: string): string | undefined {
	const parts = token.split(".");
	if (parts.length !== 3) return undefined;

	try {
		const normalized = parts[1].replace(/-/g, "+").replace(/_/g, "/");
		const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
		const payload = JSON.parse(Buffer.from(padded, "base64").toString("utf8")) as JsonObject;
		const auth = asObject(payload["https://api.openai.com/auth"]);
		return asString(auth?.chatgpt_account_id);
	} catch {
		return undefined;
	}
}

async function parseErrorResponse(response: Response): Promise<string> {
	const raw = await response.text();
	let message = compactError(raw || response.statusText || `HTTP ${response.status}`);

	try {
		const parsed = JSON.parse(raw) as JsonObject;
		const error = asObject(parsed.error);
		message = asString(error?.message) ?? asString(parsed.detail) ?? asString(parsed.message) ?? message;
	} catch {}

	return `HTTP ${response.status}: ${message}`;
}

async function fetchWithTimeout(url: string, init: RequestInit, timeoutMs = REQUEST_TIMEOUT_MS): Promise<Response> {
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), timeoutMs);

	try {
		return await fetch(url, { ...init, signal: controller.signal });
	} finally {
		clearTimeout(timer);
	}
}

async function getApiKeyForProvider(ctx: ExtensionContext, provider: string): Promise<string | undefined> {
	const registry = ctx.modelRegistry as {
		getApiKeyForProvider?: (providerName: string) => Promise<string | undefined>;
	};
	if (!registry.getApiKeyForProvider) return undefined;
	return registry.getApiKeyForProvider(provider);
}

function emptySummary(provider: "codex" | "claude", error?: string): ProviderSummary {
	return {
		provider,
		ok: false,
		error,
		windows: {
			fiveHour: { label: "5h" },
			sevenDay: { label: "7d" },
		},
	};
}

function codexMetric(window: JsonObject | undefined, fallbackLabel: string): WindowMetric {
	const percent = asNumber(window?.used_percent);
	const label = formatWindowLabel(asNumber(window?.limit_window_seconds), fallbackLabel);
	const reset = formatDuration(asNumber(window?.reset_after_seconds)) ?? formatDurationFromEpochSeconds(asNumber(window?.reset_at));
	return { label, percent, reset };
}

function claudeMetric(window: JsonObject | undefined, fallbackLabel: string): WindowMetric {
	const percent = asNumber(window?.utilization);
	const reset = formatDurationFromIso(asString(window?.resets_at));
	return { label: fallbackLabel, percent, reset };
}

async function fetchCodexUsage(ctx: ExtensionContext): Promise<ProviderSummary> {
	const token = await getApiKeyForProvider(ctx, "openai-codex");
	if (!token) return emptySummary("codex", "Not authenticated. Run /login openai-codex.");

	const accountId = parseJwtAccountId(token);
	if (!accountId) return emptySummary("codex", "Could not decode ChatGPT account id from OAuth token.");

	let response: Response;
	try {
		response = await fetchWithTimeout(CODEX_USAGE_URL, {
			method: "GET",
			headers: {
				Authorization: `Bearer ${token}`,
				"chatgpt-account-id": accountId,
				"OpenAI-Beta": "responses=experimental",
				originator: "pi",
				accept: "application/json",
				"content-type": "application/json",
			},
		});
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		return emptySummary("codex", `Request failed: ${message}`);
	}

	if (!response.ok) return emptySummary("codex", await parseErrorResponse(response));

	let data: JsonObject;
	try {
		data = (await response.json()) as JsonObject;
	} catch {
		return emptySummary("codex", "Invalid JSON response.");
	}

	const rate = asObject(data.rate_limit);
	const primary = codexMetric(asObject(rate?.primary_window), "5h");
	const secondary = codexMetric(asObject(rate?.secondary_window), "7d");

	return {
		provider: "codex",
		ok: true,
		windows: {
			fiveHour: primary,
			sevenDay: secondary,
		},
	};
}

async function fetchClaudeUsage(ctx: ExtensionContext): Promise<ProviderSummary> {
	const token = await getApiKeyForProvider(ctx, "anthropic");
	if (!token) return emptySummary("claude", "Not authenticated. Run /login anthropic.");

	let response: Response;
	try {
		response = await fetchWithTimeout(CLAUDE_USAGE_URL, {
			method: "GET",
			headers: {
				Authorization: `Bearer ${token}`,
				"anthropic-beta": "oauth-2025-04-20",
				accept: "application/json",
				"anthropic-dangerous-direct-browser-access": "true",
			},
		});
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		return emptySummary("claude", `Request failed: ${message}`);
	}

	if (!response.ok) return emptySummary("claude", await parseErrorResponse(response));

	let data: JsonObject;
	try {
		data = (await response.json()) as JsonObject;
	} catch {
		return emptySummary("claude", "Invalid JSON response.");
	}

	const errorObj = asObject(data.error);
	if (errorObj) {
		return emptySummary(
			"claude",
			asString(errorObj.message) ?? asString(errorObj.type) ?? "Unknown Anthropic API error.",
		);
	}

	return {
		provider: "claude",
		ok: true,
		windows: {
			fiveHour: claudeMetric(asObject(data.five_hour), "5h"),
			sevenDay: claudeMetric(asObject(data.seven_day), "7d"),
		},
	};
}

function stylePercent(percent: number | undefined, raw: string, theme?: { fg: (name: string, text: string) => string }): string {
	if (!theme || typeof percent !== "number" || !Number.isFinite(percent)) return raw;
	if (percent >= 80) return theme.fg("error", raw);
	if (percent >= 50) return theme.fg("warning", raw);
	return raw;
}

function renderWindow(
	metric: WindowMetric,
	theme?: { fg: (name: string, text: string) => string },
): string {
	const pctRaw = formatPercentDense(metric.percent);
	const pct = stylePercent(metric.percent, pctRaw, theme);
	const reset = metric.reset ?? "?";
	return `[${metric.label}: ${pct}${reset}]`;
}

function shortenProviderError(error?: string): string {
	const e = compactError(error ?? "unknown").toLowerCase();
	if (e.includes("not authenticated") || e.includes("token") || e.includes("oauth")) return "auth";
	if (e.includes("429") || e.includes("limit")) return "limit";
	if (e.includes("timeout") || e.includes("aborted")) return "timeout";
	return "err";
}

function buildCompactLine(
	codex: ProviderSummary,
	claude: ProviderSummary,
	theme?: { fg: (name: string, text: string) => string },
): string {
	const codexPart = codex.ok
		? `Codex ${renderWindow(codex.windows.fiveHour, theme)} ${renderWindow(codex.windows.sevenDay, theme)}`
		: `Codex [${theme ? theme.fg("error", shortenProviderError(codex.error)) : shortenProviderError(codex.error)}]`;

	const claudePart = claude.ok
		? `Claude: ${renderWindow(claude.windows.fiveHour, theme)} ${renderWindow(claude.windows.sevenDay, theme)}`
		: `Claude: [${theme ? theme.fg("error", shortenProviderError(claude.error)) : shortenProviderError(claude.error)}]`;

	return `${codexPart} | ${claudePart}`;
}

async function refreshUsage(ctx: ExtensionContext): Promise<void> {
	if (ctx.hasUI) ctx.ui.setStatus(WIDGET_ID, "Refreshing limits...");

	const [codex, claude] = await Promise.all([fetchCodexUsage(ctx), fetchClaudeUsage(ctx)]);
	const plainLine = buildCompactLine(codex, claude);

	if (ctx.hasUI) {
		ctx.ui.setWidget(
			WIDGET_ID,
			(_tui, theme) => new Text(buildCompactLine(codex, claude, theme), 0, 0),
			{ placement: "belowEditor" },
		);

		const failed = [codex, claude].filter((x) => !x.ok).length;
		ctx.ui.setStatus(
			WIDGET_ID,
			failed === 0
				? `Limits updated ${new Date().toLocaleTimeString()}`
				: `Limits updated (${failed} provider error${failed > 1 ? "s" : ""})`,
		);
	} else {
		console.log(plainLine);
	}
}

export default function (pi: ExtensionAPI) {
	pi.registerCommand("usage-limits", {
		description: "Show compact Codex/Claude limits (arg: clear)",
		handler: async (args, ctx) => {
			if (args?.trim().toLowerCase() === "clear") {
				if (ctx.hasUI) {
					ctx.ui.setWidget(WIDGET_ID, undefined);
					ctx.ui.setStatus(WIDGET_ID, undefined);
					ctx.ui.notify("Usage widget cleared.", "info");
				}
				return;
			}

			await refreshUsage(ctx);
		},
	});
}
