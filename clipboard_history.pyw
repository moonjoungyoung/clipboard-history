"""pythonw 로 실행하는 상주 진입점(콘솔 창 없음). 인자는 cli 와 같다."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from clipboard_history.cli import main  # noqa: E402

sys.exit(main())
