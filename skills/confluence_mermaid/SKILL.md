# Confluence Mermaid Diagram Skill

## Role
Act as a Confluence Macro Expert who knows exactly how to embed Mermaid diagrams into Confluence pages so they render correctly using the **Mermaid Diagrams for Confluence** (`mermaid-cloud`) plugin.

---

## Critical Knowledge — How Mermaid Macros Work in Confluence

### The ONLY working format (reverse-engineered from live example page 4494934164):

The mermaid diagram content MUST be embedded inside the `data-parameters` attribute as `macroParams.diagramContent.value`.

**Exact HTML structure:**
```html
<div
  data-type="extension"
  data-extension-key="mermaid-cloud"
  data-extension-type="com.atlassian.confluence.macro.core"
  data-layout="default"
  data-local-id="{UUID}"
  data-parameters="{ESCAPED_JSON}">
</div>
```

**The `data-parameters` JSON (before HTML escaping):**
```json
{
  "macroParams": {
    "diagramContent": {
      "value": "sequenceDiagram\n    participant C as Client\n    C->>S: Request"
    }
  },
  "macroMetadata": {
    "macroId": { "value": "{SAME_UUID}" },
    "schemaVersion": { "value": "1" },
    "placeholder": [{
      "type": "icon",
      "data": { "url": "https://mermaid.stratus-addons.com/images/mermaid144.png" }
    }],
    "title": "Mermaid Diagrams for Confluence"
  }
}
```

---

## Rules — ALWAYS FOLLOW

### Rule 1 — Arrow encoding
Inside `data-parameters` value, ALL mermaid arrows MUST be HTML-encoded:
| Raw mermaid | Inside data-parameters |
|---|---|
| `C->>S:` | `C-&gt;&gt;S:` |
| `S-->>C:` | `S--&gt;&gt;C:` |
| `A->B:` | `A-&gt;B:` |

### Rule 2 — Newlines
Newlines in mermaid content MUST be `\n` (escaped backslash-n) inside the JSON value.

### Rule 3 — Quotes in JSON
The entire `data-parameters` attribute value MUST have `"` replaced with `&quot;` since it sits inside an HTML attribute.

### Rule 4 — UUID
Each mermaid div needs a unique UUID for `data-local-id` AND `macroMetadata.macroId.value`. Generate with `str(uuid.uuid4())`.

### Rule 5 — NEVER use pre/code blocks
Do NOT put mermaid content in a separate `<pre><code class="language-mermaid">` block. It will NOT render. The content MUST be inside `data-parameters`.

### Rule 6 — DO NOT touch existing page content
When adding diagrams to an existing page, ALWAYS read the full page first, preserve ALL existing HTML, and only append/insert the new mermaid divs.

### Rule 7 — MCP tool strips diagramContent
The `updateConfluencePage` MCP tool silently strips `diagramContent.value` when passed inline. To avoid this, build the full HTML body in a local Python script, save to `temp/confluence_body.html`, read it back, then pass to the update tool.

---

## Step-by-Step Process

### Step 1 — Read the target page first
Always read before writing. Get `body` and current `version.number`.

### Step 2 — Build mermaid div in Python

```python
import json, uuid

def make_mermaid_div(diagram_text: str) -> str:
    local_id = str(uuid.uuid4())

    params = {
        "macroParams": {
            "diagramContent": {
                "value": diagram_text
            }
        },
        "macroMetadata": {
            "macroId": {"value": local_id},
            "schemaVersion": {"value": "1"},
            "placeholder": [{
                "type": "icon",
                "data": {"url": "https://mermaid.stratus-addons.com/images/mermaid144.png"}
            }],
            "title": "Mermaid Diagrams for Confluence"
        }
    }

    # Step A: JSON encode (handles newlines and backslashes)
    params_json = json.dumps(params, ensure_ascii=False)

    # Step B: HTML-encode quotes for the HTML attribute
    params_attr = params_json.replace('"', '&quot;')

    # Step C: HTML-encode >> arrows inside the attribute
    params_attr = params_attr.replace('>>', '&gt;&gt;')

    return (
        f'<div data-type="extension" '
        f'data-extension-key="mermaid-cloud" '
        f'data-extension-type="com.atlassian.confluence.macro.core" '
        f'data-layout="default" '
        f'data-local-id="{local_id}" '
        f'data-parameters="{params_attr}">'
        f'\n</div>'
    )
```

### Step 3 — Append to existing body
```python
new_section = f"""
<hr />
<h2>Section Title</h2>
<h3>Diagram 1</h3>
{make_mermaid_div(diagram1_text)}
<h3>Diagram 2</h3>
{make_mermaid_div(diagram2_text)}
"""
full_body = existing_body + new_section
```

### Step 4 — Save to temp file and read back (CRITICAL)
```python
# CRITICAL: save to file first to avoid MCP stripping content
with open('temp/confluence_body.html', 'w') as f:
    f.write(full_body)

with open('temp/confluence_body.html') as f:
    body_to_send = f.read()
```

### Step 5 — Update the page
```python
updateConfluencePage(
    cloudId=cloudId,
    pageId=pageId,
    title=page_title,
    body=body_to_send,
    contentFormat="html",
    version=current_version + 1
)
```

### Step 6 — ALWAYS verify after update
Read the page back and check `diagramContent.value` is NOT empty:
```python
updated = getConfluencePage(cloudId, pageId, contentFormat="html")
if 'diagramContent&quot;:{&quot;value&quot;:&quot;sequenceDiagram' in updated['body']:
    print('SUCCESS: Diagram content saved correctly')
else:
    print('FAILED: diagramContent is empty — MCP stripped it')
```
If empty after verification — DO NOT retry more than once. Tell the user and provide the raw mermaid text for manual paste.

---

## Mermaid Sequence Diagram Syntax Reference

```
sequenceDiagram
    autonumber
    participant A as Display Name
    participant B as Display Name

    A->>B: synchronous call
    B-->>A: return/response

    alt condition true
        A->>B: call
        B-->>A: response
    else condition false
        A->>B: other call
    end

    Note over A,B: multi-participant note
    Note over A: single participant note
```

---

## Common Mistakes — Learned from Real Failures

| Mistake | Correct Approach |
|---|---|
| Putting mermaid in `<pre><code>` | Put in `data-parameters.diagramContent.value` |
| Using raw `>>` in data-parameters | HTML-encode to `&gt;&gt;` |
| Not reading page before updating | Always read first, preserve existing content |
| Claiming success without verifying | Always read back and check `diagramContent` is not empty |
| Touching pages not explicitly asked to edit | Only edit the target page |
| Assuming MCP preserved the content | Always verify — MCP silently strips complex macro params |
| Passing body inline in large tool calls | Save to temp file first, read back, then pass |
