#!/bin/bash
# claude-sync-upstream.sh - Use Claude Code to analyze and sync upstream changes
#
# This script checks for upstream updates and hands off to Claude Code
# to analyze, merge, deploy, and verify - with automatic rollback on failure.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOMEM_DIR="${AUTOMEM_DIR:-$(dirname "$SCRIPT_DIR")}"
LOG_FILE="$HOME/.local/share/automem/claude-sync.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
UPSTREAM_REPO="verygoodplugins/automem"

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

cd "$AUTOMEM_DIR"

log "Checking for upstream updates..."

# Ensure we have upstream remote
git remote get-url upstream &>/dev/null || git remote add upstream "https://github.com/$UPSTREAM_REPO.git"

# Fetch upstream
git fetch upstream main 2>/dev/null

# Check if upstream has new commits we don't have
LOCAL=$(git rev-parse HEAD)
UPSTREAM=$(git rev-parse upstream/main)

# Find the merge base (common ancestor)
MERGE_BASE=$(git merge-base HEAD upstream/main)

# Count commits on upstream since the merge base
UPSTREAM_NEW=$(git rev-list --count "$MERGE_BASE"..upstream/main)

if [ "$UPSTREAM_NEW" -eq 0 ]; then
    log "No new upstream changes. We're up to date (or ahead)."
    exit 0
fi

log "Found $UPSTREAM_NEW new commit(s) from upstream to review!"
BEHIND=$UPSTREAM_NEW

# Get the diff and commit info for Claude
DIFF_FILE="/tmp/upstream_diff_$(date +%s).txt"
{
    echo "=== UPSTREAM COMMITS ==="
    git log --oneline HEAD..upstream/main
    echo ""
    echo "=== CHANGED FILES ==="
    git diff --name-only HEAD..upstream/main
    echo ""
    echo "=== FULL DIFF ==="
    git diff HEAD..upstream/main
} > "$DIFF_FILE"

# Truncate if too large (keep first 50KB)
if [ $(stat -f%z "$DIFF_FILE" 2>/dev/null || stat -c%s "$DIFF_FILE") -gt 50000 ]; then
    head -c 50000 "$DIFF_FILE" > "${DIFF_FILE}.tmp"
    mv "${DIFF_FILE}.tmp" "$DIFF_FILE"
    echo -e "\n\n[DIFF TRUNCATED - showing first 50KB]" >> "$DIFF_FILE"
fi

log "Handing off to Claude Code for analysis and deployment..."

# Call Claude Code to handle everything
claude --print --dangerously-skip-permissions "
You are managing the AutoMem deployment on this machine. There are upstream changes that need to be reviewed and potentially merged.

CRITICAL CONTEXT:
- This is a fork of verygoodplugins/automem
- We have CUSTOM modifications that MUST be preserved:
  1. automem/embedding/gemini.py - Our Gemini embedding provider
  2. Modified app.py with EMBEDDING_PROVIDER=gemini support
  3. Modified automem/embedding/__init__.py with GeminiEmbeddingProvider import
  4. requirements.txt includes google-genai
  5. .env.example has Gemini config options

- Current commit: $LOCAL
- Upstream commit: $UPSTREAM
- Commits behind: $BEHIND

YOUR TASK:
1. Review the upstream changes below
2. Check if they are SAFE (no malicious code, no credential exposure)
3. Check if they CONFLICT with our Gemini setup
4. If safe, merge the changes: git merge upstream/main
5. Rebuild and restart Docker: docker compose build flask-api && docker compose down && docker compose up -d
6. Wait 15 seconds for Flask API to stabilize
7. Verify Flask API is healthy: curl http://localhost:8001/health
8. CRITICAL: Restart the MCP server (it connects to Flask API and needs a fresh connection):
   sudo systemctl restart automem-mcp
9. Wait 5 seconds for MCP to initialize
10. Verify EVERYTHING is working:
    - curl http://localhost:8001/health returns healthy (Flask API)
    - curl http://localhost:8082/health returns ok (MCP server)
    - Docker logs show 'Embedding provider: gemini'

11. AUTOMEM INTEGRATION TEST - Run this exact test sequence:
    a. Store a test memory:
       curl -X POST http://localhost:8001/memory \\
         -H "Content-Type: application/json" \\
         -H "X-API-Key: \$AUTOMEM_API_TOKEN" \\
         -d '{"content": "Upstream sync test at TIMESTAMP", "tags": ["test", "sync"], "importance": 0.3}'
       (Replace TIMESTAMP with current datetime)
    b. Recall the memory:
       curl "http://localhost:8001/recall?query=upstream+sync+test&limit=1" \\
         -H "X-API-Key: \$AUTOMEM_API_TOKEN"
    c. Verify the response contains the test memory content
    d. Delete the test memory using the returned ID:
       curl -X DELETE "http://localhost:8001/memory/MEMORY_ID" \\
         -H "X-API-Key: \$AUTOMEM_API_TOKEN"

12. If ANY verification fails, ROLLBACK:
    git reset --hard $LOCAL && docker compose build flask-api && docker compose down && docker compose up -d && sudo systemctl restart automem-mcp

13. WHATSAPP NOTIFICATION - After completing (success or failure), send status via clawdbot:
    clawdbot message send --target +972548790112 --message "MESSAGE"

    On SUCCESS: "✅ AutoMem upstream sync complete. Merged X commits. All tests passed. Services healthy."
    On FAILURE: "❌ AutoMem upstream sync FAILED. Reason: [brief description]. Rolled back to previous state."
    On NO CHANGES: This script won't run if no changes, so no notification needed.

IMPORTANT:
- If changes modify automem/embedding/ files, be extra careful about conflicts
- If changes modify app.py init_embedding_provider(), verify our Gemini code survives
- Report your findings and actions clearly
- ALWAYS send the WhatsApp notification as the final step

UPSTREAM CHANGES TO REVIEW:
$(cat "$DIFF_FILE")
"

CLAUDE_EXIT=$?
rm -f "$DIFF_FILE"

if [ $CLAUDE_EXIT -eq 0 ]; then
    log "Claude Code completed successfully"
else
    log "Claude Code exited with code $CLAUDE_EXIT"
fi

exit $CLAUDE_EXIT
