"""入口：无参数时保留原资料检查；带子命令时进入业务仓 CLI。"""
import json
import sys
from pathlib import Path

from .catalog import inspect

if __name__ == "__main__":
    if len(sys.argv) == 1:
        print(json.dumps(inspect(Path(__file__).resolve().parents[1]), ensure_ascii=False, indent=2))
    else:
        from .cli import main
        sys.exit(main())
