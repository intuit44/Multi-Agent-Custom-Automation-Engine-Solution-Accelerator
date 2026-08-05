#!/bin/bash
# Safe Command Wrapper
# Validates and executes shell commands with automatic detached/timeout injection

set -e

# Colors
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
VALIDATE_LOGS=true
VALIDATE_SERVICES=true
AUTO_FIX=true
TIMEOUT_DEFAULT=300

# Command safety rules
declare -A LOG_COMMANDS=(
    ["docker logs"]="--tail"
    ["tail -f"]="tail -n"
    ["kubectl logs"]="--tail"
    ["journalctl"]="--lines"
)

declare -A SERVICE_COMMANDS=(
    ["docker run"]=" -d"
    ["docker-compose up"]=" -d"
    ["uvicorn"]=" &"
    ["npm run"]=" &"
)

function warn() {
    echo -e "${YELLOW}⚠️  WARNING:${NC} $1" >&2
}

function error() {
    echo -e "${RED}❌ ERROR:${NC} $1" >&2
}

function info() {
    echo -e "${BLUE}ℹ️  INFO:${NC} $1"
}

function success() {
    echo -e "${GREEN}✅ SUCCESS:${NC} $1"
}

function validate_command() {
    local cmd="$1"

    # Check for dangerous log commands
    for log_cmd in "${!LOG_COMMANDS[@]}"; do
        if [[ "$cmd" =~ $log_cmd ]]; then
            local required_mod="${LOG_COMMANDS[$log_cmd]}"
            if ! [[ "$cmd" =~ $required_mod ]]; then
                warn "Command '$log_cmd' detected without truncation"
                echo "   Expected: --tail, --lines, or -n flag"
                echo "   Command: $cmd"

                if [[ "$AUTO_FIX" == "true" ]]; then
                    case "$log_cmd" in
                        "docker logs")
                            cmd="${cmd} --tail 100"
                            info "Auto-fixed: $cmd"
                            ;;
                        "tail -f")
                            cmd=$(echo "$cmd" | sed 's/tail -f/tail -n 100/')
                            info "Auto-fixed: $cmd"
                            ;;
                        "kubectl logs")
                            cmd="${cmd} --tail=100"
                            info "Auto-fixed: $cmd"
                            ;;
                        "journalctl")
                            cmd="${cmd} --lines=100"
                            info "Auto-fixed: $cmd"
                            ;;
                    esac
                else
                    error "Refusing to execute unsafe command"
                    return 1
                fi
            fi
        fi
    done

    # Check for dangerous service commands
    for svc_cmd in "${!SERVICE_COMMANDS[@]}"; do
        if [[ "$cmd" =~ $svc_cmd ]]; then
            local required_mod="${SERVICE_COMMANDS[$svc_cmd]}"
            if ! [[ "$cmd" =~ $required_mod ]]; then
                warn "Service/long-running command detected without detached flag"
                echo "   Expected: $required_mod"
                echo "   Command: $cmd"

                if [[ "$AUTO_FIX" == "true" ]]; then
                    case "$svc_cmd" in
                        "docker run")
                            cmd=$(echo "$cmd" | sed "s/docker run /docker run -d /" | sed "s/ -d  / -d /")
                            info "Auto-fixed: $cmd"
                            ;;
                        "docker-compose up")
                            cmd=$(echo "$cmd" | sed "s/docker-compose up /docker-compose up -d /")
                            info "Auto-fixed: $cmd"
                            ;;
                        *)
                            cmd="${cmd} &"
                            info "Auto-fixed: $cmd"
                            ;;
                    esac
                else
                    error "Refusing to execute unsafe command"
                    return 1
                fi
            fi
        fi
    done

    echo "$cmd"
    return 0
}

function execute_safe() {
    local original_cmd="$1"

    # Validate
    local validated_cmd
    validated_cmd=$(validate_command "$original_cmd") || return 1

    # Execute with timeout
    info "Executing: $validated_cmd"
    if [[ "$validated_cmd" == *"&" ]]; then
        # Background process
        eval "$validated_cmd"
        success "Process started in background"
    else
        # Foreground with timeout protection
        timeout "$TIMEOUT_DEFAULT" bash -c "$validated_cmd" || {
            local exit_code=$?
            if [[ $exit_code -eq 124 ]]; then
                warn "Command exceeded ${TIMEOUT_DEFAULT}s timeout"
                return 1
            fi
            return $exit_code
        }
    fi
}

# Main
if [[ $# -eq 0 ]]; then
    echo "Safe Command Wrapper"
    echo "Usage: $0 '<command>'"
    echo "Example: $0 'docker logs my-container'"
    exit 1
fi

execute_safe "$1"
