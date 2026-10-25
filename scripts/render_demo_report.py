"""Render a captured, sanitized demo report as an SVG for the README."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", NS)


def render(report):
    root = ET.Element(f"{{{NS}}}svg", {
        "width": "1280", "height": "940", "viewBox": "0 0 1280 940",
        "role": "img", "aria-label": "Actual local integration demo results",
    })

    def element(tag, **attributes):
        return ET.SubElement(root, f"{{{NS}}}{tag}", {
            key.replace("_", "-"): str(value) for key, value in attributes.items()
        })

    def text(x, y, value, size=16, fill="#334155", weight=400, mono=False):
        node = element(
            "text", x=x, y=y, font_size=size, fill=fill, font_weight=weight,
            font_family="Menlo, Consolas, monospace" if mono else "Helvetica Neue, Arial, sans-serif",
        )
        node.text = str(value)

    def line(y):
        element("line", x1=48, x2=1232, y1=y, y2=y, stroke="#dce3ea")

    element("rect", x=0, y=0, width=1280, height=940, fill="#ffffff")
    text(48, 54, "LOCAL INTEGRATION RUN", 14, "#0f766e", 600)
    text(48, 108, "rc_adolph / Demo Results", 34, "#111827", 600)
    text(48, 145, "Real PostgreSQL + separate API/Worker + local supplier Mock", 18)
    captured = datetime.fromisoformat(report["run_at"]).strftime("%Y-%m-%d %H:%M:%S UTC")
    text(48, 177, "Captured " + captured + " | Receipt reuse verified in all three cases", 14)
    line(207)
    for x, label in (
        (48, "SCENARIO / RETRY CONTRACT"), (370, "API OBSERVATIONS"),
        (790, "ATTEMPTS"), (1035, "CALLS"), (1138, "EFFECTS"),
    ):
        text(x, 241, label, 12, "#526070", 600)

    labels = {
        "success": ("01  Normal delivery", "#047857"),
        "retry_after_503": ("02  Supplier recovers", "#b45309"),
        "ambiguous_timeout_reconciled": ("03  Ambiguous timeout", "#be123c"),
    }
    if [row["name"] for row in report["scenarios"]] != list(labels):
        raise ValueError("Expected the three captured interview demo scenarios")
    for index, row in enumerate(report["scenarios"]):
        y = (292, 472, 652)[index]
        label, color = labels[row["name"]]
        element("rect", x=48, y=y - 19, width=4, height=48, fill=color)
        text(66, y, label, 19, "#111827", 600)
        text(66, y + 31, row["mode"], 13, mono=True)
        if row["idempotent_receipt_reused"] is not True:
            raise ValueError("Receipt reuse was not verified")
        text(66, y + 60, "Same submission key -> same receipt", 12, "#526070")
        for offset, state in enumerate(row["observed_statuses"]):
            final = offset == len(row["observed_statuses"]) - 1
            text(370, y + offset * 25, state, 15, "#047857" if final else "#334155", mono=True)
        if row["name"] == "ambiguous_timeout_reconciled":
            text(370, y + 113, "Closed only after Mock evidence + reconcile API", 12, "#526070")
        for offset, attempt in enumerate(row["attempts"]):
            status = attempt["http_status"]
            value = str(status) if status is not None else "no status"
            color = "#047857" if status == 200 else "#b45309" if status else "#be123c"
            text(790, y + offset * 25, f"#{attempt['number']}  {value}", 15, color, mono=True)
        text(1060, y + 14, row["supplier_calls"], 30, "#111827", 600, mono=True)
        text(1160, y + 14, row["supplier_effects"], 30, "#047857", 600, mono=True)
        line(y + (157 if index == 2 else 134))

    text(48, 855, "API polling can miss transient states; durable attempts are shown separately.", 14)
    text(48, 887, "Mock effects prove this local run, not a real supplier's exactly-once guarantee.", 14)
    return ET.tostring(root, encoding="unicode") + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    svg = render(json.loads(args.report.read_text()))
    with args.output.open("x") as output:
        output.write(svg)


if __name__ == "__main__":
    main()
