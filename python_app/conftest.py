# python_app 폴더 안에서 `pytest tests/` 를 실행해도 `python_app.*` 임포트가 되도록 상위 폴더 등록
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
