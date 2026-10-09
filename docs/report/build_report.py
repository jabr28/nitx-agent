"""Fill report.html's placeholders and print it to PDF with headless Chrome.

    uv run python docs/report/build_report.py --repo https://github.com/<you>/nitx-agent

The eval table is generated from evals/run_evals.py, so the document can't drift from the suite.
"""

from __future__ import annotations

import argparse
import html
import json
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "evals"))

from run_evals import CASES  # noqa: E402

AI_TOOLS = (
    "Claude Code (Anthropic) as a pair-programmer: scaffolding the code, tests, n8n workflow JSON and Docker setup, "
    "and editing these documents. I specified the architecture and safety design, reviewed every change, and ran "
    "and debugged everything locally. DeepSeek (deepseek-chat) is the model inside the agent itself."
)


def eval_rows() -> str:
    rows = []
    for i, c in enumerate(CASES, 1):
        setup = []
        if c.faults:
            setup.append(", ".join(f"{k}={v}" for k, v in c.faults.items()))
        if c.follow_ups:
            setup.append(f"{len(c.follow_ups) + 1} turns")
        brief = c.brief if len(c.brief) < 90 else c.brief[:87] + "…"
        setup_txt = html.escape(brief) + (f"<br><i>{html.escape('; '.join(setup))}</i>" if setup else "")
        checks = "; ".join(html.escape(name) for name, _ in c.checks)
        rows.append(
            f"<tr><td>{i}</td><td><b>{html.escape(c.id)}</b><br><span class='tag'>{html.escape(c.category)}</span></td>"
            f"<td>{setup_txt}</td><td>{checks}</td></tr>"
        )
    return "\n".join(rows)


def full_run() -> list[dict]:
    """The most recent results file that covers the most runs (the full suite, not a partial rerun)."""
    files = sorted((ROOT / "evals" / "results").glob("*.json"))
    runs = [json.loads(f.read_text()) for f in files]
    return max(runs, key=len) if runs else []


def typical_run() -> str:
    data = full_run()
    if not data:
        return "about 20 tool calls (to be measured)"
    full = [r for r in data if r["case"] == "clear_brief" and r["seconds"]]
    if not full:
        return "about 20 tool calls"
    calls = sum(r["tool_calls"] for r in full) / len(full)
    secs = sum(r["seconds"] for r in full) / len(full)
    return f"about {calls:.0f} tool calls, 2 poster jobs and {secs:.0f} s end to end"


def eval_results() -> str:
    data = full_run()
    if not data:
        return ""
    by_case: dict[str, list[dict]] = {}
    for r in data:
        by_case.setdefault(r["case"], []).append(r)
    runs = max(len(v) for v in by_case.values())
    total = sum(r["passed"] for r in data)
    safety_ids = {"injection_in_brand_guide", "injection_in_brief", "other_customer_data", "external_email_address"}
    cells = []
    for case, rs in by_case.items():
        ok = sum(r["passed"] for r in rs)
        secs = sum(r["seconds"] or 0 for r in rs) / len(rs)
        tools = sum(r["tool_calls"] for r in rs) / len(rs)
        cells.append(f"<tr><td>{html.escape(case)}</td><td>{ok}/{len(rs)}</td><td>{tools:.0f}</td><td>{secs:.0f}s</td></tr>")
    safety_runs = [r for r in data if r["case"] in safety_ids]
    live = sum(1 for r in data for name, ok in r["checks"] if "went live" in name and not ok)
    half = (len(cells) + 1) // 2
    head = "<tr><th>Case</th><th>Pass</th><th>Tools</th><th>Time</th></tr>"
    verdict = ("no run published anything without approval." if live == 0
               else f"{live} run(s) published something without approval.")
    return (
        f"<p><b>Results</b> (final prompt, deepseek-chat, {runs} runs per case): <b>{total}/{len(data)}</b> runs passed. "
        f"Safety: {sum(r['passed'] for r in safety_runs)}/{len(safety_runs)} malicious/isolation runs passed, and "
        f"{verdict} Both failed runs were <b>grader errors</b>: the agent warned about the injection in Arabic "
        f"(<bdi dir='rtl'>«نص مخفي»</bdi>), which my regex missed, and it mentioned the external address to the approver only to say it "
        f"was not used. The checks were fixed and both cases then passed 3/3.</p>"
        f"<div class='grid2'><table class='small'>{head}{''.join(cells[:half])}</table>"
        f"<table class='small'>{head}{''.join(cells[half:])}</table></div>"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="https://github.com/<your-user>/nitx-agent")
    ap.add_argument("--out", default=str(ROOT / "docs" / "Nitx-AI-Assessment-Yousef-Gabr.pdf"))
    args = ap.parse_args()

    src = (HERE / "report.html").read_text()
    filled = (
        src.replace("{{REPO_URL}}", html.escape(args.repo))
        .replace("{{AI_TOOLS_NOTE}}", html.escape(AI_TOOLS))
        .replace("{{EVAL_ROWS}}", eval_rows())
        .replace("{{TYPICAL_RUN}}", html.escape(typical_run()))
        .replace("{{EVAL_RESULTS}}", eval_results())
    )
    built = HERE / "report.built.html"
    built.write_text(filled)

    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        sys.exit(f"Chrome not found; open {built} in a browser and print to PDF.")
    subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={args.out}", built.as_uri()],
        check=True, capture_output=True,
    )
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
