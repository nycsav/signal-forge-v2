#!/usr/bin/env python3
"""
Full 6-source email signal scan with 14-day lookback.
Extracts signals, cross-validates, and generates opportunity report.
"""
import asyncio
import json
import sys
import os
import re
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.email_parsers import EMAIL_SOURCES, strip_html, parse_llm_response, MAX_EMAIL_BONUS_PER_SYMBOL, CROSS_VALIDATION_BONUS, CROSS_VALIDATION_CONFIDENCE_BOOST

GMAIL_MCP_PATH = "/Users/sav/gmail-mcp-server"
BRIDGE = os.path.join(GMAIL_MCP_PATH, "scripts", "gmail-bridge.ts")


async def gmail_search(query: str, max_results: int = 15) -> list[dict]:
    proc = await asyncio.create_subprocess_exec(
        "npx", "tsx", BRIDGE, "search", query, str(max_results),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=GMAIL_MCP_PATH,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode != 0:
        print(f"  Search error: {stderr.decode()[:150]}", file=sys.stderr)
        return []
    try:
        return json.loads(stdout.decode())
    except:
        return []


async def gmail_read(message_id: str) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "npx", "tsx", BRIDGE, "read", message_id,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=GMAIL_MCP_PATH,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(stdout.decode())
    except:
        return {}


async def extract_ollama(source: str, prompt_template: str, body: str) -> list[dict]:
    body_clean = body[:4000].replace("{", "(").replace("}", ")").replace("$", "USD ")
    try:
        prompt = prompt_template.format(body=body_clean)
    except:
        return []

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
        raw = result.get("response", "")
        return parse_llm_response(raw) if raw else []
    except Exception as e:
        print(f"  Ollama error ({source}): {e}", file=sys.stderr)
        return []


async def label_email(message_id: str):
    proc = await asyncio.create_subprocess_exec(
        "npx", "tsx", BRIDGE, "label", message_id, "Label_8",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=GMAIL_MCP_PATH,
    )
    await asyncio.wait_for(proc.communicate(), timeout=15)


async def scan_source(name: str, cfg: dict, lookback: str = "14d") -> list[dict]:
    base_query = re.sub(r'newer_than:\S+', '', cfg["gmail_query"]).strip()
    query = f"{base_query} newer_than:{lookback}"
    print(f"\n--- {name.upper()} ---")
    print(f"  Query: {query}")

    emails = await gmail_search(query, 10)
    print(f"  Found: {len(emails)} emails")

    signals = []
    for email in emails[:5]:
        subject = email.get("subject", "?")[:70]
        msg_id = email.get("id", "")
        date = email.get("date", "")
        print(f"  [{date[:10]}] {subject}")

        full = await gmail_read(msg_id)
        body = full.get("body_text", "")
        if not body:
            html = full.get("body_html", "")
            body = strip_html(html) if html else ""
        if len(body) < 50:
            print(f"    -> too short, skip")
            continue

        extracted = await extract_ollama(name, cfg["extract_prompt"], body)
        bonus_map = cfg.get("score_bonus_map", {})
        for sig in extracted:
            sig["source"] = name
            sig["email_id"] = msg_id
            sig["email_subject"] = subject
            sig["email_date"] = date
            sig_type = sig.get("signal_type", "")
            sig["score_bonus"] = bonus_map.get(sig_type, 0.0)
            signals.append(sig)

        if extracted:
            print(f"    -> {len(extracted)} signals extracted")
            # Label and mark as read
            await label_email(msg_id)
        else:
            print(f"    -> no signals")

    return signals


async def main():
    print("=" * 70)
    print(f"FULL EMAIL SIGNAL SCAN — {datetime.now().strftime('%A, %B %d, %Y %H:%M %Z')}")
    print("=" * 70)

    all_signals = []
    for name, cfg in EMAIL_SOURCES.items():
        signals = await scan_source(name, cfg)
        all_signals.extend(signals)

    # Cross-validation
    print(f"\n{'=' * 70}")
    print("CROSS-VALIDATION")
    symbol_groups: dict[tuple, list] = {}
    for sig in all_signals:
        for sym in sig.get("symbols", []):
            key = (sym.upper(), sig.get("direction", "neutral"))
            symbol_groups.setdefault(key, []).append(sig)

    for (symbol, direction), sigs in symbol_groups.items():
        sources = set(s["source"] for s in sigs)
        if len(sources) >= 2:
            print(f"  CROSS-VALIDATED: {symbol} {direction} from {sources}")
            for s in sigs:
                s["cross_validated"] = True
                s["score_bonus"] = min(s.get("score_bonus", 0) + CROSS_VALIDATION_BONUS, MAX_EMAIL_BONUS_PER_SYMBOL)
                s["confidence"] = min(1.0, s.get("confidence", 0.5) + CROSS_VALIDATION_CONFIDENCE_BOOST)

    # Build opportunity report
    print(f"\n{'=' * 70}")
    print(f"OPPORTUNITY REPORT — {len(all_signals)} signals from {len(EMAIL_SOURCES)} sources")
    print("=" * 70)

    by_symbol: dict[str, list] = {}
    for sig in all_signals:
        for sym in sig.get("symbols", []):
            sym = sym.upper()
            by_symbol.setdefault(sym, []).append(sig)

    ranked = sorted(
        by_symbol.items(),
        key=lambda x: (
            any(s.get("cross_validated") for s in x[1]),  # cross-validated first
            sum(s.get("score_bonus", 0) for s in x[1]),   # then by total bonus
            sum(s.get("confidence", 0) for s in x[1]),    # then by confidence
        ),
        reverse=True,
    )

    # Regime signals
    regime_signals = [s for s in all_signals if s.get("signal_type") in ("regime_call", "macro_regime")]
    risk_signals = [s for s in all_signals if s.get("signal_type") == "risk_event"]
    fg_signals = [s for s in all_signals if s.get("signal_type") in ("fg_extreme", "fg_index")]
    funding_signals = [s for s in all_signals if s.get("signal_type") == "funding_negative_extended"]

    if regime_signals or fg_signals or funding_signals:
        print(f"\n  MARKET CONTEXT:")
        for r in regime_signals:
            print(f"    Regime: {r.get('direction', '?')} (conf {r.get('confidence', 0):.2f}) — {r.get('source')}")
            details = r.get("details", {})
            if details:
                print(f"      {json.dumps(details)[:150]}")
        for f in fg_signals:
            print(f"    Fear & Greed: {json.dumps(f.get('details', {}))[:100]} — {f.get('source')}")
        for f in funding_signals:
            print(f"    Funding: {json.dumps(f.get('details', {}))[:100]} — {f.get('source')}")

    if risk_signals:
        print(f"\n  RISK EVENTS:")
        for r in risk_signals:
            print(f"    [{r.get('source')}] {json.dumps(r.get('details', {}))[:150]}")

    print(f"\n  TRADING OPPORTUNITIES (ranked by conviction):\n")
    print(f"  {'Symbol':8s} {'Direction':10s} {'Bonus':>6s} {'Conf':>6s} {'Cross':>6s} Sources")
    print(f"  {'─'*8} {'─'*10} {'─'*6} {'─'*6} {'─'*6} {'─'*30}")

    for symbol, sigs in ranked[:20]:
        total_bonus = min(sum(s.get("score_bonus", 0) for s in sigs), MAX_EMAIL_BONUS_PER_SYMBOL)
        avg_conf = sum(s.get("confidence", 0) for s in sigs) / len(sigs)
        directions = list(set(s.get("direction", "?") for s in sigs))
        sources = list(set(s.get("source", "?") for s in sigs))
        cross = "YES" if any(s.get("cross_validated") for s in sigs) else "no"
        dir_str = "/".join(directions)

        print(f"  {symbol:8s} {dir_str:10s} {total_bonus:>+5.0f}  {avg_conf:>5.2f}  {cross:>5s}  {', '.join(sources)}")

        # Show details for top signals
        for sig in sigs[:2]:
            details = sig.get("details", {})
            sig_type = sig.get("signal_type", "?")
            if details:
                detail_str = json.dumps(details)[:120]
                print(f"           └─ {sig_type}: {detail_str}")

    # Save full results
    with open("/tmp/full_signal_scan.json", "w") as f:
        json.dump(all_signals, f, indent=2, default=str)
    print(f"\n  Full results: /tmp/full_signal_scan.json")
    print(f"  Processed emails labeled 'crypto-signal' in Gmail")


if __name__ == "__main__":
    asyncio.run(main())
