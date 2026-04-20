# workflow_manager.py
# ============================================================
#  Standalone Workflow Manager — Miro Integration
#  Import this in your main bot file with 3 lines.
# ============================================================

import os
import json
import re
import asyncio
import aiohttp
from datetime import datetime

# ============================================================
#  Config — reads from same .env as your main bot
# ============================================================

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
MIRO_ACCESS_TOKEN = os.getenv("MIRO_ACCESS_TOKEN", "")
MIRO_BOARD_ID = os.getenv("MIRO_BOARD_ID", "")
MIRO_BASE = "https://api.miro.com/v2"

WORKFLOW_FILE = os.path.join(DATA_DIR, "workflows.json")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.10.7:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

# ============================================================
#  State
# ============================================================

workflows_db: dict[str, dict] = {}
workflow_counter: int = 1

# ============================================================
#  Shape / Color Constants
# ============================================================

DIAGRAM_COLORS = {
    "start":    "#4CAF50",
    "end":      "#F44336",
    "process":  "#2196F3",
    "decision": "#FF9800",
    "default":  "#9E9E9E",
}

SHAPE_TYPES = {
    "start":    "round_rectangle",
    "end":      "round_rectangle",
    "process":  "rectangle",
    "decision": "rhombus",
    "default":  "rectangle",
}

TYPE_ICONS = {
    "start":    "🟢",
    "end":      "🔴",
    "process":  "🔷",
    "decision": "🔶",
}

# ============================================================
#  Helpers
# ============================================================

def _miro_enabled() -> bool:
    return bool(MIRO_ACCESS_TOKEN and MIRO_BOARD_ID)


def _miro_headers() -> dict:
    return {
        "Authorization": f"Bearer {MIRO_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _next_x_origin() -> int:
    """Each new workflow is offset on the board so they don't overlap."""
    return 2000 + (len(workflows_db) * 1000)


# ============================================================
#  Persistence
# ============================================================

def load_workflows():
    global workflows_db, workflow_counter
    try:
        with open(WORKFLOW_FILE, "r", encoding="utf-8") as f:
            workflows_db = json.load(f)
        if workflows_db:
            workflow_counter = max(int(k) for k in workflows_db.keys()) + 1
        print(f"[WORKFLOW] Loaded {len(workflows_db)} workflows.")
    except FileNotFoundError:
        workflows_db = {}
        print("[WORKFLOW] No workflow file found — starting fresh.")
    except Exception as e:
        print(f"[WORKFLOW] Load error: {e}")
        workflows_db = {}


def save_workflows():
    try:
        with open(WORKFLOW_FILE, "w", encoding="utf-8") as f:
            json.dump(workflows_db, f, indent=2)
    except Exception as e:
        print(f"[WORKFLOW] Save error: {e}")


# ============================================================
#  Ollama Helper (self-contained, no dependency on main bot)
# ============================================================

async def _ask_ollama(prompt: str, system: str = "") -> str | None:
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OLLAMA_URL,
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": full_prompt,
                    "stream": False,
                },
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                data = await resp.json()
                return data.get("response", "").strip()
    except Exception as e:
        print(f"[WORKFLOW/OLLAMA] Error: {e}")
        return None


def _clean_json(response: str) -> str:
    """Strip markdown fences and whitespace from AI response."""
    return response.strip().strip("```json").strip("```").strip()


def _validate_steps(steps: list) -> list:
    """
    Ensure the step list is valid:
    - First step is always 'start'
    - Last step is always 'end'
    - All branch target_index values are within bounds
    """
    if not steps or not isinstance(steps, list):
        return []
    
    if len(steps) == 0:
        return steps

    steps[0]["type"] = "start"
    steps[-1]["type"] = "end"

    for step in steps:
        for branch in step.get("branches", []):
            idx = branch.get("target_index", 0)
            try:
                idx = int(idx)
                branch["target_index"] = idx
            except (ValueError, TypeError):
                idx = len(steps) - 1
                branch["target_index"] = idx
            
            if idx < 0 or idx >= len(steps):
                branch["target_index"] = len(steps) - 1

    return steps


# ============================================================
#  Miro: Draw & Delete
# ============================================================

