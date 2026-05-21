"""経験値メーター エントリーポイント。

スクリプト実行: python run_exp_meter.py
exe 実行: 経験値メーター.exe をダブルクリック

CWD を exe / スクリプトのフォルダに切り替えてから main を呼ぶ。
これで settings.json / exp_meter.json / logs/ は常に exe と同じ場所に保存される。
"""
import os
import sys


def _ensure_cwd() -> None:
    if getattr(sys, "frozen", False):
        # PyInstaller でバンドルされた exe として実行
        app_dir = os.path.dirname(sys.executable)
    else:
        app_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(app_dir)


if __name__ == "__main__":
    _ensure_cwd()
    from gui.main import main
    main()
