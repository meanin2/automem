#!/bin/bash
# sync-and-deploy.sh - Pull updates from fork and redeploy AutoMem
#
# Usage:
#   ./scripts/sync-and-deploy.sh           # Interactive mode
#   ./scripts/sync-and-deploy.sh --auto    # Auto mode (for cron)
#   ./scripts/sync-and-deploy.sh --check   # Check only, no deploy
#
# Environment:
#   AUTOMEM_DIR     - AutoMem directory (default: script's parent dir)
#   CLAUDE_API_KEY  - For AI-powered change analysis (optional)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOMEM_DIR="${AUTOMEM_DIR:-$(dirname "$SCRIPT_DIR")}"
LOG_FILE="/var/log/automem-sync.log"
AUTO_MODE=false
CHECK_ONLY=false

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo -e "$msg"
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

error() {
    log "${RED}ERROR: $1${NC}"
    exit 1
}

success() {
    log "${GREEN}$1${NC}"
}

warn() {
    log "${YELLOW}WARNING: $1${NC}"
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --auto)
            AUTO_MODE=true
            shift
            ;;
        --check)
            CHECK_ONLY=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

cd "$AUTOMEM_DIR" || error "Cannot cd to $AUTOMEM_DIR"

log "Starting AutoMem sync check..."

# Check current branch
CURRENT_BRANCH=$(git branch --show-current)
if [ "$CURRENT_BRANCH" != "main" ]; then
    warn "Not on main branch (on $CURRENT_BRANCH)"
    if [ "$AUTO_MODE" = false ]; then
        read -p "Switch to main? (y/n) " -n 1 -r
        echo
        [[ $REPLY =~ ^[Yy]$ ]] && git checkout main || exit 1
    else
        error "Auto mode requires main branch"
    fi
fi

# Fetch latest from fork
log "Fetching updates from fork..."
git fetch origin main

# Check if we're behind
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    success "Already up to date!"
    exit 0
fi

BEHIND=$(git rev-list --count HEAD..origin/main)
log "Found $BEHIND new commit(s) to pull"

# Show what's coming
log "=== Incoming Changes ==="
git log --oneline HEAD..origin/main
echo ""

# Check if Gemini files are affected
GEMINI_FILES_CHANGED=false
CHANGED_FILES=$(git diff --name-only HEAD..origin/main)

if echo "$CHANGED_FILES" | grep -qE "automem/embedding/(gemini|__init__|provider)\.py|app\.py.*embedding"; then
    GEMINI_FILES_CHANGED=true
    warn "Changes detected in embedding-related files!"
fi

# Verify our Gemini setup will survive
verify_gemini_setup() {
    log "Verifying Gemini setup..."

    # Check if gemini.py exists
    if [ ! -f "automem/embedding/gemini.py" ]; then
        error "gemini.py is missing!"
    fi

    # Check if Gemini is in __init__.py
    if ! grep -q "GeminiEmbeddingProvider" automem/embedding/__init__.py; then
        warn "GeminiEmbeddingProvider not found in __init__.py"
        return 1
    fi

    # Check if app.py has Gemini support
    if ! grep -q 'provider_config == "gemini"' app.py; then
        warn "Gemini provider config not found in app.py"
        return 1
    fi

    # Check requirements
    if ! grep -q "google-genai" requirements.txt; then
        warn "google-genai not found in requirements.txt"
        return 1
    fi

    success "Gemini setup verified!"
    return 0
}