async def _miro_create_board(title: str) -> str:
    """
    Create a new Miro board for the workflow.
    Returns the new board ID.
    """
    if not _miro_enabled():
        return None
    
    headers = _miro_headers()
    board_payload = {
        "name": f"Workflow: {title}",
        "description": f"AI-generated workflow diagram for: {title}"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{MIRO_BASE}/boards", headers=headers, json=board_payload
        ) as resp:
            if resp.status in (200, 201):
                result = await resp.json()
                print(f"[WORKFLOW] New board created: {result['id']}")
                return result["id"]
            else:
                txt = await resp.text()
                print(f"[WORKFLOW] Board creation failed: {resp.status} {txt}")
                return None

async def _miro_create_diagram(
    title: str,
    steps: list[dict],
    x_start: int = 2000,
    y_start: int = 0,
    spacing_y: int = 160,
) -> dict:
    """
    Draw shapes + connectors on Miro from a step list.
    Returns { "shape_ids": [...], "connector_ids": [...], "board_id": "..." }
    """
    if not _miro_enabled():
        return {"error": "Miro not configured"}

    # Create a new board for this workflow
    board_id = await _miro_create_board(title)
    if not board_id:
        return {"error": "Failed to create Miro board"}

    shape_ids = []
    connector_ids = []
    headers = _miro_headers()
    base = f"{MIRO_BASE}/boards/{board_id}"

    async with aiohttp.ClientSession() as session:

        # --- Title text ---
        title_payload = {
            "data": {"content": f"<strong>{title}</strong>"},
            "position": {
                "x": x_start,
                "y": y_start - 100,
                "origin": "center",
            },
            "style": {
                "fontSize": "24",
                "textAlign": "center",
            },
        }
        async with session.post(
            f"{base}/texts", headers=headers, json=title_payload
        ) as resp:
            if resp.status not in (200, 201):
                txt = await resp.text()
                print(f"[WORKFLOW] Title create failed: {resp.status} {txt}")

        # --- Shapes ---
        for i, step in enumerate(steps):
            stype = step.get("type", "default")
            shape_payload = {
                "data": {
                    "content": step["label"],
                    "shape": SHAPE_TYPES.get(stype, "rectangle"),
                },
                "style": {
                    "fillColor":          DIAGRAM_COLORS.get(stype, DIAGRAM_COLORS["default"]),
                    "fontFamily":         "arial",
                    "fontSize":           "14",
                    "textAlign":          "center",
                    "textAlignVertical":  "middle",
                    "borderColor":        "#000000",
                    "borderWidth":        "2",
                    "borderStyle":        "normal",
                    "color":              "#ffffff",
                },
                "position": {
                    "x": x_start,
                    "y": y_start + (i * spacing_y),
                    "origin": "center",
                },
                "geometry": {
                    "width":  200,
                    "height": 80 if stype != "decision" else 100,
                },
            }
            async with session.post(
                f"{base}/shapes", headers=headers, json=shape_payload
            ) as resp:
                if resp.status in (200, 201):
                    result = await resp.json()
                    shape_ids.append(result["id"])
                    print(f"[WORKFLOW] Shape {i} created: {result['id']}")
                else:
                    txt = await resp.text()
                    print(f"[WORKFLOW] Shape {i} failed: {resp.status} {txt}")
                    shape_ids.append(None)

        # --- Connectors ---
        for i, step in enumerate(steps):
            if i >= len(shape_ids) or not shape_ids[i]:
                continue

            branches = step.get("branches")

            if branches:
                # Decision node → branching connectors
                for branch in branches:
                    target_idx = branch.get("target_index")
                    if (
                        target_idx is not None
                        and target_idx < len(shape_ids)
                        and shape_ids[target_idx]
                    ):
                        conn_payload = {
                            "startItem": {
                                "id":       shape_ids[i],
                                "position": {"x": "50%", "y": "100%"},
                            },
                            "endItem": {
                                "id":       shape_ids[target_idx],
                                "position": {"x": "50%", "y": "0%"},
                            },
                            "captions": [
                                {
                                    "content":  branch.get("label", ""),
                                    "position": "50%",
                                }
                            ],
                            "style": {
                                "strokeColor": "#000000",
                                "strokeWidth": "2",
                                "endStrokeCap": "stealth",
                            },
                        }
                        async with session.post(
                            f"{base}/connectors",
                            headers=headers,
                            json=conn_payload,
                        ) as resp:
                            if resp.status in (200, 201):
                                result = await resp.json()
                                connector_ids.append(result["id"])
                            else:
                                txt = await resp.text()
                                print(
                                    f"[WORKFLOW] Connector {i}→{target_idx} "
                                    f"failed: {resp.status} {txt}"
                                )
            else:
                # Linear → connect to next shape
                next_idx = i + 1
                if next_idx < len(shape_ids) and shape_ids[next_idx]:
                    conn_payload = {
                        "startItem": {
                            "id":       shape_ids[i],
                            "position": {"x": "50%", "y": "100%"},
                        },
                        "endItem": {
                            "id":       shape_ids[next_idx],
                            "position": {"x": "50%", "y": "0%"},
                        },
                        "style": {
                            "strokeColor":  "#000000",
                            "strokeWidth":  "2",
                            "endStrokeCap": "stealth",
                        },
                    }
                    async with session.post(
                        f"{base}/connectors",
                        headers=headers,
                        json=conn_payload,
                    ) as resp:
                        if resp.status in (200, 201):
                            result = await resp.json()
                            connector_ids.append(result["id"])

    return {"shape_ids": shape_ids, "connector_ids": connector_ids, "board_id": board_id}


