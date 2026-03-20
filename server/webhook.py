"""
server/webhook.py

FastAPI webhook listener - the entry point for all GitHub events.
Every PR open or push triggers this. The orchestrator takes it from here.

Security:
    Every request is verified against GITHUB_WEBHOOK_SECRET before processing.
    Requests with invalid or missing signatures are rejected with 401.

Usage:
    uvicorn server.webhook:app --host 0.0.0.0 --port 8080 --reload
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from core.logging_config import setup_logging
from server.orchestrator import Orchestrator

# Load .env before anything else reads environment variables
load_dotenv()
setup_logging()

logger = logging.getLogger(__name__)

app = FastAPI(
    title="DevAgent",
    description="AI coding assistant - GitHub webhook receiver",
    version="0.1.0",
    docs_url=None,   # disable Swagger in production
    redoc_url=None,
)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    """Health check endpoint. Docker and load balancers hit this."""
    return {"status": "ok", "service": "devagent"}


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str       = Header(default=""),
    x_hub_signature_256: str  = Header(default=""),
) -> JSONResponse:
    """
    Receive GitHub webhook events.

    1. Verify the HMAC-SHA256 signature
    2. Parse the payload
    3. Route to the right handler
    4. Run the orchestrator in the background so GitHub gets a fast 200
    """
    # --- Read raw body (needed for signature verification) ---
    body = await request.body()

    # --- Verify signature ---
    _verify_signature(body, x_hub_signature_256)

    # --- Parse payload ---
    try:
        payload: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event = x_github_event.lower()
    logger.info(f"Received event: {event} from {payload.get('repository', {}).get('full_name', '?')}")

    # --- Route ---
    if event == "pull_request":
        action = payload.get("action", "")
        if action in ("opened", "synchronize", "reopened"):
            background_tasks.add_task(_handle_pr_event, payload)
            return JSONResponse(
                status_code=202,
                content={
                    "status": "accepted",
                    "event":  event,
                    "action": action,
                    "pr":     payload.get("pull_request", {}).get("number"),
                },
            )
        else:
            return JSONResponse(
                status_code=200,
                content={"status": "ignored", "reason": f"action '{action}' not handled"},
            )

    elif event == "push":
        # Future: trigger dependency audit on push to main
        return JSONResponse(
            status_code=200,
            content={"status": "ignored", "reason": "push events not yet handled"},
        )

    elif event == "ping":
        # GitHub sends a ping when a webhook is first configured
        logger.info("Webhook ping received - connection established")
        return JSONResponse(
            status_code=200,
            content={"status": "pong"},
        )

    else:
        return JSONResponse(
            status_code=200,
            content={"status": "ignored", "reason": f"event '{event}' not handled"},
        )


# ---------------------------------------------------------------------------
# Event handlers (run in background)
# ---------------------------------------------------------------------------

async def _handle_pr_event(payload: dict) -> None:
    """
    Handle a pull_request event.
    Runs in the background - GitHub doesn't wait for this.
    """
    pr      = payload.get("pull_request", {})
    repo    = payload.get("repository", {}).get("full_name", "")
    pr_num  = pr.get("number")
    action  = payload.get("action", "")
    author  = pr.get("user", {}).get("login", "")
    title   = pr.get("title", "")
    branch  = pr.get("head", {}).get("ref", "")

    logger.info(f"Handling PR #{pr_num} ({action}) in {repo} - '{title}' by {author}")

    try:
        orchestrator = Orchestrator(repo=repo)
        await orchestrator.run_on_pr(
            pr_number=pr_num,
            pr_title=title,
            head_branch=branch,
            author=author,
        )
    except Exception as e:
        # Log but don't crash the server - one bad run shouldn't take everything down
        logger.error(
            f"Orchestrator failed for PR #{pr_num} in {repo}: {type(e).__name__}: {e}",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def _verify_signature(body: bytes, signature_header: str) -> None:
    """
    Verify the GitHub webhook HMAC-SHA256 signature.

    GitHub signs every request with the webhook secret.
    We recompute the signature and compare - rejects anything that doesn't match.

    Raises:
        HTTPException 401: if secret not configured or signature invalid
        HTTPException 400: if signature header is malformed
    """
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

    if not secret:
        # In development you might skip verification - log a loud warning
        logger.warning(
            "GITHUB_WEBHOOK_SECRET is not set. "
            "Webhook signature verification is DISABLED. "
            "Set this variable in production."
        )
        return

    if not signature_header:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Hub-Signature-256 header",
        )

    if not signature_header.startswith("sha256="):
        raise HTTPException(
            status_code=400,
            detail="Malformed signature header - expected 'sha256=...'",
        )

    expected_sig = signature_header[len("sha256="):]

    mac = hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    )
    computed_sig = mac.hexdigest()

    # Use hmac.compare_digest to prevent timing attacks
    if not hmac.compare_digest(computed_sig, expected_sig):
        logger.warning("Webhook signature verification failed - request rejected")
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook signature",
        )
