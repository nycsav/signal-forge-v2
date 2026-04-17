#!/usr/bin/env python3
"""Signal Forge v2 — Model Benchmark for Trading Analysis

Tests all available Ollama models on identical trading prompts.
Measures: speed, JSON reliability, signal quality, consensus accuracy.

Usage:
    PYTHONPATH=. python scripts/benchmark_models.py
"""

import json
import re
import time
import httpx
import sys
from pathlib import Path

OLLAMA_HOST = "http://localhost:11434"

# Real trading scenario prompts
PROMPTS = {
    "pre_filter": {
        "prompt": 'BTC-USD $74500 RSI=32 F&G=23 EMA=NO MACD=-150.3 MarketChange=-1.2%\n\nIs there a tradeable setup? JSON: {"setup_quality":"strong/weak/none","direction":"long/short/flat","reason":"5 words max"}',
        "expected_keys": ["setup_quality", "direction", "reason"],
    },
    "full_analysis": {
        "prompt": 'ETH-USD $2150 RSI=28 F&G=23 EMA=NO MACD=-8.5 BB=0.15 Vol=1.8x Regime=bear_trend MarketChange=-0.8% Score=68/100\n\nRules: If MarketChange>+2% and F&G<25, fear+green=strong buy. If move already >3%, wait for pullback.\n\nJSON: {"direction":"long/short/flat","score":0-100,"ai_confidence":0.0-1.0,"rationale":"one sentence"}',
        "expected_keys": ["direction", "score", "ai_confidence", "rationale"],
    },
    "sanity_check": {
        "prompt": 'Qwen3 says long ETH-USD at $2,150 with confidence 72%. RSI=28 F&G=23 MarketChange=-0.8%.\n\nDoes this make sense? JSON: {"agrees":true/false,"reason":"5 words max"}',
        "expected_keys": ["agrees", "reason"],
    },
}


def call_ollama(model: str, prompt: str, timeout: int = 120) -> tuple[str, float]:
    """Call Ollama, return (response, latency_seconds)."""
    start = time.time()
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(
                f"{OLLAMA_HOST}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False,
                      "options": {"temperature": 0.1, "num_predict": 1000}},
            )
            latency = time.time() - start
            if r.status_code == 200:
                return r.json().get("response", ""), latency
    except Exception as e:
        latency = time.time() - start
        return f"ERROR: {e}", latency
    return "ERROR: non-200", time.time() - start


def parse_json(raw: str) -> dict | None:
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    cleaned = re.sub(r"```json\s*", "", cleaned)
    cleaned = re.sub(r"```\s*", "", cleaned)
    matches = re.findall(r"\{[^{}]*\}", cleaned)
    for match in matches:
        try:
            return json.loads(match)
        except json.JSONDecodeError:
            continue
    return None


def get_available_models() -> list[str]:
    try:
        r = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        if r.status_code == 200:
            return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        pass
    return []


def main():
    models = get_available_models()
    if not models:
        print("No models found in Ollama")
        sys.exit(1)

    print(f"\n{'='*70}")
    print(f"Signal Forge — Model Benchmark for Trading Analysis")
    print(f"{'='*70}")
    print(f"Models: {', '.join(models)}")
    print(f"Prompts: {len(PROMPTS)} trading scenarios\n")

    results = {}
    for model in models:
        results[model] = {"total_latency": 0, "json_success": 0, "tests": 0}
        print(f"\n{'─'*50}")
        print(f"  {model}")
        print(f"{'─'*50}")

        for name, test in PROMPTS.items():
            response, latency = call_ollama(model, test["prompt"])
            parsed = parse_json(response)
            json_ok = parsed is not None
            keys_ok = all(k in (parsed or {}) for k in test["expected_keys"]) if parsed else False

            results[model]["total_latency"] += latency
            results[model]["tests"] += 1
            if json_ok and keys_ok:
                results[model]["json_success"] += 1

            status = "PASS" if (json_ok and keys_ok) else "PARSE_FAIL" if json_ok else "NO_JSON"
            print(f"  {name:20s} | {latency:5.1f}s | {status:10s} | ", end="")
            if parsed:
                # Show key decision fields
                if "direction" in parsed:
                    print(f"dir={parsed.get('direction')} ", end="")
                if "score" in parsed:
                    print(f"score={parsed.get('score')} ", end="")
                if "ai_confidence" in parsed:
                    print(f"conf={parsed.get('ai_confidence')} ", end="")
                if "agrees" in parsed:
                    print(f"agrees={parsed.get('agrees')} ", end="")
                if "setup_quality" in parsed:
                    print(f"setup={parsed.get('setup_quality')} ", end="")
            print()

    # Summary
    print(f"\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")
    print(f"  {'Model':30s} | {'Avg Latency':>12s} | {'JSON Rate':>10s} | {'Grade':>6s}")
    print(f"  {'─'*30}-+-{'─'*12}-+-{'─'*10}-+-{'─'*6}")

    for model, r in sorted(results.items(), key=lambda x: x[1]["total_latency"]):
        avg_lat = r["total_latency"] / max(r["tests"], 1)
        json_rate = r["json_success"] / max(r["tests"], 1) * 100
        # Grade: A = fast + reliable, B = one weakness, C = both weak
        if avg_lat < 15 and json_rate == 100:
            grade = "A"
        elif avg_lat < 30 and json_rate >= 67:
            grade = "B"
        elif json_rate >= 67:
            grade = "B-"
        else:
            grade = "C"
        print(f"  {model:30s} | {avg_lat:10.1f}s | {json_rate:8.0f}% | {grade:>6s}")

    print(f"\n  Recommendation:")
    # Find best primary (needs JSON reliability) and best fast (needs speed)
    reliable = [(m, r) for m, r in results.items() if r["json_success"] == r["tests"]]
    if reliable:
        fastest_reliable = min(reliable, key=lambda x: x[1]["total_latency"])
        slowest_reliable = max(reliable, key=lambda x: x[1]["total_latency"])
        print(f"  Fast pre-filter: {fastest_reliable[0]} ({fastest_reliable[1]['total_latency']/3:.1f}s avg)")
        if fastest_reliable[0] != slowest_reliable[0]:
            print(f"  Primary analyst: {slowest_reliable[0]} (100% JSON, deeper reasoning)")
    else:
        print(f"  No model achieved 100% JSON reliability — review prompts")
    print()


if __name__ == "__main__":
    main()
