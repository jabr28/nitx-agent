"""Turn agent webhook responses into a readable Markdown transcript.

    uv run python scripts/make_transcript.py run.json -o transcripts/01-weekend-sale.md --title "..."
    uv run python scripts/make_transcript.py evals/results/<file>.json --case poster_fails_then_recovers -o ...

Input is either one webhook response, or an eval results file plus --case.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re


def short(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + " …"


def redact(text: str) -> str:
    # Signed URLs are noise in a transcript; keep the path, drop the signature.
    return re.sub(r"(\.png)\?expires=\d+&sig=[0-9a-f]+", r"\1?…", text)


def pretty_observation(raw: str) -> str:
    """n8n wraps MCP results as [{"response": [...], "structuredContent": {...}}]; show the useful part."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    item = data[0] if isinstance(data, list) and data else data
    if isinstance(item, dict):
        if item.get("structuredContent") is not None:
            return json.dumps(item["structuredContent"], ensure_ascii=False, indent=1)
        if isinstance(item.get("response"), list):
            return "\n".join(c.get("text", "") for c in item["response"] if isinstance(c, dict))
        if "error" in item:
            return f"ERROR: {item['error']}"
    return json.dumps(data, ensure_ascii=False, indent=1)


def render_turn(turn: dict, brief: str | None, n: int) -> list[str]:
    out = [f"## Turn {n}", ""]
    if brief:
        out += ["**User brief**", "", "> " + brief.replace("\n", "\n> "), ""]
    out += [f"**Tool calls ({len(turn.get('steps', []))})**", ""]
    for s in turn.get("steps", []):
        tool = (s.get("tool") or "").split("__")[-1]
        args = {k: v for k, v in (s.get("input") or {}).items() if k != "tool"}
        flag = " ❌" if s.get("is_error") else ""
        out.append(f"<details><summary><code>{s['n']:>2}. {tool}</code>{flag}</summary>\n")
        out.append("```json\n" + redact(json.dumps(args, ensure_ascii=False, indent=1)) + "\n```")
        out.append("Result:\n```\n" + redact(short(pretty_observation(s.get("observation", "")), 1500)) + "\n```\n</details>\n")
    out += ["", "**Agent reply**", "", redact(turn.get("reply", "")), ""]
    out += ["**Verified by platform (read after the agent finished)**", "", "```", turn.get("verified_by_platform", ""), "```", ""]
    warnings = turn.get("warnings") or []
    out += ["**Verifier warnings:** " + ("; ".join(warnings) if warnings else "none"), ""]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--case")
    ap.add_argument("--title", default="Campaign Agent transcript")
    ap.add_argument("--brief", action="append", default=[], help="user message(s), in order")
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    data = json.loads(pathlib.Path(args.input).read_text())
    if args.case:
        result = next(r for r in data if r["case"] == args.case)
        turns = result["turns"]
        from run_evals import CASES  # type: ignore[import-not-found]

        case = next(c for c in CASES if c.id == args.case)
        briefs = [case.brief, *case.follow_ups]
        header = [f"Eval case `{args.case}`: **{'PASS' if result['passed'] else 'FAIL'}**", ""]
        header += [f"- {'✓' if ok else '✗'} {name}" for name, ok in result["checks"]] + [""]
    else:
        turns = data if isinstance(data, list) else [data]
        briefs = args.brief
        header = []

    lines = [f"# {args.title}", ""]
    if args.note:
        lines += [args.note, ""]
    lines += [f"Campaign `{turns[-1].get('campaign_id')}` · model `deepseek-chat` via n8n · tools over MCP", ""]
    lines += header
    for i, turn in enumerate(turns, 1):
        lines += render_turn(turn, briefs[i - 1] if i - 1 < len(briefs) else None, i)
    pathlib.Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.output).write_text("\n".join(lines))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "evals"))
    main()
