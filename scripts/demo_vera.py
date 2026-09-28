#!/usr/bin/env python3
"""Developer demo: drive a running Vera server through seven scenarios with the official seed data.

Talks to the real HTTP API (``/v1/context``, ``/v1/tick``, ``/v1/reply``) with the same
payload shapes the official ``judge_simulator.py`` sends, and prints each step as
INPUT / DECISION / MESSAGE / STATE.

Vera keeps all state in memory, so start a fresh server before each run::

    .venv/bin/python -m uvicorn app.main:app --port 8080 --log-level warning
    .venv/bin/python scripts/demo_vera.py

Standard library only; nothing here is imported by the application or the tests.
"""

import argparse
import json
import re
import sys
import textwrap
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DATASET_DIR = Path(__file__).resolve().parent.parent / "magicpin-ai-challenge" / "dataset"
TICK_NOW = datetime(2026, 4, 26, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
"""Simulated time of every tick; the seed data is written around this date (same as the test suite)."""

WIDTH = 100
BASE_URL = "http://localhost:8080"


# --------------------------------------------------------------------------- #
# Seed data
# --------------------------------------------------------------------------- #


def load_seed() -> dict[str, dict[str, dict[str, Any]]]:
    """``{scope: {context_id: payload}}`` from the official seed files (read-only)."""
    categories = [json.loads(p.read_text()) for p in sorted((DATASET_DIR / "categories").glob("*.json"))]
    return {
        "category": {c["slug"]: c for c in categories},
        "merchant": {m["merchant_id"]: m for m in json.loads((DATASET_DIR / "merchants_seed.json").read_text())["merchants"]},
        "customer": {c["customer_id"]: c for c in json.loads((DATASET_DIR / "customers_seed.json").read_text())["customers"]},
        "trigger": {t["id"]: t for t in json.loads((DATASET_DIR / "triggers_seed.json").read_text())["triggers"]},
    }


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def scenario(title: str, purpose: str) -> None:
    print("\n" + "=" * WIDTH)
    print(title)
    print(textwrap.fill(purpose, WIDTH))
    print("=" * WIDTH)


def step(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, WIDTH - len(title) - 5))


def section(label: str, *lines: str) -> None:
    print(f"  {label}")
    for line in lines:
        print(textwrap.fill(line, WIDTH, initial_indent="    ", subsequent_indent="      "))


def message_box(heading: str, body: str) -> None:
    print(f"  MESSAGE  >>> {heading}")
    print("    +" + "-" * (WIDTH - 6))
    for line in textwrap.wrap(body, WIDTH - 8):
        print(f"    | {line}")
    print("    +" + "-" * (WIDTH - 6))


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


class VeraClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")


# --------------------------------------------------------------------------- #
# Demo driver
# --------------------------------------------------------------------------- #

TICK_RATIONALE = re.compile(r"^(?P<action>\w+) \((?P<scope>\w+)\) for \S+: (?P<objective>.*)\. .*plan_id=(?P<plan_id>\S+)$")
REPLY_INTENT = re.compile(r"read as (?P<intent>\w+)")
REPLY_STATE = re.compile(r"state (?P<before>\w+) -> (?P<after>\w+)")
MERCHANT_SUPPRESSION = re.compile(r"suppress:merchant:\S+?(?=[,)\s;]|$)")


