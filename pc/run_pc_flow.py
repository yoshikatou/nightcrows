"""PC フロー制御 エントリーポイント。

スクリプト実行: python run_pc_flow.py
exe 実行: PCフロー制御.exe をダブルクリック

CWD を exe / スクリプトのフォルダに切り替えてから main を呼ぶ。
"""
import os
import sys


def _ensure_cwd() -> None:
    if getattr(sys, "frozen", False):
        app_dir = os.path.dirname(sys.executable)
    else:
        app_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(app_dir)


if __name__ == "__main__":
    _ensure_cwd()
    from gui.pc_main import main
    main()
