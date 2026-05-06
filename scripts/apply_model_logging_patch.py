#!/usr/bin/env python3
"""Patch IntelliRoute to log/show the selected provider model per request.

Run from the repository root:
    python scripts/apply_model_logging_patch_v2.py
or:
    python apply_model_logging_patch_v2.py
"""
from __future__ import annotations

from pathlib import Path
import re
import sys

ROOT = Path.cwd()
ROUTER = ROOT / "intelliroute" / "router" / "main.py"
GATEWAY = ROOT / "intelliroute" / "gateway" / "main.py"
FRONTEND = ROOT / "frontend" / "index.html"


def read(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"missing expected file: {path}")
    return path.read_text(encoding="utf-8")


def write_if_changed(path: Path, old: str, new: str) -> bool:
    if old == new:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def patch_router() -> bool:
    text = read(ROUTER)
    original = text

    if "<<<<<<<" in text or ">>>>>>>" in text:
        raise SystemExit(
            f"{ROUTER} still contains merge-conflict markers. Resolve those first."
        )

    # Add selected_model immediately after estimated_cost is computed.
    if "selected_model = str(data.get(\"model\") or info.model)" not in text:
        lines = text.splitlines(keepends=True)
        out: list[str] = []
        inserted = False
        for line in lines:
            out.append(line)
            if (
                not inserted
                and "estimated_cost" in line
                and "total_tokens" in line
                and "info.cost_per_1k_tokens" in line
            ):
                indent = line[: len(line) - len(line.lstrip())]
                out.append(f'{indent}selected_model = str(data.get("model") or info.model)\n')
                inserted = True
        if not inserted:
            raise SystemExit(
                "Could not find the estimated_cost line in intelliroute/router/main.py. "
                "Add selected_model manually after estimated_cost is calculated."
            )
        text = "".join(out)

    # Use selected_model in all success-path model fields.
    text = text.replace("model=info.model,", "model=selected_model,")

    # Insert the structured router log immediately before the success response.
    if '"model_selected"' not in text:
        lines = text.splitlines(keepends=True)
        out = []
        inserted = False
        for line in lines:
            if not inserted and "return CompletionResponse(" in line:
                indent = line[: len(line) - len(line.lstrip())]
                block = f'''{indent}log_event(\n{indent}    log,\n{indent}    "model_selected",\n{indent}    request_id=request_id,\n{indent}    provider=info.name,\n{indent}    model=selected_model,\n{indent}    intent=intent.value,\n{indent}    latency_ms=round(latency_ms, 2),\n{indent}    prompt_tokens=prompt_tokens,\n{indent}    completion_tokens=completion_tokens,\n{indent}    total_tokens=total_tokens,\n{indent}    estimated_cost_usd=round(estimated_cost, 6),\n{indent}    fallback_used=fallback_used or i > 0,\n{indent}    degraded=i > 0,\n{indent})\n'''
                out.append(block)
                inserted = True
            out.append(line)
        if not inserted:
            raise SystemExit(
                "Could not find return CompletionResponse(...) in intelliroute/router/main.py. "
                "Add the model_selected log manually before the successful return."
            )
        text = "".join(out)

    return write_if_changed(ROUTER, original, text)


def patch_gateway() -> bool:
    text = read(GATEWAY)
    original = text

    if "<<<<<<<" in text or ">>>>>>>" in text:
        raise SystemExit(
            f"{GATEWAY} still contains merge-conflict markers. Resolve those first."
        )

    if '"completion_served"' in text:
        return False

    # Most versions have: return CompletionResponse(**r.json())
    pattern = re.compile(r"^(?P<indent>\s*)return CompletionResponse\(\*\*r\.json\(\)\)\s*$", re.M)
    match = pattern.search(text)
    if not match:
        raise SystemExit(
            "Could not find `return CompletionResponse(**r.json())` in intelliroute/gateway/main.py. "
            "Use the manual gateway patch from the chat response."
        )

    indent = match.group("indent")
    replacement = f'''{indent}body = r.json()\n{indent}log_event(\n{indent}    log,\n{indent}    "completion_served",\n{indent}    trace_id=trace_id,\n{indent}    tenant=tenant,\n{indent}    provider=body.get("provider"),\n{indent}    model=body.get("model"),\n{indent}    latency_ms=body.get("latency_ms"),\n{indent}    total_tokens=body.get("total_tokens"),\n{indent}    estimated_cost_usd=body.get("estimated_cost_usd"),\n{indent}    fallback_used=body.get("fallback_used"),\n{indent})\n{indent}return CompletionResponse(**body)'''
    text = pattern.sub(replacement, text, count=1)

    return write_if_changed(GATEWAY, original, text)


