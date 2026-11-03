import ast
import json
import operator
import os
import re

import anthropic
from bs4 import BeautifulSoup

from .db import connect

MODEL = os.environ.get("ANSWER_MODEL", "claude-haiku-5-5")

SEARCH = """
SELECT page_no, html FROM pages, to_tsquery('english', %s) AS q
WHERE doc_id = %s AND search @@ q
ORDER BY ts_rank(search, q) DESC, page_no
LIMIT 3
"""

RULES = """You answer questions about one PDF using only the pages given, which are HTML.
Never do arithmetic. Name the inputs a, b, c ... and write a formula over those names; code computes it.
- A figure in a table: ref is its cell id (the td id, like p5-r0-c0) and quote is empty.
- A figure in a sentence: ref is the line id (the p id, like p18-b12) and quote is the figure exactly as printed, like "$133,414".
- formula: only input names, numbers, + - * / and brackets. A single figure is "a". For a percent, multiply by 100 and use unit "percent".
- Amounts in brackets are negative and the cell already carries the sign. Never add a minus sign to fix it.
- A text answer, like a name or a firm: kind "text", the answer in text_answer, and one input whose ref is a line id and whose quote is the exact supporting words from that line.
- "fiscal 2025" means the 2025 column; "last year" means the latest year shown.
- If the pages do not hold the answer, kind "not_found"."""

PLAN = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["number", "text", "not_found"]},
        "inputs": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "ref": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["name", "ref", "quote"],
            "additionalProperties": False,
        }},
        "formula": {"type": "string"},
        "unit": {"type": "string", "enum": ["usd", "percent", "number", "none"]},
        "text_answer": {"type": "string"},
    },
    "required": ["kind", "inputs", "formula", "unit", "text_answer"],
    "additionalProperties": False,
}

OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def answer(doc_id, question):
    words = " | ".join(dict.fromkeys(re.findall(r"[a-z0-9]+", question.lower())))
    with connect() as conn:
        hits = conn.execute(SEARCH, (words, doc_id)).fetchall() if words else []
    result = {"question": question, "search": words, "pages": [h["page_no"] for h in hits], "cited": []}
    if not hits:
        return {**result, "answer": "Not found: no page shares a word with the question."}
    stored = "\n".join(h["html"] for h in hits)
    evidence = re.sub(r' data-(value|bbox)="[^"]*"', "", stored)
    response = anthropic.Anthropic().messages.create(
        model=MODEL, max_tokens=2000, system=RULES,
        messages=[{"role": "user", "content": f"{evidence}\n\nQuestion: {question}"}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": PLAN}},
    )
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        return {**result, "answer": "The model gave no answer."}
    plan = json.loads(text)
    result["plan"] = plan
    if plan["kind"] == "not_found":
        return {**result, "answer": "Not found on the pages searched."}
    soup = BeautifulSoup(stored, "html.parser")
    try:
        result["cited"] = [cite(soup, item) for item in plan["inputs"]]
        if plan["kind"] == "text":
            return {**result, "answer": plan["text_answer"]}
        names = {c["name"]: c["value"] for c in result["cited"]}
        return {**result, "answer": show(calculate(ast.parse(plan["formula"], mode="eval").body, names), plan["unit"])}
    except (ValueError, KeyError, SyntaxError, ZeroDivisionError) as e:
        return {**result, "answer": f"Could not check this answer: {e}"}


def cite(soup, item):
    tag = soup.find(id=item["ref"])
    if tag is None:
        raise ValueError(f"{item['ref']} is not on the pages searched")
    found = {**item, "page": int(tag.find_parent("section")["data-page"]), "bbox": tag["data-bbox"]}
    if tag.name == "td":
        return {**found, "text": tag.get_text(), "value": float(tag["data-value"])}
    if not item["quote"] or item["quote"] not in tag.get_text():
        raise ValueError(f"{item['quote']!r} is not printed on line {item['ref']}")
    return {**found, "text": item["quote"], "value": number(item["quote"])}


def number(text):
    found = re.search(r"\(?\$?\d[\d,]*(\.\d+)?\)?", text)
    if not found:
        return None
    v = float(re.sub(r"[^\d.]", "", found.group()))
    return -v if found.group().startswith("(") else v


def calculate(node, names):
    if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
        return OPERATORS[type(node.op)](calculate(node.left, names), calculate(node.right, names))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -calculate(node.operand, names)
    if isinstance(node, ast.Name) and names.get(node.id) is not None:
        return names[node.id]
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    raise ValueError(f"the formula has {ast.unparse(node)!r}, which is not an input, a number or + - * /")


def show(v, unit):
    if unit == "percent":
        return f"{v:.1f}%"
    if unit == "usd":
        return f"(${-v:,.0f})" if v < 0 else f"${v:,.0f}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")
