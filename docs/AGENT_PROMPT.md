# Integration verification checklist

The AI addon has already been merged and wired into the Jobie watcher and
import API. Do not repeat the integration or overwrite existing project files.

Before live use:

1. Run `python -m unittest discover -s tests_ai -t .` and
   `python -m app.ai_pipeline.cli demo`.
2. Confirm each Telegram environment-variable mapping with the operator, then
   create `config/telegram_destinations.json` using variable names only.
   Resume PDFs may be routed only to destinations named `1 min`, `Fresher bot`,
   and `5 min and 30 min ago`; leave PDF delivery blocked if any mapping is
   unclear.
3. Copy the example resume profile and templates to their runtime paths and
   replace every placeholder with the operator's own facts/content.
4. Keep `AI_DRY_RUN=true`; verify config, destination reachability, and
   pipeline reports without sending real messages or email.
5. Verify the Render watcher uses `deploy/Dockerfile.addon` if resume PDFs are
   required, and that it is the only process with
   `AI_RUN_APPROVAL_POLLER=true`.
6. Report exact test output and any manual Render/environment setup still
   required. Do not turn off dry-run or send messages without explicit
   authorization.
