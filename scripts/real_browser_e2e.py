"""Real-model browser end-to-end check (needs the app running with a real LLM on 127.0.0.1:8000).

Drives the actual page (textarea + Run) in installed Google Chrome via Playwright, then independently
verifies, per task: the read-back snapshot against expected values fixed BEFORE submission (exact
string and UTF-8 byte count), the rendered DOM text, and that `docker ps -a` shows no container left
for that task in any state (running, exited or created). Records a video and full-page screenshots.

Playwright is a test-only tool, deliberately not a project dependency; install it in a separate venv:
    uv venv pwvenv && uv pip install --python pwvenv/Scripts/python.exe playwright==1.49.1
    pwvenv\\Scripts\\python.exe scripts/real_browser_e2e.py
"""
import asyncio
import json
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8000"
OUT = Path(__file__).resolve().parent.parent / "docs" / "evidence"
TERMINAL = ("succeeded", "failed", "timeout")


def leftover_containers(task_id):
    out = subprocess.run(["docker", "ps", "-a", "--filter", f"label=agent-task={task_id}", "--format", "{{.ID}} {{.Status}}"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return out.splitlines()


async def submit(page, text):
    await page.evaluate("sessionStorage.clear()")
    await page.fill("#request", text)
    await page.click("#run")
    await page.wait_for_function("() => sessionStorage.getItem('mca-task-id')", timeout=15000)


async def finish(page):
    await page.wait_for_function(f"() => {list(TERMINAL)}.includes(document.getElementById('status').textContent)",
                                 timeout=120000, polling=250)
    await page.wait_for_timeout(1000)
    return await page.evaluate("""async () => {
        const id = sessionStorage.getItem('mca-task-id');
        const snap = await fetch('/tasks/' + id, {cache: 'no-store'}).then(r => r.json());
        const dom = [...document.querySelectorAll('#files article.file')].map(a => {
            const pre = a.querySelector('pre.content');
            return {path: a.querySelector('.path').textContent,
                    text: pre.firstChild && pre.firstChild.nodeType === 3 ? pre.firstChild.nodeValue : '',
                    element_children: [...pre.children].map(c => c.tagName)};
        });
        return {snap, dom, dom_status: document.getElementById('status').textContent,
                dom_cleanup: document.getElementById('cleanup').textContent, xss: window.__xss ?? null,
                health_text: document.getElementById('health').textContent};
    }""")


def verify(name, request, expected, result, exact_required=True):
    snap, dom = result["snap"], {d["path"]: d for d in result["dom"]}
    files = {f["path"]: f for f in snap["files"]}
    checks = []
    for path, content in expected.items():
        f, d = files.get(path), dom.get(path)
        checks.append({
            "path": path, "expected": content, "expected_utf8_bytes": len(content.encode()),
            "actual": f["content"] if f else None, "actual_size_bytes": f["size_bytes"] if f else None,
            "exact_match": bool(f) and f["content"] == content and f["size_bytes"] == len(content.encode()),
            "read_back": bool(f) and f["read_back"] and not f["truncated"] and not f["decode_replaced"],
            "dom_text_match": bool(d) and d["text"] == content, "dom_element_children": d["element_children"] if d else None})
    leftovers = leftover_containers(snap["task_id"])
    ok = (snap["status"] == "succeeded" and snap["cleanup"]["status"] == "done" and not leftovers
          and result["dom_status"] == snap["status"] and result["xss"] is None
          and set(files) == set(expected)
          and all(c["read_back"] and c["dom_text_match"] and not c["dom_element_children"] for c in checks)
          and (all(c["exact_match"] for c in checks) or not exact_required))
    return {"scenario": name, "real_model": True, "request": request, "task_id": snap["task_id"],
            "container_id": snap["container_id"], "status": snap["status"],
            "execution_status": snap["execution_status"], "cleanup": snap["cleanup"], "error": snap["error"],
            "unexpected_files": sorted(set(files) - set(expected)), "checks": checks,
            "leftover_containers_any_state": leftovers, "xss_flag": result["xss"],
            "health_text_after": result["health_text"],
            "agent_message": snap["agent_message"],
            "events": [f'{e["at"][11:23]} {e["code"]} {e["message"] if e["code"] == "PHASE_CHANGED" else ""}'.strip()
                       for e in snap["events"]],
            "passed": ok}


def tag(n=4):
    return secrets.token_hex(n)


async def run_one(page, name, request, expected, exact_required=True):
    await submit(page, request)
    result = await finish(page)
    await page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    record = verify(name, request, expected, result, exact_required)
    # Single task: the header must already show the freed slot, not a stale count.
    record["health_slots_freed"] = "slots in use 0/" in record["health_text_after"]
    record["passed"] = record["passed"] and record["health_slots_freed"]
    print(json.dumps({k: record[k] for k in ("scenario", "status", "container_id", "passed")}, ensure_ascii=False), flush=True)
    return record


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    records = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        # bypass_csp: Playwright waits need eval; it also makes the XSS check stricter (textContent alone must hold).
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, bypass_csp=True,
                                            record_video_dir=str(OUT / "video"), record_video_size={"width": 1280, "height": 900})
        page = await context.new_page()
        await page.goto(BASE)
        await page.wait_for_function("() => document.getElementById('health').textContent.startsWith('Backend ready')")
        health = await page.evaluate("fetch('/health').then(r => r.json())")
        print("health", health, flush=True)

        # 1. Original task, then explicit exact content (13 bytes, no newline).
        records.append(await run_one(page, "s1a-original", "Create hello.txt in the workspace with the content Hello, world!",
                                     {"hello.txt": "Hello, world!"}))
        records.append(await run_one(page, "s1b-exact",
            "Create hello.txt whose content is exactly the 13 characters Hello, world! — no trailing newline, no quotes, no other characters.",
            {"hello.txt": "Hello, world!"}))

        # 2. Random file name and marker, fixed before submission.
        for i in range(2):
            name, marker = f"rand-{tag()}.txt", f"MARK-{tag(8)}"
            records.append(await run_one(page, f"s2-random-{i + 1}",
                f"Create a file named {name} whose entire content is exactly {marker} with no trailing newline and nothing else.",
                {name: marker}))

        # 3. Two directories, Chinese, multi-line.
        m = tag()
        expected = {f"docs/说明-{m}.txt": f"第一行：沙箱测试 {m}\n第二行：中文内容\n第三行：结束",
                    f"data/nested/notes-{m}.md": f"# 笔记 {m}\n- item one\n- 项目二"}
        spec = "\n".join(f"- path {p} with content equal to the JSON string {json.dumps(c, ensure_ascii=False)}" for p, c in expected.items())
        records.append(await run_one(page, "s3-nested-multi",
            "Create these two files. Decode each JSON string literal and write its exact characters "
            f"(real newlines where \\n appears, no trailing newline):\n{spec}", expected))

        # 4. Quotes, backslashes, newlines, HTML; must render as text and never execute.
        m = tag()
        content = (f"He said \"hi\" & 'bye' {m}\nPath: C:\\temp\\new\\x.txt and \\n literal\n"
                   "<b>bold</b><img src=x onerror=\"window.__xss=1\"><script>window.__xss=2</script>")
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), asyncio.ensure_future(d.dismiss())))
        records.append(await run_one(page, "s4-special-chars",
            f"Create the file special-{m}.html whose content equals the JSON string {json.dumps(content)} after JSON decoding "
            "(exact characters, real newlines for \\n escapes, keep every backslash that the JSON decodes to, no trailing newline).",
            {f"special-{m}.html": content}))
        records[-1]["dialogs"] = dialogs

        # 5. Two concurrent tasks (separate tabs), same file name, different markers.
        second = await context.new_page()
        await second.goto(BASE)
        pages, markers = [page, second], [f"ISO-{tag(8)}", f"ISO-{tag(8)}"]
        await asyncio.gather(*(p.evaluate("sessionStorage.clear()") for p in pages))
        for p, mk in zip(pages, markers):
            await p.fill("#request", f"Create shared.txt whose entire content is exactly {mk} with no trailing newline.")
        await asyncio.gather(*(p.click("#run") for p in pages))
        await asyncio.gather(*(p.wait_for_function("() => sessionStorage.getItem('mca-task-id')") for p in pages))
        results = await asyncio.gather(*(finish(p) for p in pages))
        pair = []
        for i, (p, mk, res) in enumerate(zip(pages, markers, results)):
            await p.screenshot(path=str(OUT / f"s5-concurrent-{i + 1}.png"), full_page=True)
            pair.append(verify(f"s5-concurrent-{i + 1}", f"shared.txt = {mk}", {"shared.txt": mk}, res))
        ids = [r["container_id"] for r in pair]
        windows = [(r["events"][0][:12], r["events"][-1][:12]) for r in pair]
        overlap = windows[0][0] < windows[1][1] and windows[1][0] < windows[0][1]
        for r in pair:
            r["distinct_containers"] = len(set(ids)) == 2
            r["executions_overlapped"] = overlap
            r["passed"] = r["passed"] and len(set(ids)) == 2 and overlap
            print(json.dumps({k: r[k] for k in ("scenario", "status", "container_id", "passed")}), flush=True)
        records.extend(pair)

        await second.close()
        await page.close()
        await context.close()
        await browser.close()

    report = {"run_at": datetime.now(timezone.utc).isoformat(), "model": health.get("model"), "records": records,
              "project_containers_after_any_state": subprocess.run(
                  ["docker", "ps", "-a", "--filter", "label=mca-project=minimal-coding-agent", "--format", "{{.ID}} {{.Status}}"],
                  capture_output=True, text=True, check=True).stdout.strip().splitlines()}
    (OUT / "real-browser-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [r["scenario"] for r in records if not r["passed"]]
    print("FAILED:" if failed else "ALL PASSED", failed or "", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
