"""Package honest collaboration materials; never manufacture a full conversation."""

import argparse
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcript", type=Path, help="Actual conversation export file")
    args = parser.parse_args()
    if args.transcript and not args.transcript.is_file():
        parser.error("--transcript must be an existing export file")
    root = Path(__file__).resolve().parents[1]
    destination = root / "artifacts" / "ai-session.zip"
    destination.parent.mkdir(exist_ok=True)
    transcript = args.transcript
    if transcript:
        status = (
            "An externally supplied conversation export is included. "
            "Its completeness and redaction must be checked by the submitter.\n"
        )
    else:
        transcript = root / "docs" / "ai-conversation-excerpts.md"
        if transcript.is_file():
            status = (
                "PARTIAL CONVERSATION EXCERPTS AND DECISION SUMMARIES. "
                "Real selected user/assistant messages are included; see their "
                "source and scope in the transcript file. This is NOT a complete "
                "conversation export. Tool outputs and private data are excluded.\n"
            )
        else:
            transcript = None
            status = (
                "SUMMARY ONLY. No full raw conversation export is included. "
                "Do not describe this archive as a complete AI session transcript.\n"
            )
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("STATUS.txt", status)
        for name in ("ai-collaboration.md", "references.md", "verification.md",
                     "architecture.md", "production-runbook.md"):
            archive.write(root / "docs" / name, "docs/" + name)
        for path in sorted((root / "docs" / "adr").glob("*.md")):
            archive.write(path, path.relative_to(root))
        archive.write(root / "GLOSSARY.md", "GLOSSARY.md")
        if transcript:
            archive.write(transcript, "transcript/" + transcript.name)
    print(destination)
    print(status.strip())


if __name__ == "__main__":
    main()
