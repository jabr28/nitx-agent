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


def typical_run() -> str:
    results = sorted((ROOT / "evals" / "results").glob("*.json"))
    if not results:
        return "about 20 tool calls (to be measured)"
    data = json.loads(results[-1].read_text())
    full = [r for r in data if r["case"] == "clear_brief" and r["seconds"]]
    if not full:
        return "about 20 tool calls"
    r = full[0]
    return f"about {r['tool_calls']} tool calls, {r['poster_jobs']} poster job(s) and {r['seconds']:.0f} s end to end"


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
