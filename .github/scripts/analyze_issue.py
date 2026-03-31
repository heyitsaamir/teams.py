"""
Copyright (c) Microsoft Corporation. All rights reserved.
Licensed under the MIT License.
"""

# GitHub Issue Analysis → Teams Notification
# Analyzes newly opened GitHub issues using the GitHub Models API (GPT-4o)
# and sends a rich Adaptive Card summary to a Microsoft Teams channel.

import asyncio
import json
import os
import sys
import urllib.request

from microsoft_teams.apps import App
from microsoft_teams.cards import (
    ActionSet,
    AdaptiveCard,
    Column,
    ColumnSet,
    Container,
    Fact,
    FactSet,
    OpenUrlAction,
    TextBlock,
)
from openai import OpenAI

SEVERITY_COLORS = {
    "critical": "Attention",
    "high": "Attention",
    "medium": "Warning",
    "low": "Good",
    "info": "Good",
}

SYSTEM_PROMPT = """\
You are a GitHub issue triage assistant for the Microsoft Teams Python SDK.

The SDK is a UV workspace with these packages:
- api: Core API clients, models, auth
- apps: App orchestrator, plugins, routing, events, HttpServer
- common: HTTP client abstraction, logging, storage
- cards: Adaptive cards
- ai: AI/function calling utilities
- botbuilder: Bot Framework integration plugin
- devtools: Development tools plugin
- mcpplugin: MCP server plugin
- a2aprotocol: A2A protocol plugin
- graph: Microsoft Graph integration
- openai: OpenAI integration

Analyze the issue and respond with ONLY valid JSON (no markdown fencing):
{
  "category": "bug | feature | question | docs | security",
  "severity": "critical | high | medium | low | info",
  "summary": "1-2 sentence plain-text summary of the issue",
  "affected_packages": ["list", "of", "affected", "packages"],
  "suggested_labels": ["list", "of", "suggested", "labels"],
  "key_details": "Brief bullet points of key technical details (plain text)"
}\
"""


def _parse_issue(issue: dict) -> dict:
    """Extract fields from a GitHub issue object."""
    return {
        "title": issue.get("title", ""),
        "body": issue.get("body", "") or "",
        "labels": [label.get("name", "") for label in issue.get("labels", [])],
        "author": issue.get("user", {}).get("login", "unknown"),
        "number": issue.get("number", 0),
        "html_url": issue.get("html_url", ""),
    }


def load_issue_from_event() -> dict:
    """Read issue data from the event payload, or fetch by number for manual triggers."""
    issue_number = os.environ.get("ISSUE_NUMBER")
    if issue_number:
        return fetch_issue(int(issue_number))

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        print("ERROR: GITHUB_EVENT_PATH not set and ISSUE_NUMBER not provided")
        sys.exit(1)

    with open(event_path) as f:
        event = json.load(f)

    return _parse_issue(event.get("issue", {}))


def fetch_issue(issue_number: int) -> dict:
    """Fetch an issue from the GitHub API by number."""
    repo = os.environ.get("GITHUB_UPSTREAM_REPO") or os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not repo:
        print("ERROR: GITHUB_REPOSITORY not set")
        sys.exit(1)

    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        issue = json.loads(resp.read().decode())

    return _parse_issue(issue)


def analyze_issue(issue: dict) -> dict:
    """Call GitHub Models API to analyze the issue."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: GITHUB_TOKEN not set")
        sys.exit(1)

    client = OpenAI(
        base_url="https://models.inference.ai.azure.com",
        api_key=token,
    )

    user_message = (
        f"Issue #{issue['number']}: {issue['title']}\n\n"
        f"Author: {issue['author']}\n"
        f"Labels: {', '.join(issue['labels']) or 'none'}\n\n"
        f"Body:\n{issue['body'][:3000]}"
    )

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.2,
    )

    content = response.choices[0].message.content or "{}"
    return json.loads(content)


def build_analysis_card(issue: dict, analysis: dict) -> AdaptiveCard:
    """Build an Adaptive Card with the issue analysis."""
    repo = os.environ.get("GITHUB_REPOSITORY", "microsoft/teams.py")
    severity = analysis.get("severity", "info")
    severity_color = SEVERITY_COLORS.get(severity, "Default")

    return AdaptiveCard(
        version="1.5",
        body=[
            # Header
            TextBlock(
                text=f"{repo}#{issue['number']}: {issue['title']}",
                size="Medium",
                weight="Bolder",
                wrap=True,
            ),
            # Metadata columns: category | severity | author
            ColumnSet(
                columns=[
                    Column(
                        width="auto",
                        items=[
                            TextBlock(
                                text=analysis.get("category", "unknown").upper(),
                                weight="Bolder",
                                is_subtle=True,
                                size="Small",
                            ),
                        ],
                    ),
                    Column(
                        width="auto",
                        items=[
                            TextBlock(
                                text=severity.upper(),
                                color=severity_color,
                                weight="Bolder",
                                size="Small",
                            ),
                        ],
                    ),
                    Column(
                        width="stretch",
                        items=[
                            TextBlock(
                                text=f"by @{issue['author']}",
                                is_subtle=True,
                                size="Small",
                                horizontal_alignment="Right",
                            ),
                        ],
                    ),
                ],
            ),
            # AI Summary
            Container(
                style="Emphasis",
                items=[
                    TextBlock(
                        text=analysis.get("summary", "No summary available."),
                        wrap=True,
                    ),
                ],
            ),
            # Details
            FactSet(
                facts=[
                    Fact(title="Packages", value=", ".join(analysis.get("affected_packages", [])) or "N/A"),
                    Fact(title="Labels", value=", ".join(analysis.get("suggested_labels", [])) or "N/A"),
                    Fact(title="Details", value=analysis.get("key_details", "N/A")),
                ],
            ),
            # Action
            ActionSet(
                actions=[
                    OpenUrlAction(title="View Issue", url=issue["html_url"]),
                ],
            ),
        ],
    )


async def send_to_teams(card: AdaptiveCard) -> None:
    """Send the card to Teams via proactive messaging."""
    conversation_id = os.environ.get("TEAMS_CONVERSATION_ID")
    if not conversation_id:
        print("ERROR: TEAMS_CONVERSATION_ID not set")
        sys.exit(1)

    app = App()
    await app.initialize()
    result = await app.send(conversation_id, card)
    print(f"Card sent to Teams. Activity ID: {result.id}")


async def main() -> None:
    print("Loading issue from event payload...")
    issue = load_issue_from_event()
    print(f"Issue #{issue['number']}: {issue['title']}")

    print("Analyzing issue with GitHub Models API...")
    analysis = analyze_issue(issue)
    print(f"Analysis: category={analysis.get('category')}, severity={analysis.get('severity')}")

    print("Building Adaptive Card...")
    card = build_analysis_card(issue, analysis)

    print("Sending to Teams...")
    await send_to_teams(card)

    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
