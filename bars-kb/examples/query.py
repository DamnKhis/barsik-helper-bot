from __future__ import annotations

import argparse
import json

from kb_client import KB


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--mode", choices=["dense", "sparse", "hybrid"], default="hybrid")
    parser.add_argument("--export", default=None)
    args = parser.parse_args()

    kb = KB(args.export)
    for rank, hit in enumerate(kb.search(args.query, args.top_k, args.mode), start=1):
        payload = hit.get("payload") or {}
        print(f"\n[{rank}] score={hit.get('score')} type={payload.get('chunk_type')}")
        print("section:", " > ".join(payload.get("section_path") or []))
        print("text:", (payload.get("raw_text") or "")[:500].replace("\n", " "))
        print("source:", payload.get("source_url"))
        if payload.get("image_path"):
            print("image:", payload["image_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