# Run Claude Code analysis if available
analyze_with_claude() {
    if ! command -v claude &> /dev/null; then
        warn "Claude Code CLI not found, skipping AI analysis"
        return 0
    fi

    log "Running Claude Code analysis on incoming changes..."

    local diff_file="/tmp/automem_incoming_changes.diff"
    git diff HEAD..origin/main > "$diff_file"

    # Run claude in non-interactive mode
    local analysis
    analysis=$(claude --print "Analyze this git diff for the AutoMem project. Our fork has a custom Gemini embedding provider. Check if these changes:
1. Have any security issues
2. Would break our Gemini provider (automem/embedding/gemini.py)
3. Modify the embedding provider initialization in app.py
4. Are safe to auto-merge

Respond with: SAFE, CAUTION, or DANGEROUS followed by a brief explanation.

$(head -c 30000 "$diff_file")" 2>/dev/null || echo "CAUTION: Claude analysis failed")

    log "Claude Analysis: $analysis"

    if echo "$analysis" | grep -qi "DANGEROUS"; then
        error "Claude flagged these changes as DANGEROUS - manual review required"
    elif echo "$analysis" | grep -qi "CAUTION"; then
        warn "Claude flagged these changes as CAUTION"
        if [ "$AUTO_MODE" = true ]; then
            error "Auto mode aborted due to CAUTION flag"
        fi
    fi

    return 0
}

if [ "$CHECK_ONLY" = true ]; then
    log "Check-only mode - not pulling or deploying"
    verify_gemini_setup || warn "Gemini setup may have issues"
    exit 0
fi

# Ask for confirmation in interactive mode
if [ "$AUTO_MODE" = false ]; then
    echo ""
    read -p "Pull these changes and redeploy? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log "Aborted by user"
        exit 0
    fi
fi

# Run analysis
analyze_with_claude

# Pull changes
log "Pulling changes..."
git pull origin main

# Verify Gemini setup after pull
if ! verify_gemini_setup; then
    error "Gemini setup broken after pull! Rolling back..."
    git reset --hard "$LOCAL"
    exit 1
fi

# Rebuild and restart Docker services
log "Rebuilding Docker services..."
cd "$AUTOMEM_DIR"

# Rebuild the flask-api service
docker compose build flask-api

# Restart services
log "Restarting services..."
docker compose down
docker compose up -d

# Wait for services to be healthy
log "Waiting for services to be healthy..."
sleep 10

# Comprehensive post-deployment verification
verify_deployment() {
    log "Running comprehensive deployment verification..."

    local errors=0

    # 1. Basic health check
    log "  [1/5] Checking API health..."
    HEALTH=$(curl -s http://localhost:8001/health 2>/dev/null || echo '{"status":"error"}')
    STATUS=$(echo "$HEALTH" | jq -r '.status // "error"')
    if [ "$STATUS" != "healthy" ]; then
        warn "  FAIL: API health check failed (status: $STATUS)"
        ((errors++))
    else
        log "  PASS: API is healthy"
    fi

    # 2. Check FalkorDB connection
    log "  [2/5] Checking FalkorDB connection..."
    FALKORDB=$(echo "$HEALTH" | jq -r '.falkordb // "error"')
    if [ "$FALKORDB" != "connected" ]; then
        warn "  FAIL: FalkorDB not connected"
        ((errors++))
    else
        log "  PASS: FalkorDB connected"
    fi

    # 3. Check Qdrant connection
    log "  [3/5] Checking Qdrant connection..."
    QDRANT=$(echo "$HEALTH" | jq -r '.qdrant // "error"')
    if [ "$QDRANT" != "connected" ]; then
        warn "  FAIL: Qdrant not connected"
        ((errors++))
    else
        log "  PASS: Qdrant connected"
    fi

    # 4. Verify Gemini embedding provider is active
    log "  [4/5] Checking Gemini embedding provider..."
    EMBEDDING_LOG=$(docker logs automem-flask-api-1 2>&1 | grep -i "embedding provider" | tail -1)
    if echo "$EMBEDDING_LOG" | grep -qi "gemini"; then
        log "  PASS: Gemini embeddings active ($EMBEDDING_LOG)"
    else
        warn "  FAIL: Gemini embeddings NOT active! Found: $EMBEDDING_LOG"
        ((errors++))
    fi

    # 5. Test store and recall (functional test)
    log "  [5/5] Testing store and recall functionality..."
    TEST_ID="deploy-test-$(date +%s)"
    TEST_CONTENT="Deployment verification test at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

    # Store a test memory
    STORE_RESULT=$(curl -s -X POST http://localhost:8001/memory \
        -H "Authorization: Bearer $AUTOMEM_API_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"content\": \"$TEST_CONTENT\", \"tags\": [\"deploy-test\"], \"importance\": 0.1}" 2>/dev/null)

    STORE_STATUS=$(echo "$STORE_RESULT" | jq -r '.status // "error"')
    MEMORY_ID=$(echo "$STORE_RESULT" | jq -r '.memory_id // ""')

    if [ "$STORE_STATUS" = "success" ] && [ -n "$MEMORY_ID" ]; then
        log "  PASS: Memory stored (ID: $MEMORY_ID)"

        # Wait for embedding to be processed
        sleep 3

        # Test recall
        RECALL_RESULT=$(curl -s "http://localhost:8001/recall?query=deployment+verification+test&limit=1" \
            -H "Authorization: Bearer $AUTOMEM_API_TOKEN" 2>/dev/null)

        RECALL_COUNT=$(echo "$RECALL_RESULT" | jq -r '.count // 0')
        if [ "$RECALL_COUNT" -gt 0 ]; then
            log "  PASS: Recall working (found $RECALL_COUNT memories)"
        else
            warn "  FAIL: Recall returned no results"
            ((errors++))
        fi

        # Clean up test memory
        curl -s -X DELETE "http://localhost:8001/memory/$MEMORY_ID" \
            -H "Authorization: Bearer $AUTOMEM_API_TOKEN" > /dev/null 2>&1
    else
        warn "  FAIL: Could not store test memory"
        ((errors++))
    fi

    # 6. Check MCP server if running
    log "  [6/6] Checking MCP server..."
    MCP_HEALTH=$(curl -s http://localhost:8082/health 2>/dev/null || echo '{"status":"error"}')
    MCP_STATUS=$(echo "$MCP_HEALTH" | jq -r '.status // "error"')
    if [ "$MCP_STATUS" = "healthy" ]; then
        log "  PASS: MCP server healthy"
    else
        warn "  WARN: MCP server not responding (may need restart)"
    fi

    return $errors
}

rollback_deployment() {
    local previous_commit="$1"
    log "Rolling back to previous commit: $previous_commit"

    # Stop services
    docker compose down

    # Reset to previous commit
    git reset --hard "$previous_commit"

    # Rebuild and restart
    docker compose build flask-api
    docker compose up -d

    # Wait for services
    sleep 10

    # Verify rollback worked
    HEALTH=$(curl -s http://localhost:8001/health 2>/dev/null || echo '{"status":"error"}')
    STATUS=$(echo "$HEALTH" | jq -r '.status // "error"')

    if [ "$STATUS" = "healthy" ]; then
        success "Rollback successful - system restored to previous state"
    else
        error "CRITICAL: Rollback failed! Manual intervention required!"
    fi
}

# Run verification
AUTOMEM_API_TOKEN="${AUTOMEM_API_TOKEN:-automem-secret-token-2025}"
VERIFICATION_ERRORS=0
verify_deployment || VERIFICATION_ERRORS=$?

if [ $VERIFICATION_ERRORS -gt 0 ]; then
    error "Deployment verification failed with $VERIFICATION_ERRORS errors!"
    warn "Initiating automatic rollback..."
    rollback_deployment "$LOCAL"
    exit 1
fi

success "All deployment verifications passed!"

# Verify Gemini embeddings are working (summary)
EMBEDDING_PROVIDER=$(docker logs automem-flask-api-1 2>&1 | grep -o "Embedding provider:.*" | tail -1)
log "Active embedding provider: $EMBEDDING_PROVIDER"

log "Sync and deploy completed successfully!"
