#!/usr/bin/env python3
"""
Quick 3-source test scan for debugging the email signal pipeline.

Scans only altfins, coinbase_research, and cmc (7-day lookback, 3 emails each)
to verify Gmail bridge, Ollama extraction, and cross-validation are working.
For the full 6-source production scan, use scripts/full_signal_scan.py.

Run from SF2 root: python scripts/test_email_scan.py
"""
import asyncio
import json
import sys
import os
import subprocess
from datetime import datetime, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.email_parsers import EMAIL_SOURCES, strip_html, parse_llm_response

GMAIL_MCP_PATH = "/Users/sav/gmail-mcp-server"


BRIDGE_SCRIPT = os.path.join(GMAIL_MCP_PATH, "scripts", "gmail-bridge.ts")


async def gmail_search(query: str, max_results: int = 20) -> list[dict]:
    """Search Gmail via the bridge script."""
    proc = await asyncio.create_subprocess_exec(
        "npx", "tsx", BRIDGE_SCRIPT, "search", query, str(max_results),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=GMAIL_MCP_PATH,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode != 0:
        print(f"  Gmail search error: {stderr.decode()[:200]}", file=sys.stderr)
        return []
    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError:
        return []


async def gmail_read(message_id: str) -> dict:
    """Read a single email via the bridge script."""
    proc = await asyncio.create_subprocess_exec(
        "npx", "tsx", BRIDGE_SCRIPT, "read", message_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=GMAIL_MCP_PATH,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError:
        return {}


async def extract_with_ollama(source: str, prompt_template: str, email_body: str) -> list[dict]:
    """Extract signals using Ollama Qwen3."""
    # Sanitize body to avoid .format() issues with curly braces and dollar signs
    body_truncated = email_body[:4000].replace("{", "(").replace("}", ")").replace("$", "USD ")
    try:
        prompt = prompt_template.format(body=body_truncated)
    except (KeyError, IndexError) as e:
        print(f"  Prompt format error for {source}: {e}", file=sys.stderr)
        return []

    import urllib.request

    data = json.dumps({
        "model": "qwen3:14b",
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 4000},
    }).encode()

    def _call():
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())

    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _call)
        raw_text = result.get("response", "")
        if not raw_text:
            print(f"    Ollama returned empty response (eval_count={result.get('eval_count', 0)})")
            return []
        return parse_llm_response(raw_text)
    except Exception as e:
        print(f"  Ollama error for {source}: {e}", file=sys.stderr)
        return []


async def scan_source(source_name: str, source_config: dict) -> list[dict]:
    """Scan one email source end-to-end."""
    # Strip any existing newer_than from the source query, use 7d for testing
    base_query = source_config["gmail_query"]
    import re
    base_query = re.sub(r'newer_than:\S+', '', base_query).strip()
    query = base_query + " newer_than:7d"
    print(f"\n{'='*60}")
    print(f"Scanning: {source_name}")
    print(f"Query: {query}")

    emails = await gmail_search(query, max_results=5)
    print(f"  Found {len(emails)} emails")

    all_signals = []
    for email in emails[:3]:  # Process top 3 for test
        subject = email.get("subject", "?")
        email_id = email.get("id", "")
        print(f"  Reading: {subject[:60]}...")

        full = await gmail_read(email_id)
        if not full:
            print(f"    Failed to read email")
            continue

        body = full.get("body_text", "")
        if not body:
            html = full.get("body_html", "")
            body = strip_html(html) if html else ""

        if len(body) < 50:
            print(f"    Body too short ({len(body)} chars), skipping")
            continue

        print(f"    Body: {len(body)} chars, extracting signals...")
        signals = await extract_with_ollama(
            source_name,
            source_config["extract_prompt"],
            body,
        )
        print(f"    Extracted {len(signals)} signals")

        for sig in signals:
            sig["source"] = source_name
            sig["email_id"] = email_id
            sig["email_subject"] = subject
            # Apply bonus conditions
            bonus_map = source_config.get("score_bonus_map", {})
            sig_type = sig.get("signal_type", "")
            sig["score_bonus"] = bonus_map.get(sig_type, 0.0)
            all_signals.append(sig)

    return all_signals


async def main():
    print("=" * 60)
    print("EMAIL SIGNAL AGENT — LIVE TEST SCAN")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Check Ollama is running
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "http://localhost:11434/api/tags",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    if proc.returncode != 0:
        print("ERROR: Ollama not running. Start it first.", file=sys.stderr)
        sys.exit(1)

    models = json.loads(stdout.decode()).get("models", [])
    model_names = [m["name"] for m in models]
    print(f"Ollama models: {model_names}")
    if not any("qwen3" in m for m in model_names):
        print("WARNING: qwen3 not found in Ollama models. Using first available.", file=sys.stderr)

    # Scan top 3 sources (most signal-rich)
    priority_sources = ["altfins", "coinbase_research", "cmc"]
    all_signals = []

    for source_name in priority_sources:
        if source_name not in EMAIL_SOURCES:
            continue
        signals = await scan_source(source_name, EMAIL_SOURCES[source_name])
        all_signals.extend(signals)

    # Cross-validate
    print(f"\n{'='*60}")
    print(f"CROSS-VALIDATION")
    symbol_signals: dict[tuple, list] = {}
    for sig in all_signals:
        for sym in sig.get("symbols", []):
            key = (sym.upper(), sig.get("direction", "neutral"))
            if key not in symbol_signals:
                symbol_signals[key] = []
            symbol_signals[key].append(sig)

    cross_validated = []
    for (symbol, direction), sigs in symbol_signals.items():
        sources = set(s["source"] for s in sigs)
        if len(sources) >= 2:
            print(f"  CROSS-VALIDATED: {symbol} {direction} — {sources}")
            for s in sigs:
                s["cross_validated"] = True
                s["score_bonus"] = min(s.get("score_bonus", 0) + 3, 15)
            cross_validated.append((symbol, direction, sources))

    # Output results
    print(f"\n{'='*60}")
    print(f"RESULTS: {len(all_signals)} total signals extracted")
    print(f"{'='*60}\n")

    # Group by symbol
    by_symbol: dict[str, list] = {}
    for sig in all_signals:
        for sym in sig.get("symbols", []):
            sym = sym.upper()
            if sym not in by_symbol:
                by_symbol[sym] = []
            by_symbol[sym].append(sig)

    # Sort by total bonus descending
    ranked = sorted(by_symbol.items(), key=lambda x: sum(s.get("score_bonus", 0) for s in x[1]), reverse=True)

    for symbol, sigs in ranked[:15]:
        total_bonus = sum(s.get("score_bonus", 0) for s in sigs)
        directions = [s.get("direction", "?") for s in sigs]
        sources = list(set(s.get("source", "?") for s in sigs))
        cross = any(s.get("cross_validated") for s in sigs)

        print(f"  {symbol:8s} | bonus: +{total_bonus:5.1f} | direction: {', '.join(set(directions)):10s} | sources: {', '.join(sources)} {'[CROSS-VALIDATED]' if cross else ''}")
        for sig in sigs:
            details = sig.get("details", {})
            if details:
                detail_str = json.dumps(details)[:120]
                print(f"           └─ {sig.get('signal_type', '?')}: {detail_str}")

    # Dump full results
    output_path = "/tmp/email_signal_test_results.json"
    with open(output_path, "w") as f:
        json.dump(all_signals, f, indent=2, default=str)
    print(f"\nFull results saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
