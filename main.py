from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI

from config import get_settings
from webhook_handler import WebhookHandler, create_webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

settings = get_settings()
handler = WebhookHandler(settings)

app = FastAPI(
    title="PR Review Copilot + Risk Radar",
    version="1.0.0",
    description="AI-powered pull request review and risk scoring webhook service.",
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(create_webhook_router(handler))


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=settings.port)