async def _miro_delete_diagram(wf: dict):
    """Delete all Miro shapes + connectors for a workflow."""
    if not _miro_enabled():
        return

    board_id = wf.get("miro_board_id", MIRO_BOARD_ID)
    if not board_id:
        return

    headers = _miro_headers()
    base = f"{MIRO_BASE}/boards/{board_id}"

    async with aiohttp.ClientSession() as session:
        # Connectors first (they reference shapes)
        for conn_id in wf.get("miro_connector_ids", []):
            try:
                async with session.delete(
                    f"{base}/connectors/{conn_id}", headers=headers
                ) as resp:
                    print(f"[WORKFLOW] Deleted connector {conn_id}: {resp.status}")
            except Exception as e:
                print(f"[WORKFLOW] Connector delete error: {e}")

        # Then shapes
        for shape_id in wf.get("miro_shape_ids", []):
            try:
                async with session.delete(
                    f"{base}/shapes/{shape_id}", headers=headers
                ) as resp:
                    print(f"[WORKFLOW] Deleted shape {shape_id}: {resp.status}")
            except Exception as e:
                print(f"[WORKFLOW] Shape delete error: {e}")

    wf["miro_shape_ids"] = []
    wf["miro_connector_ids"] = []


async def _miro_redraw(wf_id: str) -> dict:
    """Delete old diagram and redraw from stored steps."""
    wf = workflows_db.get(wf_id)
    if not wf:
        return {"error": "Workflow not found"}

    await _miro_delete_diagram(wf)

    result = await _miro_create_diagram(
        title=wf["title"],
        steps=wf["steps"],
        x_start=wf.get("x_origin", 2000),
        y_start=wf.get("y_origin", 0),
    )

    wf["miro_shape_ids"]    = result.get("shape_ids", [])
    wf["miro_connector_ids"] = result.get("connector_ids", [])
    wf["miro_board_id"]      = result.get("board_id", "")
    save_workflows()
    return result


# ============================================================
#  AI: Generate + Edit Workflows
# ============================================================

async def ai_generate_workflow(description: str) -> list[dict] | None:
    """Ask Ollama to design a workflow from a natural language description."""
    system = """You are a workflow designer. Given a process description, create a workflow diagram.
Return ONLY a valid JSON array of steps. Each step:
  { "label": "short text (max 40 chars)", "type": "start|end|process|decision" }
Decision steps also have:
  "branches": [ {"label": "Yes/No", "target_index": <int>} ]

Rules:
- Always start with "start" and end with "end"
- 5 to 12 steps maximum
- Decision nodes must have exactly 2 branches
- target_index is 0-based index in the array
- Return ONLY the JSON array, no markdown, no explanation."""

    response = await _ask_ollama(f'Create a workflow for: "{description}"', system)
    if not response:
        print("[WORKFLOW] No response from Ollama")
        return None

    print(f"[WORKFLOW] Raw Ollama response: {response[:200]}...")
    response = _clean_json(response)
    print(f"[WORKFLOW] Cleaned response: {response[:200]}...")

    try:
        steps = json.loads(response)
        print(f"[WORKFLOW] Parsed JSON type: {type(steps)}")
        if isinstance(steps, list) and len(steps) >= 2:
            return _validate_steps(steps)
        else:
            print(f"[WORKFLOW] Invalid steps format: {type(steps)}")
    except json.JSONDecodeError as e:
        print(f"[WORKFLOW] JSON decode error: {e}")
        match = re.search(r"\[.*\]", response, re.DOTALL)
        if match:
            try:
                steps = json.loads(match.group())
                print(f"[WORKFLOW] Regex extracted and parsed: {type(steps)}")
                return _validate_steps(steps)
            except Exception as e2:
                print(f"[WORKFLOW] Regex parse error: {e2}")
    print("[WORKFLOW] Returning None")
    return None


