import os
import sys

exe_dir = os.path.dirname(sys.executable)
os.environ["PATH"] = exe_dir + os.pathsep + os.environ.get("PATH", "")

from wrekker.ui.app import main

main()