class Demo:
    def __init__(self, client: VeraClient, seed: dict[str, dict[str, dict[str, Any]]]) -> None:
        self.client = client
        self.seed = seed
        self.pushed: set[tuple[str, str]] = set()
        self.clock = TICK_NOW

    # ---- names --------------------------------------------------------- #

    def merchant_label(self, merchant_id: str) -> str:
        identity = self.seed["merchant"][merchant_id]["identity"]
        return f"{identity['name']} (owner {identity.get('owner_first_name', '?')}, {identity.get('locality', '')} {identity.get('city', '')}) [{merchant_id}]"

    def customer_label(self, customer_id: str) -> str:
        return f"{self.seed['customer'][customer_id]['identity']['name']} [{customer_id}]"

    # ---- /v1/context --------------------------------------------------- #

    def push_for(self, trigger_id: str) -> None:
        """Push the trigger and exactly the contexts it references (category, merchant, customer)."""
        trigger = self.seed["trigger"][trigger_id]
        merchant = self.seed["merchant"][trigger["merchant_id"]]
        wanted = [("category", merchant["category_slug"]), ("merchant", merchant["merchant_id"])]
        if trigger.get("customer_id"):
            wanted.append(("customer", trigger["customer_id"]))
        wanted.append(("trigger", trigger_id))

        lines = []
        for scope, context_id in wanted:
            if (scope, context_id) in self.pushed:
                continue
            body = {
                "scope": scope, "context_id": context_id, "version": 1,
                "payload": self.seed[scope][context_id], "delivered_at": TICK_NOW.isoformat(),
            }
            status, response = self.client.call("POST", "/v1/context", body)
            if status != 200:
                sys.exit(f"context push failed for {scope}/{context_id}: HTTP {status} {response}")
            self.pushed.add((scope, context_id))
            lines.append(f"POST /v1/context  scope={scope:<8} context_id={context_id}  version=1  -> HTTP {status} {response['outcome']}")
        if lines:
            section("INPUT (context pushes; payloads are the official seed records)", *lines)

    # ---- /v1/tick ------------------------------------------------------ #

    def tick(self, trigger_id: str, expected_skip: str | None = None) -> list[dict[str, Any]]:
        self.push_for(trigger_id)
        trigger = self.seed["trigger"][trigger_id]
        body = {"now": TICK_NOW.isoformat(), "available_triggers": [trigger_id]}
        status, response = self.client.call("POST", "/v1/tick", body)
        if status != 200:
            sys.exit(f"/v1/tick failed: HTTP {status} {response}")
        actions = response["actions"]

        section("INPUT", f"POST /v1/tick {json.dumps(body)}",
                f"merchant: {self.merchant_label(trigger['merchant_id'])}",
                f"trigger:  {trigger_id} (kind={trigger['kind']}, scope={trigger['scope']}, urgency={trigger.get('urgency')})")
        if not actions:
            decision = [f"HTTP {status}: 0 actions - Vera sends nothing for this trigger."]
            if expected_skip:
                decision.append(f"(the tick response carries no reason; the engine's eligibility reason here is '{expected_skip}')")
            section("DECISION", *decision)
            section("STATE", "No conversation opened; no suppression key committed by this tick.")
            return actions

        for action in actions:
            parsed = TICK_RATIONALE.match(action["rationale"])
            plan = parsed.groupdict() if parsed else {"action": "?", "scope": "?", "objective": "?", "plan_id": "?"}
            if action["send_as"] == "merchant_on_behalf":
                recipient = f"customer {self.customer_label(action['customer_id'])}"
                heading = (f"VERA MESSAGE - sent AS THE MERCHANT ({self.seed['merchant'][action['merchant_id']]['identity']['name']}) "
                           f"on its behalf, TO customer {self.seed['customer'][action['customer_id']]['identity']['name']}")
            else:
                recipient = f"merchant {self.merchant_label(action['merchant_id'])}"
                heading = f"VERA MESSAGE - sent AS VERA, TO merchant owner {self.seed['merchant'][action['merchant_id']]['identity'].get('owner_first_name')}"
            section(
                "DECISION",
                f"HTTP {status}: {len(actions)} action(s)",
                f"selected action: {plan['action']} ({plan['scope']}) - {plan['objective']}",
                f"send_as: {action['send_as']}    recipient: {recipient}",
                f"cta: {action['cta']}    template_name: {action['template_name']}",
                f"plan_id: {plan['plan_id']}",
            )
            message_box(heading, action["body"])
            section("STATE", f"conversation_id: {action['conversation_id']}  (new conversation opened)",
                    f"suppression_key: {action['suppression_key']}  (committed: this outreach will not be re-sent)")
        return actions

    # ---- /v1/reply ----------------------------------------------------- #

    def reply(self, action: dict[str, Any], message: str, turn: int) -> dict[str, Any]:
        self.clock += timedelta(minutes=5)
        from_role = "customer" if action["send_as"] == "merchant_on_behalf" else "merchant"
        body = {
            "conversation_id": action["conversation_id"], "merchant_id": action["merchant_id"],
            "customer_id": action["customer_id"], "from_role": from_role, "message": message,
            "received_at": self.clock.isoformat(), "turn_number": turn,
        }
        status, response = self.client.call("POST", "/v1/reply", body)
        if status != 200:
            sys.exit(f"/v1/reply failed: HTTP {status} {response}")

        rationale = response.get("rationale", "")
        intent = REPLY_INTENT.search(rationale)
        decision = [f"HTTP {status}: action={response['action'].upper()}"]
        if response["action"] == "send":
            decision[0] += f"  cta={response['cta']}"
        if response["action"] == "wait":
            decision[0] += f"  wait_seconds={response['wait_seconds']}"
        decision.append(f"reply read as: {intent['intent'] if intent else '(not re-read: conversation already closed)'}")
        decision.append(f"rationale: {rationale}")

        section("INPUT", f"{from_role} says: {message!r}", f"POST /v1/reply {json.dumps(body)}")
        section("DECISION", *decision)
        if response["action"] == "send":
            speaker = ("AS THE MERCHANT, to the customer" if from_role == "customer" else "AS VERA, to the merchant")
            message_box(f"VERA REPLY - {speaker}", response["body"])
        else:
            section("MESSAGE", f"(none - '{response['action']}' responses carry no body on the wire)")

        state = REPLY_STATE.search(rationale)
        state_lines = [f"conversation {action['conversation_id']}: "
                       + (f"{state['before']} -> {state['after']}" if state else "stays closed (ended)")]
        suppression = MERCHANT_SUPPRESSION.search(rationale)
        if suppression:
            state_lines.append(f"merchant suppression written: {suppression.group(0)} (every later trigger for this merchant is skipped)")
        section("STATE", *state_lines)
        return response


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


