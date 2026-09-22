@echo off
rem 2026-09-22：心跳改由 Rust exe 直跑（原 Python driver 通道退役）
rem 保留此 bat 兼容旧注册，动作等价 exe --tick
"D:\FDE\publisher-agent\desktop\src-tauri\target\release\publisher-agent-desktop.exe" --tick
