#!/bin/bash
# Start ngrok daemon, exposing localhost:8080

set -e

# Read authtoken from environment variable, fall back to existing config if not set
if [ -z "$NGROK_AUTHTOKEN" ]; then
  echo "NGROK_AUTHTOKEN env var not set, using existing ngrok config"
else
  ngrok config add-authtoken "$NGROK_AUTHTOKEN"
fi

# Start ngrok in background as a daemon
nohup ngrok http localhost:8888 > /tmp/ngrok.log 2>&1 &
echo $! > /tmp/ngrok.pid
echo "ngrok daemon started, pid=$(cat /tmp/ngrok.pid), log=/tmp/ngrok.log"

# Wait briefly and print the public URL
sleep 2
URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null \
  | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['tunnels'][0]['public_url'])" 2>/dev/null || echo "")

if [ -n "$URL" ]; then
  echo "Pls click the link $URL"
else
  echo "Could not retrieve ngrok URL yet. Check /tmp/ngrok.log or http://localhost:4040"
fi
