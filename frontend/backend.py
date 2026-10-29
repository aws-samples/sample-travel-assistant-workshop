#!/usr/bin/env python3
"""
FastAPI backend that proxies requests to the supervisor agent with SSE streaming.
Reads supervisor_agent_arn from config.json.

Usage:
    pip install fastapi uvicorn
    python backend.py

Security: this server has no authentication and invokes the agent with the
operator's AWS credentials, so it listens on loopback (127.0.0.1) only. To use
it from another machine, use an SSH tunnel instead of widening the bind:
    ssh -L 8000:localhost:8000 <user>@<host>
"""

import boto3
import json
import os
from botocore.config import Config
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(os.path.dirname(SCRIPT_DIR), "config.json")

# Loopback only. Not configurable on purpose: there is no auth in front of /api/chat.
HOST = "127.0.0.1"
PORT = 8000

# Only the React dev server (`npm start`) needs CORS; the production build is same-origin.
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if o.strip()
]
# Reject requests whose Host header isn't local, which blocks DNS-rebinding attacks
# where a malicious web page resolves its own domain to 127.0.0.1.
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

with open(CONFIG_PATH) as f:
    config = json.load(f)

AGENT_ARN = config["supervisor_agent_arn"]
client = boto3.client("bedrock-agentcore", region_name=REGION, config=Config(read_timeout=300))

app = FastAPI()
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    session_id = body.get("session_id")

    kwargs = {
        "agentRuntimeArn": AGENT_ARN,
        "qualifier": "DEFAULT",
        "payload": json.dumps({"prompt": prompt}),
    }
    if session_id:
        kwargs["runtimeSessionId"] = session_id

    resp = client.invoke_agent_runtime(**kwargs)
    resp_session_id = resp.get("runtimeSessionId", session_id or "")

    def stream():
        # Send session ID as first event
        yield f"data: {json.dumps({'type': 'session', 'session_id': resp_session_id})}\n\n"

        if "text/event-stream" in resp.get("contentType", ""):
            for line in resp["response"].iter_lines(chunk_size=1):
                if line:
                    decoded = line.decode("utf-8")
                    if decoded.startswith("data:"):
                        data = decoded[5:].strip()
                        try:
                            text = json.loads(data)
                        except (json.JSONDecodeError, TypeError):
                            text = data
                        if isinstance(text, str):
                            yield f"data: {json.dumps({'type': 'text', 'content': text})}\n\n"
        else:
            body = resp["response"].read().decode("utf-8")
            yield f"data: {json.dumps({'type': 'text', 'content': body})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# Serve React build
build_dir = os.path.realpath(os.path.join(SCRIPT_DIR, "build"))
index_html = os.path.join(build_dir, "index.html")


def _resolve_in_build_dir(path: str):
    """Return the real path of `path` under build_dir, or None if it escapes build_dir.

    `{path:path}` keeps `..` segments and decoded `%2F`, and os.path.join() discards
    build_dir entirely when `path` is absolute, so the joined path must be resolved
    (realpath also follows symlinks) and checked for containment before use.
    """
    candidate = os.path.realpath(os.path.join(build_dir, path))
    try:
        if os.path.commonpath([candidate, build_dir]) != build_dir:
            return None
    except ValueError:  # e.g. different drives on Windows
        return None
    return candidate


if os.path.exists(build_dir):
    app.mount("/static", StaticFiles(directory=os.path.join(build_dir, "static")), name="static")

    @app.get("/{path:path}")
    async def serve_react(path: str):
        file_path = _resolve_in_build_dir(path)
        if file_path and os.path.isfile(file_path):
            return FileResponse(file_path)
        # Unknown paths (and anything outside build_dir) fall back to the SPA entry point.
        return FileResponse(index_html)


if __name__ == "__main__":
    import uvicorn
    print(f"Agent ARN: {AGENT_ARN}")
    print(f"Starting server at http://localhost:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
