#!/bin/bash
# Tests if Python engine listener is responsive on target port 5000
if ! lsof -Pi :5000 -sTCP:LISTEN -t > /dev/null; then
    echo "Runtime alert: App server execution failure detected."
    # Inject your live webhook link below to enable production push alert system
    # curl -X POST -H "Content-Type: application/json" -d '{"content":"🚨 SYSTEM EMERGENCY CRASH: Backend listener port 5000 offline!"}' YOUR_DISCORD_WEBHOOK_URL
else
    echo "System operational. Heartbeat normal."
fi