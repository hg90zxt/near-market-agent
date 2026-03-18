#!/usr/bin/env bash
set -e

echo "=== NEAR Market Bot — installer ==="

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "Error: python3 not found. Install Python 3.8+ first."
  exit 1
fi

# Check Node.js / npm
if ! command -v npm &>/dev/null; then
  echo "Installing Node.js..."
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi

# Install near-cli
if ! command -v near &>/dev/null; then
  echo "Installing near-cli..."
  npm install -g near-cli
fi

# Install Rust
if ! command -v cargo &>/dev/null; then
  echo "Installing Rust..."
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
  source "$HOME/.cargo/env"
fi

# Add WASM target for NEAR contract compilation
rustup target add wasm32-unknown-unknown 2>/dev/null || true

# Install Python dependencies
echo "Installing Python dependencies..."
pip install -r requirements.txt --break-system-packages --quiet 2>/dev/null \
  || pip install -r requirements.txt --quiet

echo ""
echo "=== Setup complete ==="
echo ""
echo "Set environment variables before running:"
echo "  export NEAR_KEY=your_near_api_key"
echo "  export OPENROUTER_KEY=your_openrouter_key"
echo "  export TG_BOT_TOKEN=your_tg_token"
echo "  export TG_CHAT_ID=your_chat_id"
echo "  export AGENT_ID=your_agent_id"
echo "  export NPM_TOKEN=your_npm_token          # for npm publish"
echo "  export GITHUB_TOKEN=your_github_token    # for GitHub repo/gist"
echo "  export NEAR_DEPLOY_ACCOUNT=name.testnet  # for NEAR deploy"
echo ""
echo "Then run:"
echo "  nohup python3 autobot.py >> autobot.log 2>&1 &"
echo ""
