# PR Review Copilot + Risk Radar

AI-powered GitHub webhook service that automatically reviews pull requests for security + code quality and computes a risk score with automated gates.

## What this project does

- Receives GitHub `pull_request` webhook events (`opened`, `reopened`, `synchronize`)
- Runs **Review Copilot** using Groq (Llama 3.3 70B) to analyze unified diffs
- Posts inline review comments back to the pull request
- Runs **Risk Radar** to score PR risk (`low`, `medium`, `high`)
- Applies labels (`risk:low`, `risk:medium`, `risk:high`)
- Enforces 2 required approvals for HIGH risk PRs (when repository permissions allow)
- Posts Friday-after-3pm warning for HIGH/MEDIUM risk PRs
- Sends Slack and/or Discord alert for HIGH risk PRs

## Project structure

```text
.
|-- main.py
|-- webhook_handler.py
|-- agents/
|   |-- review_copilot.py
|   `-- risk_radar.py
|-- integrations/
|   |-- github_client.py
|   |-- groq_client.py
|   |-- discord_client.py
|   `-- slack_client.py
|-- prompts/
|   |-- security_review.txt
|   `-- code_quality.txt
|-- config.py
|-- .env.example
|-- requirements.txt
`-- README.md
```

## Prerequisites

- Python 3.11+
- GitHub repository admin access (for branch protection updates)
- Groq API key
- Optional: Slack incoming webhook URL
- Optional: Discord webhook URL
- Optional for local webhook testing: ngrok

## Setup

1. Create and activate a virtual environment.
2. Install dependencies.
3. Create `.env` from `.env.example` and fill values.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

## Environment variables

```env
GITHUB_TOKEN=...
GITHUB_WEBHOOK_SECRET=...
GROQ_API_KEY=...
SLACK_WEBHOOK_URL=...
DISCORD_WEBHOOK_URL=...
RISK_HIGH_THRESHOLD=67
RISK_MEDIUM_THRESHOLD=34
PORT=8000
GROQ_MODEL=llama-3.3-70b-versatile
REQUEST_TIMEOUT_SECONDS=25
```

## Run locally

```powershell
python main.py
```

## Run tests

```powershell
python -m unittest discover -s tests -v
```

Health check:

```text
GET http://localhost:8000/health
```

## Expose local server (ngrok)

```powershell
ngrok http 8000
```

Use the generated HTTPS URL + `/webhook` as the GitHub webhook payload URL.

## Local signed webhook simulation

You can send a signed webhook payload to your local server without configuring GitHub first:

```powershell
python scripts/simulate_webhook.py --event pull_request --action opened --repo <owner/repo> --pr <number>
```

Quick connectivity check (ignored event path):

```powershell
python scripts/simulate_webhook.py --event push --action opened
```

## GitHub webhook configuration

- Webhook URL: `https://<your-ngrok-or-host>/webhook`
- Content type: `application/json`
- Secret: same as `GITHUB_WEBHOOK_SECRET`
- Events: select `Pull requests`
- Active: enabled

## Discord webhook setup (optional)

1. Open your Discord server settings.
2. Go to Integrations -> Webhooks.
3. Click New Webhook.
4. Choose the channel where alerts should appear.
5. Copy the Webhook URL.
6. Put it into `DISCORD_WEBHOOK_URL` in your `.env` file.

If both `SLACK_WEBHOOK_URL` and `DISCORD_WEBHOOK_URL` are set, HIGH-risk alerts are sent to both.

## How the flow works

1. GitHub sends a `pull_request` event.
2. Signature is validated with `X-Hub-Signature-256`.
3. Two tasks run concurrently:
   - Review Copilot: diff -> Groq -> structured comments -> GitHub review
   - Risk Radar: files/sensitive paths/tests/time -> score -> labels/gates/alerts

## Notes and limitations

- Inline comments can fail if model line numbers do not map to current diff context. The service falls back to issue comments.
- Updating required approvals requires appropriate GitHub token permissions and protected branch settings.
- Large diffs are truncated before model submission to stay performant.

## Demo checklist

- Open PR touching `auth/` or `migrations/`
- Confirm inline review comments appear
- Confirm risk label is applied
- Confirm HIGH risk sends Slack/Discord alert and attempts 2-approval gate
- Confirm Friday-after-3pm warning comment behavior
