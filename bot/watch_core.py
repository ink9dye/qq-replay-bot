"""兼容旧导入路径；所有逻辑与全局状态都来自项目根目录watch.py。"""
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
import watch as _backend
if Path(_backend.__file__).resolve() != (root / "watch.py").resolve():
    raise RuntimeError("加载到了其他目录的watch，请从qq-replay-bot项目启动。")
if __name__ == "__main__":
    _backend.main()
else:
    sys.modules[__name__] = _backend
