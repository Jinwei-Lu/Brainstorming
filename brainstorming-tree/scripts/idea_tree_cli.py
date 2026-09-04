#!/usr/bin/env python3
"""One-shot command-line driver for idea_tree_server.py.

    python3 idea_tree_cli.py <tool_name> ['<json args>'] [workspace]

Runs exactly one MCP ``tools/call`` against the local server and prints the
result. State lives in ``<workspace>/.idea-tree/ideas.sqlite3``, so every call
may be a fresh process; ``workspace`` defaults to the current directory and is
passed to the tool as an absolute path unless the args already carry one.

Exit codes: 0 success; 1 the tool refused (its message is printed); 2 the server
did not answer (its stderr is printed).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "idea_tree_server.py")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    tool = argv[1]
    args = json.loads(argv[2]) if len(argv) > 2 and argv[2] else {}
    workspace = argv[3] if len(argv) > 3 else os.getcwd()
    args.setdefault("workspace", os.path.abspath(workspace))
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "idea_tree_cli", "version": "0.3.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": tool, "arguments": args}},
    ]
    stdin = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
    proc = subprocess.run([sys.executable, SERVER], input=stdin, capture_output=True, text=True)
    response = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == 2:
            response = message
    if response is None:
        sys.stderr.write(proc.stderr)
        return 2
    if "error" in response:
        print(json.dumps(response["error"], ensure_ascii=False, indent=2))
        return 1
    result = response["result"]
    text = "".join(block.get("text", "") for block in result.get("content", []))
    if result.get("isError"):
        print(text)
        return 1
    try:
        print(json.dumps(json.loads(text), ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