async def ai_edit_workflow(wf_id: str, edit_request: str, author: str) -> dict:
    """
    Use Ollama to apply a natural language edit to an existing workflow.
    Returns { "success": True, "changes": "..." } or { "error": "..." }
    """
    wf = workflows_db.get(wf_id)
    if not wf:
        return {"error": f"Workflow #{wf_id} not found."}

    current_steps_json = json.dumps(wf["steps"], indent=2)

    system = """You are a workflow editor. You receive the current steps as JSON and an edit request.
Return ONLY a valid JSON object:
{
  "steps": [ ...complete modified step list... ],
  "changes": "short description of what changed"
}

Rules for steps:
- { "label": "...", "type": "start|end|process|decision" }
- Decision steps also have "branches": [{"label": "...", "target_index": <int>}]
- First step must always be type "start", last must be "end"
- target_index is 0-based, must be within array bounds
- Max 20 steps, labels under 50 chars
- Return ONLY the JSON object, no markdown, no explanation."""

    prompt = f"""Current workflow "{wf['title']}" (ID: {wf_id}):
{current_steps_json}

User ({author}) wants to: "{edit_request}"

Return the modified JSON:"""

    response = await _ask_ollama(prompt, system)
    if not response:
        return {"error": "AI unavailable. Try again."}

    response = _clean_json(response)

    try:
        result = json.loads(response)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
            except Exception:
                return {"error": "AI returned invalid JSON. Try rephrasing."}
        else:
            return {"error": "AI returned invalid JSON. Try rephrasing."}

    new_steps = result.get("steps")
    changes   = result.get("changes", "Modified workflow")

    if not new_steps or not isinstance(new_steps, list) or len(new_steps) < 2:
        return {"error": "AI produced an invalid workflow. Try rephrasing."}

    new_steps = _validate_steps(new_steps)

    # Save history + apply
    wf.setdefault("history", []).append({
        "action":    changes,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "by":        author,
        "snapshot":  wf["steps"].copy(),   # store previous steps for undo
    })
    wf["steps"] = new_steps
    save_workflows()

    # Redraw on Miro
    redraw = await _miro_redraw(wf_id)
    if "error" in redraw:
        return {"error": redraw["error"]}

    return {
        "success":    True,
        "changes":    changes,
        "step_count": len(new_steps),
    }


async def ai_undo_workflow(wf_id: str, author: str) -> dict:
    """Revert the last edit by restoring the previous snapshot."""
    wf = workflows_db.get(wf_id)
    if not wf:
        return {"error": f"Workflow #{wf_id} not found."}

    history = wf.get("history", [])
    if not history:
        return {"error": "Nothing to undo."}

    last = history[-1]
    snapshot = last.get("snapshot")
    if not snapshot:
        return {"error": "No snapshot available for undo."}

    # Restore
    wf["steps"] = snapshot
    history.pop()
    wf["history"] = history
    save_workflows()

    redraw = await _miro_redraw(wf_id)
    if "error" in redraw:
        return {"error": redraw["error"]}

    return {
        "success":  True,
        "changes":  f"Undid: {last['action']}",
        "step_count": len(snapshot),
    }


# ============================================================
#  Public API — called from your main bot file
# ============================================================

