"""
Per-turn conversational latency report.

Reads ai-agent "turn latency" JSON log lines on stdin and summarises the gap
the prospect actually experiences: how long they sit in silence after they
finish talking before hearing anything back. That number — not whether the
watchdog fired — is what decides whether the call feels conversational.

Usage (via the wrapper):  scripts/turn_latency_report.sh [since]
"""
from __future__ import annotations

import json
import sys

TARGET_MS = 1000


def pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round(p / 100 * len(s) + 0.5)) - 1))
    return s[k]


def fmt(v: float | None) -> str:
    return "    n/a" if v is None else f"{v:6.0f}ms"


def main() -> int:
    turns = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        # Log lines may carry a compose prefix before the JSON object.
        start = line.find("{")
        if start == -1:
            continue
        try:
            turns.append(json.loads(line[start:]))
        except json.JSONDecodeError:
            continue

    if not turns:
        print("No turn-latency records found in this window.")
        print("(Place a call, then re-run. For an older call widen the window, e.g. `2h`.)")
        return 0

    completed = [t for t in turns if t.get("outcome") == "completed"]
    first_audio = [t["firstAudioPacketSentMs"] for t in turns
                   if t.get("firstAudioPacketSentMs") is not None]
    totals = [t["totalMs"] for t in turns if t.get("totalMs") is not None]
    tool_turns = [t for t in turns if t.get("toolName")]

    print()
    print(f"TURN LATENCY — {len(turns)} turns "
          f"({len(completed)} completed, {len(turns) - len(completed)} interrupted)")
    print("=" * 66)
    print()
    print("SILENCE AFTER PROSPECT STOPS TALKING -> first audio back")
    print(f"  target: under {TARGET_MS}ms when no tool is needed")
    print(f"    turns measured : {len(first_audio)}")
    if first_audio:
        avg = sum(first_audio) / len(first_audio)
        over = [v for v in first_audio if v > TARGET_MS]
        print(f"    average        : {fmt(avg)}")
        print(f"    p95            : {fmt(pct(first_audio, 95))}")
        print(f"    max            : {fmt(max(first_audio))}")
        print(f"    min            : {fmt(min(first_audio))}")
        print(f"    over {TARGET_MS}ms    : {len(over)}/{len(first_audio)} turns")
    print()

    if totals:
        print("FULL TURN (speech end -> assistant finished speaking)")
        print(f"    average        : {fmt(sum(totals) / len(totals))}")
        print(f"    p95            : {fmt(pct(totals, 95))}")
        print(f"    max            : {fmt(max(totals))}")
        print()

    # "It hears me seconds after I spoke" is a transcription-lag complaint, and
    # it is a different fault from a slow model. Partial vs final separates a
    # transcriber slow to START from one slow to FINISH.
    partials = [t["firstTranscriptPartialMs"] for t in turns
                if t.get("firstTranscriptPartialMs") is not None]
    finals = [t["transcriptCompletedMs"] for t in turns
              if t.get("transcriptCompletedMs") is not None]
    print("RECOGNISING THE PROSPECT'S SPEECH (after they stop talking)")
    if not partials and not finals:
        print("    no transcription timings recorded — input transcription may be off,")
        print("    which also means slot captures cannot be verified against speech")
    else:
        print(f"    first partial  : {fmt(pct(partials, 50))} median   "
              f"{fmt(pct(partials, 95))} p95")
        print(f"    final          : {fmt(pct(finals, 50))} median   "
              f"{fmt(pct(finals, 95))} p95")
    print()

    # Does she actually stop when talked over? Measured from the prospect's
    # voice being detected to the queued audio being cut.
    flushes = [t["bargeInToFlushMs"] for t in turns
               if t.get("bargeInToFlushMs") is not None]
    print(f"INTERRUPTIONS: {len(flushes)}")
    if flushes:
        print(f"    voice detected -> queued audio cut : {fmt(pct(flushes, 50))} median   "
              f"{fmt(max(flushes))} max")
    print()

    silent_tools = [
        t for t in tool_turns
        if t.get("functionCallDetectedMs") is not None
        and (t.get("firstAudioPacketSentMs") is None
             or t["functionCallDetectedMs"] < t["firstAudioPacketSentMs"])
    ]
    print(f"TOOL TURNS: {len(tool_turns)}   silent (nothing said before the tool ran): "
          f"{len(silent_tools)}")
    if silent_tools:
        print("  The prospect heard dead air while these ran:")
        for t in silent_tools[:10]:
            name = t.get("toolName") or "-"
            at = fmt(t.get("functionCallDetectedMs"))
            print(f"    - {name}: called at {at}, no speech first")
    print()

    print("SLOWEST TURNS (ms after the prospect stopped talking)")
    print("-" * 66)
    worst = sorted(
        turns,
        key=lambda t: t.get("firstAudioPacketSentMs") or t.get("totalMs") or 0,
        reverse=True,
    )[:5]
    for t in worst:
        name = t.get("toolName") or "-"
        print(f"  tool={name}  outcome={t.get('outcome')}")
        print(f"    speech started    {fmt(t.get('speechStartedMs'))}")
        print(f"    transcript first  {fmt(t.get('firstTranscriptPartialMs'))}")
        print(f"    transcript done   {fmt(t.get('transcriptCompletedMs'))}")
        print(f"    response created  {fmt(t.get('responseCreatedMs'))}")
        print(f"    first text token  {fmt(t.get('firstTranscriptDeltaMs'))}")
        print(f"    FIRST AUDIO OUT   {fmt(t.get('firstAudioPacketSentMs'))}   <- perceived delay")
        if t.get("functionCallDetectedMs") is not None:
            print(f"    tool call seen    {fmt(t.get('functionCallDetectedMs'))}")
            print(f"    tool exec took    {fmt(t.get('toolExecDurationMs'))}")
        print(f"    turn done         {fmt(t.get('responseDoneMs'))}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
