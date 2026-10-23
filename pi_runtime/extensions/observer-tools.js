/**
 * Pi Observer tools for the paper-aligned experiment.
 *
 * Registers the four Observer-only tools:
 *   records_read / evidence_read / observer_report / infra_issue
 *
 * The Observer is an independent witness: it may read experiment records and
 * evidence, publish an advisory report and request repair of an environment
 * failure. It has no game client, shell, memory or web tools. Each call is
 * forwarded to the loopback bridge that the Pi Observer runner started
 * (HARNESS_PI_BRIDGE_URL + a one-shot token FD); the bridge owns
 * authentication, argument validation, permissions and the durable RPC writes,
 * so no runner token or harness path is exposed to the model.
 */

import fs from "node:fs";

import { Type } from "@earendil-works/pi-ai";

const BRIDGE_URL = process.env.HARNESS_PI_BRIDGE_URL || "";
// The bridge token arrives through an inherited one-shot pipe FD (never the
// environment). Read it once during extension load and close the FD so later
// model-directed shell commands cannot recover it from this process.
function readBridgeToken() {
	const fdValue = process.env.HARNESS_PI_BRIDGE_TOKEN_FD;
	if (!fdValue) {
		return "";
	}
	const fd = Number(fdValue);
	try {
		return fs.readFileSync(fd, "utf8").trim();
	} catch (_error) {
		return "";
	} finally {
		try {
			fs.closeSync(fd);
		} catch (_error) {
			// already closed
		}
		delete process.env.HARNESS_PI_BRIDGE_TOKEN_FD;
	}
}

const BRIDGE_TOKEN = readBridgeToken();
const string = Type.String();
const number = Type.Number();

const EVIDENCE_REF = Type.Union([
	Type.Number(),
	Type.String(),
	Type.Object({
		hash: Type.Optional(Type.String()),
		sha256: Type.Optional(Type.String()),
	}),
]);

const EVIDENCE_REFS = Type.Array(EVIDENCE_REF, { maxItems: 20 });
const FINDING_STATUS = Type.Union([
	Type.Literal("verified"),
	Type.Literal("player_claim"),
	Type.Literal("insufficient_evidence"),
	Type.Literal("contradicted"),
]);

const TOOL_SPECS = [
	{
		name: "records_read",
		label: "Records Read",
		description:
			"Read a page of the experiment records. Returns the events on the page and a cursor for the next page. Cite an event by its event_seq in evidence_refs.",
		parameters: Type.Object({
			after: { ...number, description: "Cursor to read after (default 0)." },
			limit: { ...number, description: "Maximum number of records (default 50)." },
		}),
	},
	{
		name: "evidence_read",
		label: "Evidence Read",
		description:
			"Read one artifact referenced by a record you already viewed. Cite the artifact by its SHA256 hash in evidence_refs.",
		parameters: Type.Object(
			{
				hash: { ...string, description: "SHA256 hex hash of a referenced artifact." },
				offset: { ...number, description: "Character offset (default 0)." },
				limit: { ...number, description: "Maximum characters to return (default 12000)." },
			},
			{ required: ["hash"] },
		),
	},
	{
		name: "observer_report",
		label: "Observer Report",
		description:
			"Publish an advisory Observer report. Check the players' claims against the records first. Include a progress summary, the verified events you relied on, infrastructure findings and your findings. Findings with status verified/player_claim/insufficient_evidence/contradicted must cite evidence_refs.",
		parameters: Type.Object(
			{
				progress_summary: {
					...string,
					description: "Short summary of progress for this review.",
				},
				verified_events: Type.Array(
					Type.Object(
						{
							claim: { ...string, description: "Verified event or claim." },
							evidence_refs: { ...EVIDENCE_REFS, description: "Records or artifacts supporting the claim." },
						},
						{ required: ["claim", "evidence_refs"] },
					),
					{ maxItems: 100 },
				),
				infrastructure_findings: Type.Array(string, { maxItems: 20 }),
				findings: Type.Array(
					Type.Object(
						{
							status: { ...FINDING_STATUS, description: "Evidence status." },
							claim: { ...string, description: "Claim being assessed." },
							evidence_refs: { ...EVIDENCE_REFS, description: "Records or artifacts supporting the assessment." },
						},
						{ required: ["status", "claim", "evidence_refs"] },
					),
					{ maxItems: 100 },
				),
			},
			{ required: ["findings"] },
		),
	},
	{
		name: "infra_issue",
		label: "Infrastructure Issue",
		description:
			"Report an environment failure to the supervisor: a broken tool, a runtime fault or a server fault. Never request changes to the players' strategy, game state or budget.",
		parameters: Type.Object(
			{
				kind: {
					...Type.Union([
						Type.Literal("reconnect"),
						Type.Literal("runtime_recovery"),
						Type.Literal("server_recovery"),
						Type.Literal("broken_tool"),
						Type.Literal("quota_ceiling"),
					]),
					description: "Environment failure category.",
				},
				reason: { ...string, description: "Short evidence-based reason." },
				evidence_refs: { ...EVIDENCE_REFS, description: "Records or artifacts supporting the request." },
			},
			{ required: ["kind", "reason", "evidence_refs"] },
		),
	},
];

function bridgeError(message) {
	return new Error(message || "observer tool is unavailable in this runner");
}

async function callBridge(toolName, args) {
	if (!BRIDGE_URL || !BRIDGE_TOKEN) {
		throw bridgeError("observer tools are not configured for this runner");
	}
	let response;
	try {
		response = await fetch(`${BRIDGE_URL}/tool`, {
			method: "POST",
			headers: {
				"content-type": "application/json",
				authorization: `Bearer ${BRIDGE_TOKEN}`,
			},
			body: JSON.stringify({ tool: toolName, arguments: args ?? {} }),
		});
	} catch (_error) {
		throw bridgeError("observer tool bridge is unreachable");
	}
	let payload = null;
	try {
		payload = await response.json();
	} catch (_error) {
		throw bridgeError("observer tool bridge returned an invalid response");
	}
	if (!response.ok || !payload || payload.ok !== true) {
		const message =
			payload && typeof payload.error === "string"
				? payload.error
				: "observer tool call failed";
		throw bridgeError(message);
	}
	return payload;
}

export default function observerToolsExtension(pi) {
	for (const spec of TOOL_SPECS) {
		pi.registerTool({
			name: spec.name,
			label: spec.label,
			description: spec.description,
			parameters: spec.parameters,
			async execute(_toolCallId, params) {
				const payload = await callBridge(spec.name, params ?? {});
				const text =
					typeof payload.content === "string"
						? payload.content
						: String(payload.content ?? "");
				return {
					content: [{ type: "text", text }],
					details:
						payload.details && typeof payload.details === "object"
							? payload.details
							: { tool: spec.name },
				};
			},
		});
	}
}
