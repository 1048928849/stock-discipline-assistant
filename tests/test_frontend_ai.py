import json
import subprocess
from pathlib import Path


APP_JS = Path(__file__).parents[1] / "app" / "static" / "app.js"


def render_ai(item):
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function evidenceIds")
    end = source.index("function renderOneClick", start)
    functions = source[start:end]
    script = (
        "const esc=value=>String(value??'');\n"
        + functions
        + "\nprocess.stdout.write(aiExplanation("
        + json.dumps(
            {
                "status": "success",
                "provider": "test",
                "model": "test",
                "result": {
                    "schema_version": "2.0",
                    "ai_summaries": [item],
                    "ai_inferences": [],
                    "supporting_evidence": [],
                    "opposing_evidence": [],
                    "risk_events": [],
                    "conflicts": [],
                    "missing_data": [],
                },
            },
            ensure_ascii=False,
        )
        + "));"
    )
    return subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout


def test_ai_success_renders_evidence_ids_in_browser():
    html = render_ai(
        {
            "topic": "research",
            "content": "supported conclusion",
            "evidence_ids": ["evidence:1"],
            "confidence": "high",
        }
    )
    assert "evidence:1" in html


def test_ai_legacy_source_ids_remains_compatible():
    html = render_ai(
        {
            "topic": "legacy",
            "content": "legacy conclusion",
            "source_ids": ["legacy:1"],
            "confidence": "medium",
        }
    )
    assert "legacy:1" in html


def test_ai_missing_evidence_ids_does_not_crash():
    assert "supported conclusion" in render_ai(
        {
            "topic": "research",
            "content": "supported conclusion",
            "evidence_ids": "invalid",
            "confidence": "low",
        }
    )
