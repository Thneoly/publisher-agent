@echo off
cd /d D:\FDE\publisher-agent
uv run python -m publisher_agent.driver tick >> _state\tick.log 2>&1
