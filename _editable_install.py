"""Run `pip install -e .[mcp]` for agent-recall editable install."""
import subprocess, sys
result = subprocess.run(
    [sys.executable, "-m", "pip", "install", "-e", ".[mcp]"],
    cwd=r"C:\Users\Administrator\projects\agent-recall",
    capture_output=True, text=True
)
print(result.stdout)
if result.stderr:
    print(result.stderr, file=sys.stderr)
sys.exit(result.returncode)
