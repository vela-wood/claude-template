#!/usr/bin/env python
import argparse
import sys

from dotenv import load_dotenv

load_dotenv()


def main():
    parser = argparse.ArgumentParser(
        description="NetDocs CLI - browse and download documents"
    )
    parser.add_argument("--ls", metavar="DOC_ID", help="List files in a folder")
    parser.add_argument("--dl", metavar="DOC_ID", help="Download a document")
    parser.add_argument("--version", type=int, default=1, help="Version to download (default: 1)")
    parser.add_argument("--name", help="Output filename (default: derived from document)")
    parser.add_argument("--recent", action="store_true", help="Show recent matters")

    args = parser.parse_args()

    # If no CLI flags, launch TUI
    if not any([args.ls, args.dl, args.recent]):
        from netdocs import NetDocsApp
        NetDocsApp().run()
        return

    from netdocs.config import load_config

    if args.recent:
        config = load_config()
        if "recent_matters" not in config or not config["recent_matters"]:
            print("No recent matters found.")
            return
        print("Recent matters:")
        for doc_id, label in config["recent_matters"].items():
            print(f"  {doc_id}  {label}")
        return

    # For ls and dl, we need NDHelper
    config = load_config()
    if "settings" not in config or "download_dir" not in config["settings"]:
        print("Error: No download directory configured. Run the TUI first to set it up.", file=sys.stderr)
        sys.exit(1)

    from netdocs import NDHelper
    nd = NDHelper(config["settings"]["download_dir"])

    if args.ls:
        results = nd.ls(args.ls)
        for f in results:
            attrs = f.get("Attributes", {})
            versions = f.get("Versions", {})
            doc_id = f.get("DocId", "")
            name = attrs.get("Name", "")
            ext = attrs.get("Ext", "")
            version = versions.get("Official", 1)
            modified = (attrs.get("Modified", "") or "")[:10]
            type_str = "📁" if ext == "ndfld" else ext
            print(f"{doc_id}  v{version}  {type_str:6}  {modified}  {name}")
        return

    if args.dl:
        # Download requires filename - if not provided, we need to get file info
        if args.name:
            filename = args.name
        else:
            # We don't have direct file info API, so use doc_id as fallback
            filename = f"{args.dl}.download"
            print(f"Note: No --name provided, saving as {filename}", file=sys.stderr)

        path = nd.download(args.dl, args.version, filename)
        print(f"Downloaded: {path}")
        return


if __name__ == "__main__":
    main()
