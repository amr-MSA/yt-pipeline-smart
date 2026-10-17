from pathlib import Path
import importlib.util

spec = importlib.util.spec_from_file_location("test_long_pipeline_safety", Path(__file__).with_name("test_long_pipeline_safety.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

for name in sorted(dir(module)):
    if name.startswith("test_"):
        getattr(module, name)()
        print(f"{name}: OK")
