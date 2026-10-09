"""Generate n8n/workflows/campaign-agent.json from readable sources.

The system prompt lives in system-prompt.md and the verification code in
verify_report.js so reviewers can read them without digging through JSON.
Run: uv run python n8n/build_workflow.py
"""

from __future__ import annotations

import json
import pathlib

HERE = pathlib.Path(__file__).parent
DEMO_CRED = {"id": "nitxDemoCred0001", "name": "Nitx platform (demo tenant)"}
DEEPSEEK_CRED = {"id": "deepseekCred0001", "name": "DeepSeek"}
PLATFORM = "http://platform:8080"

system_prompt = (HERE / "system-prompt.md").read_text().replace(
    "{{TODAY}}", "{{ $now.setZone('Asia/Riyadh').toFormat(\"cccc yyyy-MM-dd HH:mm\") }}"
)

PREPARE_JS = r"""
// Normalise the request. The tenant is NOT taken from the request: it is fixed by the
// platform credential attached to this workflow.
const body = $json.body ?? {};
const brief = String(body.brief ?? '').trim();
if (!brief) throw new Error('Send JSON like {"brief": "..."}');
if (brief.length > 4000) throw new Error('brief is longer than 4000 characters');
return [{ json: {
  brief,
  session_id: String(body.session_id ?? $execution.id),
  campaign_id: body.campaign_id ? String(body.campaign_id) : null,
  name: brief.slice(0, 80),
  started_at: new Date().toISOString(),
}}];
"""


def node(name, type_, version, params, pos, **extra):
    return {"parameters": params, "name": name, "type": type_, "typeVersion": version, "position": pos, **extra}


nodes = [
    node(
        "Brief in", "n8n-nodes-base.webhook", 2,
        {"httpMethod": "POST", "path": "campaign-agent", "responseMode": "responseNode", "options": {}},
        [0, 0], webhookId="5d1c2f4e-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
    ),
    node("Prepare", "n8n-nodes-base.code", 2, {"jsCode": PREPARE_JS.strip()}, [220, 0]),
    node(
        "Open campaign", "n8n-nodes-base.httpRequest", 4.2,
        {
            "method": "POST", "url": f"{PLATFORM}/v1/campaigns",
            "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
            "sendBody": True, "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({ campaign_id: $json.campaign_id, name: $json.name, brief: $json.brief }) }}",
            "options": {"timeout": 10000},
        },
        [440, 0], credentials={"httpHeaderAuth": DEMO_CRED},
    ),
    node(
        "Campaign Agent", "@n8n/n8n-nodes-langchain.agent", 3.1,
        {
            "promptType": "define",
            "text": "=campaign_id: {{ $json.campaign_id }}\n\nBrief from the user:\n<<<\n{{ $('Prepare').item.json.brief }}\n>>>",
            "options": {
                "systemMessage": "=" + system_prompt,
                "maxIterations": 40,
                "returnIntermediateSteps": True,
                "enableStreaming": False,
            },
        },
        [700, 0], onError="continueRegularOutput",
    ),
    node(
        "DeepSeek", "@n8n/n8n-nodes-langchain.lmChatDeepSeek", 1,
        {"model": "deepseek-chat", "options": {"temperature": 0.2, "maxRetries": 2, "timeout": 120000}},
        [560, 260], credentials={"deepSeekApi": DEEPSEEK_CRED},
    ),
    node(
        "Conversation memory", "@n8n/n8n-nodes-langchain.memoryBufferWindow", 1.3,
        {"sessionIdType": "customKey", "sessionKey": "={{ $('Prepare').item.json.session_id }}",
         "contextWindowLength": 20},
        [700, 260],
    ),
    node(
        "Poster tools (MCP)", "@n8n/n8n-nodes-langchain.mcpClientTool", 1.4,
        {"endpointUrl": "http://poster-mcp:8000/mcp", "serverTransport": "httpStreamable", "include": "all",
         "options": {"timeout": 60000}},
        [840, 260],
    ),
    node(
        "Campaign tools (MCP)", "@n8n/n8n-nodes-langchain.mcpClientTool", 1.4,
        {"endpointUrl": "http://campaign-mcp:8001/mcp", "serverTransport": "httpStreamable", "include": "all",
         "options": {"timeout": 60000}},
        [980, 260],
    ),
    node(
        "Read real state", "n8n-nodes-base.httpRequest", 4.2,
        {
            "method": "GET",
            "url": f"={PLATFORM}/v1/campaigns/{{{{ $('Open campaign').item.json.campaign_id }}}}",
            "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
            "options": {"timeout": 10000},
        },
        [1060, 0], credentials={"httpHeaderAuth": DEMO_CRED}, onError="continueRegularOutput",
    ),
    node("Verify report", "n8n-nodes-base.code", 2, {"jsCode": (HERE / "verify_report.js").read_text()}, [1280, 0]),
    node(
        "Reply", "n8n-nodes-base.respondToWebhook", 1.1,
        {"respondWith": "json", "responseBody": "={{ JSON.stringify($json) }}", "options": {}},
        [1500, 0],
    ),
]


def main_link(*names):
    return {"main": [[{"node": n, "type": "main", "index": 0}] for n in names]}


connections = {
    "Brief in": main_link("Prepare"),
    "Prepare": main_link("Open campaign"),
    "Open campaign": main_link("Campaign Agent"),
    "Campaign Agent": main_link("Read real state"),
    "Read real state": main_link("Verify report"),
    "Verify report": main_link("Reply"),
    "DeepSeek": {"ai_languageModel": [[{"node": "Campaign Agent", "type": "ai_languageModel", "index": 0}]]},
    "Conversation memory": {"ai_memory": [[{"node": "Campaign Agent", "type": "ai_memory", "index": 0}]]},
    "Poster tools (MCP)": {"ai_tool": [[{"node": "Campaign Agent", "type": "ai_tool", "index": 0}]]},
    "Campaign tools (MCP)": {"ai_tool": [[{"node": "Campaign Agent", "type": "ai_tool", "index": 0}]]},
}

workflow = {
    "id": "campaignAgent001",
    "name": "Nitx Campaign Agent",
    "active": False,
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1", "saveManualExecutions": True, "saveDataSuccessExecution": "all"},
    "pinData": {},
    "versionId": "8f0c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a5b",
}

out = HERE / "workflows" / "campaign-agent.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n")
print(f"wrote {out}")