def patch_frontend() -> bool:
    if not FRONTEND.exists():
        return False
    text = read(FRONTEND)
    original = text

    # Add model into assistant-message metadata.
    if "model: data.model," not in text:
        text = text.replace(
            "provider: data.provider,\n                    latency: data.latency_ms,",
            "provider: data.provider,\n                    model: data.model,\n                    latency: data.latency_ms,",
            1,
        )

    # Show model as a message badge.
    if "🤖 ${escapeHtml(meta.model)}" not in text:
        text = text.replace(
            "if (meta.provider) {\n                metaHtml += `<span class=\"meta-badge\">🔗 ${escapeHtml(meta.provider)}</span>`;\n            }\n            if (meta.latency) {",
            "if (meta.provider) {\n                metaHtml += `<span class=\"meta-badge\">🔗 ${escapeHtml(meta.provider)}</span>`;\n            }\n            if (meta.model) {\n                metaHtml += `<span class=\"meta-badge\">🤖 ${escapeHtml(meta.model)}</span>`;\n            }\n            if (meta.latency) {",
            1,
        )

    # Add model to the user response subtext.
    if "data.model ? ` · model ${data.model}` : ''" not in text:
        text = text.replace(
            "const ms = data.latency_ms != null ? `${data.latency_ms}ms` : null;\n                    handledSubText.textContent = ms\n                        ? `Automatically optimized for speed, quality, and cost · ${ms}`\n                        : 'Automatically optimized for speed, quality, and cost';",
            "const ms = data.latency_ms != null ? `${data.latency_ms}ms` : null;\n                    const model = data.model ? ` · model ${data.model}` : '';\n                    handledSubText.textContent = ms\n                        ? `Automatically optimized for speed, quality, and cost · ${ms}${model}`\n                        : `Automatically optimized for speed, quality, and cost${model}`;",
            1,
        )

    # Include model in the live ops route event.
    if "modelLine" not in text:
        text = text.replace(
            "const brownoutLine = bo && bo.is_degraded\n                    ? `Brownout: ${bo.reason} (q=${bo.queue_depth}, p95=${Math.round(bo.p95_latency_ms || 0)}ms)`\n                    : '';\n                addEvent('routing', `Route to ${data.provider} (${data.latency_ms}ms, $${(data.estimated_cost_usd || 0).toFixed(4)})${policyLine ? ' | ' + policyLine : ''}${brownoutLine ? ' | ' + brownoutLine : ''}`);",
            "const brownoutLine = bo && bo.is_degraded\n                    ? `Brownout: ${bo.reason} (q=${bo.queue_depth}, p95=${Math.round(bo.p95_latency_ms || 0)}ms)`\n                    : '';\n                const modelLine = data.model ? `Model: ${data.model}` : '';\n                addEvent('routing', `Route to ${data.provider} (${data.latency_ms}ms, $${(data.estimated_cost_usd || 0).toFixed(4)})${modelLine ? ' | ' + modelLine : ''}${policyLine ? ' | ' + policyLine : ''}${brownoutLine ? ' | ' + brownoutLine : ''}`);",
            1,
        )

    return write_if_changed(FRONTEND, original, text)


def main() -> int:
    changed = []
    if patch_router():
        changed.append(str(ROUTER.relative_to(ROOT)))
    if patch_gateway():
        changed.append(str(GATEWAY.relative_to(ROOT)))
    if patch_frontend():
        changed.append(str(FRONTEND.relative_to(ROOT)))

    if changed:
        print("patched files:")
        for p in changed:
            print(f"  - {p}")
    else:
        print("no changes needed; model logging/display patch appears to already be applied")

    print("\nnext commands:")
    print("  python -m compileall -q intelliroute scripts")
    print("  git diff")
    print("  git add intelliroute/router/main.py intelliroute/gateway/main.py frontend/index.html")
    print('  git commit -m "Log and display selected model per request"')
    print("  git push")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
