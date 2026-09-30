#!/usr/bin/env bash
# Install the TAHMO agent and skills as a Claude Code plugin.
set -e

claude plugin marketplace add TAHMO/tahmo-skills
claude plugin install tahmo@tahmo-skills

cat <<'MSG'

Installed the tahmo plugin.

Next steps:

# Set your TAHMO API credentials
export TAHMO_API_USERNAME=...
export TAHMO_API_PASSWORD=...

# Then run the agent
claude --agent tahmo:tahmo

MSG