def run(demo: Demo) -> None:
    scenario("SCENARIO 1 - MERCHANT TICK",
             "Dr. Meera's clinic gets the JIDA research digest. Vera writes to the owner in its own voice.")
    demo.tick("trg_001_research_digest_dentists")

    scenario("SCENARIO 2 - CUSTOMER TICK",
             "Priya's 6-month dental recall is due. Vera writes to the customer as Dr. Meera's clinic (merchant_on_behalf).")
    [recall] = demo.tick("trg_003_recall_due_priya")

    scenario("SCENARIO 3 - MERCHANT CONVERSATION",
             "Dr. Bharat gets a call-volume dip alert, asks why, agrees, then says 'Cancel'.")
    step("tick")
    [dip] = demo.tick("trg_004_perf_dip_bharat")
    step("merchant asks why")
    demo.reply(dip, "Why did you flag this?", turn=2)
    step("merchant agrees")
    demo.reply(dip, "Okay, let's do it", turn=3)
    step("merchant cancels")
    demo.reply(dip, "Cancel", turn=4)

    scenario("SCENARIO 4 - CUSTOMER CONVERSATION",
             "Priya answers the recall message from Scenario 2 with questions, then agrees; the replies come from the "
             "clinic, not Vera.")
    step("customer asks about availability")
    demo.reply(recall, "Do you have any evening slots?", turn=2)
    step("customer asks about price")
    demo.reply(recall, "How much is the cleaning?", turn=3)
    step("customer agrees")
    demo.reply(recall, "yes", turn=4)

    scenario("SCENARIO 5 - SUPPRESSION",
             "The same Diwali trigger is ticked twice. The first tick sends and commits its suppression key; "
             "the second tick sends nothing.")
    step("first tick")
    demo.tick("trg_006_festival_diwali")
    step("second tick, same trigger and contexts")
    demo.tick("trg_006_festival_diwali", expected_skip="suppressed")

    scenario("SCENARIO 6 - AUTO-REPLY",
             "Powerhouse Gym's WhatsApp auto-responder answers three times in a row (api-call-examples 4.1).")
    step("tick")
    [gym] = demo.tick("trg_014_seasonal_acquisition_dip_powerhouse")
    auto = "Thank you for contacting us! Our team will respond shortly."
    for turn, label in enumerate(["first auto-reply", "second auto-reply", "third auto-reply"], start=2):
        step(label)
        demo.reply(gym, auto, turn=turn)

    scenario("SCENARIO 7 - HOSTILE / OPT-OUT",
             "7a: Pizza Junction's owner is hostile; the conversation ends and the merchant is suppressed, so a "
             "different trigger for the same merchant is then skipped. 7b: Zen Yoga's owner opts out; a later "
             "message stays closed.")
    step("7a tick")
    [pizza] = demo.tick("trg_011_review_theme_late_delivery")
    step("7a hostile reply")
    demo.reply(pizza, "Why are you bothering me. This is useless. Stop sending these.", turn=2)
    step("7a later tick, different trigger for the same merchant")
    demo.tick("trg_010_ipl_match_delhi", expected_skip="merchant_suppressed")
    step("7b tick")
    [zen] = demo.tick("trg_024_perf_spike_zen")
    step("7b opt-out reply")
    demo.reply(zen, "Not interested. Stop messaging me.", turn=2)
    step("7b message after the conversation ended")
    demo.reply(zen, "Actually, tell me more", turn=3)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=BASE_URL, help=f"Vera server (default {BASE_URL})")
    parser.add_argument("--force", action="store_true", help="run even if the server already holds contexts")
    args = parser.parse_args()

    client = VeraClient(args.base_url)
    try:
        status, health = client.call("GET", "/v1/healthz")
    except urllib.error.URLError as error:
        sys.exit(f"Vera is not reachable at {args.base_url} ({error.reason}). Start it with:\n"
                 "  .venv/bin/python -m uvicorn app.main:app --port 8080 --log-level warning")
    loaded = sum(health.get("contexts_loaded", {}).values())
    if loaded and not args.force:
        sys.exit(f"The server already holds {loaded} contexts (and possibly suppressions) from an earlier run. "
                 "Restart it for a clean demo, or pass --force.")
    _, metadata = client.call("GET", "/v1/metadata")
    print(f"Vera at {args.base_url}: healthz HTTP {status}; engine={metadata.get('engine')} model={metadata.get('model')}")
    print(f"Seed data: {DATASET_DIR}   simulated tick time: {TICK_NOW.isoformat()}")

    run(Demo(client, load_seed()))
    print("\nDemo complete.")


if __name__ == "__main__":
    main()