async def cmd_workflow_create(
    send_fn,            # async function: send_fn("message")
    description: str,
    author_name: str,
) -> None:
    """Create a new workflow from a description."""
    global workflow_counter

    await send_fn("🤖 Designing workflow with AI...")
    steps = await ai_generate_workflow(description)
    if not steps:
        await send_fn("❌ Couldn't generate a workflow. Try being more specific.")
        return

    x_origin = _next_x_origin()
    result = await _miro_create_diagram(
        title=description[:50],
        steps=steps,
        x_start=x_origin,
        y_start=0,
    )
    if "error" in result:
        await send_fn(f"❌ Miro error: {result['error']}")
        return

    wf_id = str(workflow_counter)
    workflows_db[wf_id] = {
        "title":               description[:50],
        "steps":               steps,
        "miro_shape_ids":      result.get("shape_ids", []),
        "miro_connector_ids":  result.get("connector_ids", []),
        "miro_board_id":       result.get("board_id", ""),
        "x_origin":            x_origin,
        "y_origin":            0,
        "created_by":          author_name,
        "created_at":          datetime.now().isoformat(timespec="seconds"),
        "history":             [{
            "action":    "created",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "by":        author_name,
            "snapshot":  [],
        }],
    }
    workflow_counter += 1
    save_workflows()

    board_id = result.get("board_id", MIRO_BOARD_ID)
    board_url = f"https://miro.com/app/board/{board_id}/"
    await send_fn(
        f"✅ Workflow **#{wf_id}** created: *{description[:50]}*\n"
        f"📊 {len(steps)} steps\n"
        f"🔗 {board_url}\n\n"
        f"💡 Edit it with `!wf_edit {wf_id} <your changes>`"
    )


async def cmd_workflow_edit(
    send_fn,
    wf_id: str,
    edit_request: str,
    author_name: str,
) -> None:
    """Edit an existing workflow."""
    if wf_id not in workflows_db:
        await send_fn(f"❌ Workflow #{wf_id} not found. Use `!wf_list` to see all.")
        return

    await send_fn(f"🔄 Editing workflow #{wf_id}...")
    result = await ai_edit_workflow(wf_id, edit_request, author_name)

    if result.get("success"):
        board_url = f"https://miro.com/app/board/{MIRO_BOARD_ID}/"
        await send_fn(
            f"✅ Workflow #{wf_id} updated!\n"
            f"📝 *{result['changes']}*\n"
            f"📊 {result['step_count']} steps\n"
            f"🔗 {board_url}"
        )
    else:
        await send_fn(f"❌ {result.get('error', 'Unknown error')}")


async def cmd_workflow_undo(send_fn, wf_id: str, author_name: str) -> None:
    """Undo the last edit."""
    if wf_id not in workflows_db:
        await send_fn(f"❌ Workflow #{wf_id} not found.")
        return

    await send_fn(f"↩️ Undoing last edit on workflow #{wf_id}...")
    result = await ai_undo_workflow(wf_id, author_name)

    if result.get("success"):
        board_url = f"https://miro.com/app/board/{MIRO_BOARD_ID}/"
        await send_fn(
            f"✅ {result['changes']}\n"
            f"📊 {result['step_count']} steps restored\n"
            f"🔗 {board_url}"
        )
    else:
        await send_fn(f"❌ {result.get('error')}")


async def cmd_workflow_list(send_fn) -> None:
    """List all workflows."""
    if not workflows_db:
        await send_fn("📭 No workflows yet. Use `!workflow <description>` to create one.")
        return

    lines = ["**📊 Saved Workflows**\n"]
    for wf_id, wf in workflows_db.items():
        step_count  = len(wf.get("steps", []))
        edit_count  = max(0, len(wf.get("history", [])) - 1)
        lines.append(
            f"**#{wf_id}** — {wf['title']}\n"
            f"   📊 {step_count} steps · ✏️ {edit_count} edits · "
            f"👤 {wf.get('created_by', '?')} · 📅 {wf.get('created_at', '')[:10]}"
        )
    await send_fn("\n".join(lines))


