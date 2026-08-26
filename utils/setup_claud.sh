#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "=== 1. 安装 @anthropic-ai/claude-code ==="
if ! command -v npm &> /dev/null; then
    echo "npm 未安装，请先安装 Node.js (建议 nvm + Node 20+)"
    exit 1
fi
npm install -g @anthropic-ai/claude-code

# 校验 ANTA_LLM_API_KEY
if [ -z "${ANTA_LLM_API_KEY:-}" ]; then
    echo "错误: 环境变量 ANTA_LLM_API_KEY 未设置"
    echo "请先 export ANTA_LLM_API_KEY=<your_key> 再运行本脚本"
    exit 1
fi
KEY_SUFFIX="$(printf '%s' "$ANTA_LLM_API_KEY" | tail -c 20)"
echo "ANTA_LLM_API_KEY 已设置，后缀: $KEY_SUFFIX"

echo ""
echo "=== 2. 写入 ~/.claude/settings.json ==="
mkdir -p "$HOME/.claude"

# 若已存在则先备份
if [ -f "$HOME/.claude/settings.json" ]; then
    cp "$HOME/.claude/settings.json" "$HOME/.claude/settings.json.bak.$(date +%s)"
    echo "已备份原 settings.json"
fi

# 注意: ANTHROPIC_API_KEY 不写入 settings.json，改由 shell rc 中
# export ANTHROPIC_API_KEY="$ANTA_LLM_API_KEY" 提供，避免硬编码
cat > "$HOME/.claude/settings.json" <<EOF
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://ai.anta.com/aimodels-server/private/llm",
    "ANTHROPIC_MODEL": "glm-5.2",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.2",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.2",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_SKIP_FAST_MODE_NETWORK_ERRORS": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "ENABLE_TOOL_SEARCH": "false"
  },
  "statusLine": {
    "type": "command",
    "command": "ccstatusline",
    "padding": 0
  },
  "enabledPlugins": {
    "frontend-design@claude-plugins-official": true,
    "superpowers@claude-plugins-official": true,
    "code-review@claude-plugins-official": true,
    "code-simplifier@claude-plugins-official": true,
    "context7@claude-plugins-official": true,
    "feature-dev@claude-plugins-official": true,
    "playwright@claude-plugins-official": true,
    "skill-creator@claude-plugins-official": true,
    "claude-md-management@claude-plugins-official": true,
    "commit-commands@claude-plugins-official": true,
    "chrome-devtools-mcp@claude-plugins-official": true
  },
  "extraKnownMarketplaces": {
    "omc": {
      "source": {
        "source": "git",
        "url": "https://github.com/yeachan-heo/oh-my-claudecode.git"
      }
    },
    "claude-for-financial-services": {
      "source": {
        "source": "github",
        "repo": "anthropics/financial-services"
      }
    },
    "claude-for-financial-services-china": {
      "source": {
        "source": "github",
        "repo": "jwangkun/claude-for-financial-services-cn"
      }
    },
    "last30days-skill": {
      "source": {
        "source": "github",
        "repo": "mvanhorn/last30days-skill"
      }
    }
  },
  "skipDangerousModePermissionPrompt": true
}
EOF
echo "已写入 ~/.claude/settings.json"

echo ""
echo "=== 3. 写入 ~/.claude.json (最小化，跳过 onboarding + 预批准 API key) ==="
if [ -f "$HOME/.claude.json" ]; then
    cp "$HOME/.claude.json" "$HOME/.claude.json.bak.$(date +%s)"
    echo "已备份原 .claude.json"
fi
cat > "$HOME/.claude.json" <<EOF
{
  "hasCompletedOnboarding": true,
  "installMethod": "global",
  "customApiKeyResponses": {
    "approved": ["$KEY_SUFFIX"],
    "rejected": []
  },
  "opusProMigrationComplete": true,
  "sonnet1m45MigrationComplete": true
}
EOF
echo "已写入 ~/.claude.json (预批准 key 后缀: $KEY_SUFFIX)"

echo ""
echo "=== 3.5 注入 ANTHROPIC_API_KEY 到 shell rc ==="
INJECT_LINE='export ANTHROPIC_API_KEY="$ANTA_LLM_API_KEY"'

inject_to_rc() {
    local rc="$1"
    [ -f "$rc" ] || touch "$rc"
    if ! grep -qF 'export ANTHROPIC_API_KEY="$ANTA_LLM_API_KEY"' "$rc" 2>/dev/null; then
        printf '\n# Added by setup_claud.sh\n%s\n' "$INJECT_LINE" >> "$rc"
        echo "已写入 $rc"
    else
        echo "$rc 已存在该行，跳过"
    fi
}

if [ -n "${ZSH_VERSION:-}" ] || [ "$SHELL" = "/bin/zsh" ] || [ "$SHELL" = "/usr/bin/zsh" ]; then
    inject_to_rc "$HOME/.zshrc"
elif [ -n "${BASH_VERSION:-}" ] || [ "$SHELL" = "/bin/bash" ] || [ "$SHELL" = "/usr/bin/bash" ]; then
    inject_to_rc "$HOME/.bashrc"
else
    # 两个都写保险一点
    inject_to_rc "$HOME/.zshrc"
    inject_to_rc "$HOME/.bashrc"
fi
echo "请在新的 shell 中 source 对应 rc，或直接运行: export ANTHROPIC_API_KEY=\"\$ANTA_LLM_API_KEY\""

echo ""
echo "=== 3.6 注入 mcc alias 到 shell rc ==="
MCC_ALIAS_LINE="alias mcc='IS_SANDBOX=1 claude --dangerously-skip-permissions --continue'"

inject_mcc_alias() {
    local rc="$1"
    [ -f "$rc" ] || touch "$rc"
    if ! grep -qF "$MCC_ALIAS_LINE" "$rc" 2>/dev/null; then
        echo "$MCC_ALIAS_LINE" >> "$rc"
        echo "已写入 mcc alias 到 $rc"
    else
        echo "$rc 已存在 mcc alias，跳过"
    fi
}

if [ -n "${ZSH_VERSION:-}" ] || [ "$SHELL" = "/bin/zsh" ] || [ "$SHELL" = "/usr/bin/zsh" ]; then
    inject_mcc_alias "$HOME/.zshrc"
    # shellcheck disable=SC1090
    source "$HOME/.zshrc"
elif [ -n "${BASH_VERSION:-}" ] || [ "$SHELL" = "/bin/bash" ] || [ "$SHELL" = "/usr/bin/bash" ]; then
    inject_mcc_alias "$HOME/.bashrc"
    # shellcheck disable=SC1090
    source "$HOME/.bashrc"
else
    inject_mcc_alias "$HOME/.zshrc"
    inject_mcc_alias "$HOME/.bashrc"
fi

echo ""
echo "=== 4. 安装 ccstatusline (statusLine 依赖) ==="
if ! command -v ccstatusline &> /dev/null; then
    npm install -g ccstatusline 2>/dev/null || echo "提示: ccstatusline 安装失败，可后续手动 npm i -g ccstatusline"
else
    echo "ccstatusline 已存在"
fi

echo ""
echo "=== 完成 ==="
echo "运行 'claude' 启动 Claude Code"
echo "运行 'mcc' 以 sandbox 模式继续上次会话"
