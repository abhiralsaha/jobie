#!/usr/bin/env python3
"""List Telegram-related variable NAMES your existing project already defines (values are never printed).

    python scripts/inspect_env.py [path/to/.env ...]

Looks at the given files (default: .env, .env.example, .env.local) and at the current environment,
then suggests how to map them into config/telegram_destinations.json.
"""
import os
import re
import sys

PAT = re.compile(r"(TELEGRAM|TG_|BOT_TOKEN|CHAT_ID|CHANNEL|THREAD|DIGEST)", re.I)


def names_from_file(path):
    out = []
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if m and PAT.search(m.group(1)):
                out.append(m.group(1))
    except OSError:
        pass
    return out


def main():
    files = sys.argv[1:] or [".env", ".env.example", ".env.local"]
    found = {}
    for f in files:
        for n in names_from_file(f):
            found.setdefault(n, set()).add(f)
    for n in os.environ:
        if PAT.search(n):
            found.setdefault(n, set()).add("<current environment>")
    if not found:
        print("No Telegram-looking variables found. Pass the path of your .env / render env export.")
        return 1
    print("Telegram-related variable names (values hidden):\n")
    for n in sorted(found):
        kind = "token?" if re.search(r"TOKEN", n, re.I) else "chat/channel id?" if re.search(r"CHAT|CHANNEL", n, re.I) else "other"
        print(f"  {n:<44} {kind:<18} in {', '.join(sorted(found[n]))}")
    print("\nNext: copy config/telegram_destinations.example.json -> config/telegram_destinations.json and put these NAMES in")
    print("bot_token_env / chat_id_env for each destination you call '1 min', 'Fresher bot', '5 min and 30 min ago', etc.")
    print("Then run: python -m app.ai_pipeline.cli check-destinations   (verifies access without sending anything)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