async def cmd_workflow_view(send_fn, wf_id: str) -> None:
    """Show workflow steps as text."""
    wf = workflows_db.get(wf_id)
    if not wf:
        await send_fn(f"❌ Workflow #{wf_id} not found.")
        return

    lines = [f"**📊 Workflow #{wf_id}: {wf['title']}**\n"]
    for i, step in enumerate(wf["steps"]):
        icon = TYPE_ICONS.get(step.get("type", "default"), "⬜")
        line = f"`{i}` {icon} {step['label']}"
        if step.get("branches"):
            branch_text = " | ".join(
                f"*{b['label']}* → step {b['target_index']}"
                for b in step["branches"]
            )
            line += f"\n     ↳ {branch_text}"
        lines.append(line)

    history = wf.get("history", [])
    if len(history) > 1:
        lines.append(f"\n**📜 Last {min(5, len(history)-1)} edits:**")
        for entry in history[-5:]:
            if entry["action"] == "created":
                continue
            lines.append(
                f"  • {entry['action']} — "
                f"{entry['by']} ({entry['timestamp'][:16]})"
            )

    await send_fn("\n".join(lines))


async def cmd_workflow_delete(send_fn, wf_id: str) -> None:
    """Delete a workflow and remove it from Miro."""
    wf = workflows_db.get(wf_id)
    if not wf:
        await send_fn(f"❌ Workflow #{wf_id} not found.")
        return

    await _miro_delete_diagram(wf)
    del workflows_db[wf_id]
    save_workflows()
    await send_fn(f"🗑️ Workflow #{wf_id} *{wf['title']}* deleted from Discord and Miro.")


async def cmd_workflow_redraw(send_fn, wf_id: str) -> None:
    """Force redraw a workflow on Miro."""
    if wf_id not in workflows_db:
        await send_fn(f"❌ Workflow #{wf_id} not found.")
        return

    await send_fn(f"🔄 Redrawing workflow #{wf_id} on Miro...")
    result = await _miro_redraw(wf_id)
    if "error" in result:
        await send_fn(f"❌ {result['error']}")
    else:
        board_url = f"https://miro.com/app/board/{MIRO_BOARD_ID}/"
        await send_fn(f"✅ Redrawn!\n🔗 {board_url}")


async def parse_workflow_intent(message_text: str, author_name: str) -> dict | None:
    """
    Check if the message is workflow-related.
    Returns intent dict or None if not workflow-related.
    """
    # Build context list of existing workflows
    if workflows_db:
        wf_list_str = "\n".join(
            f"  #{wf_id}: {wf['title']} ({len(wf['steps'])} steps)"
            for wf_id, wf in workflows_db.items()
        )
    else:
        wf_list_str = "  No workflows yet."

    system = """You are a workflow management assistant. Analyze if the user's message 
is about creating or managing workflow diagrams on Miro.

Return ONLY a valid JSON object with one of these actions:

1. Create new workflow:
   { "action": "workflow_create", "description": "what the workflow is about" }

2. Edit existing workflow:
   { "action": "workflow_edit", "id": "1", "edit": "what to change" }

3. Undo last edit:
   { "action": "workflow_undo", "id": "1" }

4. List all workflows:
   { "action": "workflow_list" }

5. View a workflow's steps:
   { "action": "workflow_view", "id": "1" }

6. Delete a workflow:
   { "action": "workflow_delete", "id": "1" }

7. Redraw a workflow on Miro:
   { "action": "workflow_redraw", "id": "1" }

8. Not workflow related:
   { "action": "not_workflow" }

RULES:
- If the user mentions "workflow", "diagram", "flowchart", "flow", "miro diagram" → it's workflow-related
- If they say "create", "make", "build", "generate" + workflow → workflow_create
- If they say "edit", "change", "update", "modify", "add step", "remove step", "rename" + workflow → workflow_edit
- If they say "undo", "revert", "go back" + workflow → workflow_undo
- If they say "list", "show", "all workflows" → workflow_list
- If they say "view", "see steps", "show steps" + workflow number → workflow_view
- If they say "delete", "remove" + workflow → workflow_delete
- If they say "redraw", "refresh" + workflow → workflow_redraw
- Extract workflow ID as a string (e.g. "1", "2")
- Return ONLY the JSON, no markdown, no explanation"""

    prompt = f"""Existing workflows:
{wf_list_str}

User ({author_name}) says: "{message_text}"

Return JSON:"""

    response = await _ask_ollama(prompt, system)
    if not response:
        return None

    response = _clean_json(response)

    try:
        intent = json.loads(response)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            try:
                intent = json.loads(match.group())
            except Exception:
                return None
        else:
            return None

    # If not workflow-related, return None so main bot handles it
    if intent.get("action") == "not_workflow":
        return None

    return intent