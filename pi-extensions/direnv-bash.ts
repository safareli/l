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

export default function (pi: ExtensionAPI) {
	const cwd = process.cwd();

	const bashTool = createBashTool(cwd, {
		spawnHook: ({ command, cwd, env }) => ({
			command: `eval "$(direnv export bash 2>/dev/null)"\n${command}`,
			cwd,
			env,
		}),
	});

	pi.registerTool({
		...bashTool,
		execute: async (id, params, signal, onUpdate, _ctx) => {
			return bashTool.execute(id, params, signal, onUpdate);
		},
	});
}
