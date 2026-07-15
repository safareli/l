/**
 * Direnv Bash Hook
 *
 * Wraps all bash commands with `direnv export bash` so that
 * environment variables from .envrc are always available.
 *
 * This avoids the common issue where pi's bash tool doesn't
 * load direnv automatically.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { createBashTool } from "@mariozechner/pi-coding-agent";

const spawnHook = ({ command, cwd, env }: { command: string; cwd: string; env?: Record<string, string> }) => ({
	command: `eval "$(direnv export bash 2>/dev/null)"\n${command}`,
	cwd,
	env,
});

const toolCache = new Map<string, ReturnType<typeof createBashTool>>();

function getOrCreateTool(cwd: string) {
	let tool = toolCache.get(cwd);
	if (!tool) {
		tool = createBashTool(cwd, { spawnHook });
		toolCache.set(cwd, tool);
	}
	return tool;
}

export default function (pi: ExtensionAPI) {
	// Register with a placeholder cwd for the schema/description.
	// The actual cwd comes from ctx at execution time.
	const placeholder = createBashTool(process.cwd(), { spawnHook });

	pi.registerTool({
		...placeholder,
		execute: async (id, params, signal, onUpdate, ctx) => {
			return getOrCreateTool(ctx.cwd).execute(id, params, signal, onUpdate);
		},
	});
}
