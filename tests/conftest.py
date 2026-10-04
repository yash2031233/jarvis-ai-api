import os
import tempfile

# Isolate every test run from the user's real settings, memory and undo journal.
_tmp = tempfile.mkdtemp(prefix="jarvis_test_")
os.environ["JARVIS_CONFIG_DIR"] = os.path.join(_tmp, "config")
os.environ["JARVIS_DATA_DIR"] = os.path.join(_tmp, "data")
os.environ.pop("JARVIS_API_KEY", None)
