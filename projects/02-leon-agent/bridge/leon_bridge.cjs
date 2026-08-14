"use strict";

const path = require("node:path");

function readStdin() {
    return new Promise((resolve, reject) => {
        let body = "";
        process.stdin.setEncoding("utf8");
        process.stdin.on("data", (chunk) => { body += chunk; });
        process.stdin.on("end", () => {
            try { resolve(JSON.parse(body || "{}")); }
            catch (error) { reject(new Error("Invalid bridge input JSON: " + error.message)); }
        });
        process.stdin.on("error", reject);
    });
}

async function main() {
    const pluginDir = path.resolve(process.argv[2] || "");
    require(path.join(pluginDir, "src", "executor-assets.js"));
    const core = require(path.join(pluginDir, "src", "executor-core.js"));
    const assets = globalThis.LeonImageExecutorAssets;
    const input = await readStdin();

    if (input.action === "list_modes") {
        const modes = Object.values(assets.modes || {}).map((mode) => ({
            id: String(mode.id || ""),
            family: String(mode.family || ""),
            template_name: String(mode.templateName || ""),
        }));
        process.stdout.write(JSON.stringify({ modes }));
        return;
    }
    if (input.action === "inspect_environment") {
        const report = core.inspectEnvironment(
            assets,
            input.object_info || {},
            input.lora_names || [],
        );
        process.stdout.write(JSON.stringify(report));
        return;
    }
    if (input.action === "build_request") {
        process.stdout.write(JSON.stringify(core.buildRequest(assets, input.options || {})));
        return;
    }
    throw new Error("Unknown bridge action: " + String(input.action || ""));
}

main().catch((error) => {
    process.stderr.write(String(error && error.message || error));
    process.exitCode = 1;
});
