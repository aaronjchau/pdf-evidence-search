import html
import re

import pdfplumber

NUMBER = re.compile(r"^\(?-?\$?\d[\d,]*(\.\d+)?\)?%?$")
YEAR = re.compile(r"^(19|20)\d\d$")
DASHES = {"-", "–", "—", "−"}


def pdf_to_pages(pdf_path, max_pages=300):
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for index, page in enumerate(pdf.pages[:max_pages]):
            words, width = read_words(pdf_path, page, index)
            page_no = index + 1
            items = parse_page(words, width, page_no)
            pages.append((page_no, page_html(items, page_no), " ".join(w["text"] for w in words)))
    return pages


def read_words(pdf_path, page, index):
    sideways = sum(not c.get("upright", True) for c in page.chars)
    if len(page.chars) >= 30 and sideways <= len(page.chars) * 0.05:
        return page.extract_words(x_tolerance=1.5, y_tolerance=2.5), page.width
    return ocr_words(pdf_path, index)


def ocr_words(pdf_path, index):
    import pypdfium2
    import pytesseract

    scale = 300 / 72
    image = pypdfium2.PdfDocument(pdf_path)[index].render(scale=scale).to_pil().convert("L")
    width, height = image.width / scale, image.height / scale
    try:
        angle = int(pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT)["rotate"])
    except pytesseract.TesseractError:
        angle = 0
    image = image.rotate(-angle, expand=True)
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, config="--psm 6")
    words = []
    for i, text in enumerate(data["text"]):
        if text.strip() and float(data["conf"][i]) >= 30:
            x, y, w, h = (data[k][i] / scale for k in ("left", "top", "width", "height"))
            words.append({"text": text.strip(), "x0": x, "x1": x + w, "top": y, "bottom": y + h,
                          "page_box": unrotate(x, y, x + w, y + h, angle, width, height)})
    return words, image.width / scale


def unrotate(x0, y0, x1, y1, angle, width, height):
    turn = {0: lambda x, y: (x, y), 90: lambda x, y: (y, height - x),
            180: lambda x, y: (width - x, height - y), 270: lambda x, y: (width - y, x)}[angle]
    (ax, ay), (bx, by) = turn(x0, y0), turn(x1, y1)
    return min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)


def lines_of(words):
    lines = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= 2.5:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def is_number(text):
    return bool(NUMBER.match(text)) or text in DASHES


def value(text):
    if text in DASHES:
        return 0.0
    v = float(text.strip("()").replace("$", "").replace(",", "").rstrip("%"))
    return -v if text.startswith("(") else v


def split_numbers(line, width):
    i = len(line)
    while i > 0 and line[i - 1]["x0"] > width * 0.4 and (is_number(line[i - 1]["text"]) or line[i - 1]["text"] == "$"):
        i -= 1
    return [w for w in line[:i] if w["text"] != "$"], [w for w in line[i:] if w["text"] != "$"]


def parse_page(words, width, page_no):
    out = []
    for line in lines_of(words):
        label, numbers = split_numbers(line, width)
        after_row = bool(out) and out[-1]["kind"] == "table"
        apart = not label or numbers and numbers[0]["x0"] - label[-1]["x1"] > 10
        if numbers and apart and (label or after_row) and not all(YEAR.match(w["text"]) for w in numbers):
            row = {"line": line, "label": label, "numbers": numbers}
            if after_row:
                out[-1]["rows"].append(row)
            else:
                out.append({"kind": "table", "rows": [row]})
        else:
            out.append({"kind": "text", "line": line})
    names, rows = [], 0
    for i, table in enumerate(out):
        if table["kind"] != "table":
            continue
        columns = column_edges([w for r in table["rows"] for w in r["numbers"]])
        heads = take_header(out, i, columns)
        if heads or len(names) != len(columns):
            names = column_names(columns, heads)
        table["columns"] = names
        for r in table["rows"]:
            r["id"] = f"p{page_no}-r{rows}"
            r["cells"] = {}
            for w in r["numbers"]:
                k = column_of(columns, w)
                r["cells"][k] = {"id": f"{r['id']}-c{k}", "text": w["text"], "value": value(w["text"]), "bbox": box([w])}
            rows += 1
    return out


def column_edges(numbers):
    columns = []
    for w in sorted(numbers, key=lambda w: w["x1"]):
        if columns and w["x1"] - columns[-1]["x1"] <= 8:
            columns[-1]["x0"] = min(columns[-1]["x0"], w["x0"])
            columns[-1]["x1"] = w["x1"]
        else:
            columns.append({"x0": min(w["x0"], w["x1"] - 45), "x1": w["x1"]})
    return columns


def distance(column, w):
    middle = (w["x0"] + w["x1"]) / 2
    return max(column["x0"] - middle, middle - column["x1"], 0)


def column_of(columns, w):
    return min(range(len(columns)), key=lambda k: distance(columns[k], w))


def take_header(out, i, columns):
    start = columns[0]["x0"] - 15
    heads, skipped = [], 0
    for item in reversed(out[max(i - 7, 0):i]):
        if item["kind"] != "text":
            break
        left = [w for w in item["line"] if (w["x0"] + w["x1"]) / 2 < start]
        right = item["line"][len(left):]
        if not right:
            if heads or skipped == 3:
                break
            skipped += 1
            continue
        lone_year = len(item["line"]) == 1 and YEAR.match(right[0]["text"])
        fits = all(distance(columns[column_of(columns, w)], w) <= 15 and (YEAR.match(w["text"]) or not is_number(w["text"]))
                   for w in right)
        if not (lone_year or fits) or (left and right[0]["x0"] - left[-1]["x1"] < 10):
            break
        heads.insert(0, [dict(right[0], span=True)] if lone_year else right)
        item["line"] = left
        item["kind"] = "text" if left else "head"
    return heads


def column_names(columns, heads):
    names = [[] for _ in columns]
    for line in heads:
        for w in line:
            for k in range(len(columns)) if w.get("span") else [column_of(columns, w)]:
                names[k].append(w["text"])
    return [" ".join(name) or f"column {k + 1}" for k, name in enumerate(names)]


def box(words):
    boxes = [w.get("page_box") or (w["x0"], w["top"], w["x1"], w["bottom"]) for w in words]
    edges = min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)
    return ",".join(f"{e:.1f}" for e in edges)


def page_html(items, page_no):
    out, lines = [f'<section data-page="{page_no}">'], 0
    for item in items:
        if item["kind"] == "text":
            text = html.escape(" ".join(w["text"] for w in item["line"]))
            out.append(f'<p id="p{page_no}-b{lines}" data-bbox="{box(item["line"])}">{text}</p>')
            lines += 1
        elif item["kind"] == "table":
            out.append("<table>")
            out.append("<tr><th></th>" + "".join(f"<th>{html.escape(n)}</th>" for n in item["columns"]) + "</tr>")
            for r in item["rows"]:
                tds = ""
                for k in range(len(item["columns"])):
                    c = r["cells"].get(k)
                    tds += (f'<td id="{c["id"]}" data-value="{c["value"]}" data-bbox="{c["bbox"]}">{html.escape(c["text"])}</td>'
                            if c else "<td></td>")
                label = html.escape(" ".join(w["text"] for w in r["label"]))
                out.append(f'<tr id="{r["id"]}" data-bbox="{box(r["line"])}"><th>{label}</th>{tds}</tr>')
            out.append("</table>")
    out.append("</section>")
    return "\n".join(out)
