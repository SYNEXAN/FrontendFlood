#!/usr/bin/env python3
"""
Synexan AI Smooth Stream & Single-Screen Injector
Injects the client-side UI & Single-Screen script into ALL Gradio HTML templates
(including both frontend/index.html and frontend/share.html used on Kaggle/Colab).
Ensures 100% zero changes to the backend/ folder.
"""
import os
import sys
import re


def inject_patch():
    try:
        import gradio
        gradio_dir = os.path.dirname(gradio.__file__)
        templates_root = os.path.join(gradio_dir, "templates")

        script_dir = os.path.dirname(os.path.abspath(__file__))
        patch_file = os.path.join(script_dir, "smooth_stream_patch.js")

        if not os.path.exists(patch_file):
            print(f"⚠️ [Injector] Không tìm thấy file patch: {patch_file}")
            return False

        with open(patch_file, "r", encoding="utf-8") as f:
            js_code = f.read()

        patched_files = []
        for root, _, files in os.walk(templates_root):
            for fname in files:
                if fname.endswith(".html"):
                    tpath = os.path.join(root, fname)
                    try:
                        with open(tpath, "r", encoding="utf-8") as tf:
                            content = tf.read()

                        if "</head>" not in content:
                            continue

                        if "synexan-smooth-stream" in content:
                            content = re.sub(
                                r'<script id="synexan-smooth-stream">.*?</script>\s*',
                                '',
                                content,
                                flags=re.DOTALL
                            )

                        injection = f'\n<script id="synexan-smooth-stream">\n{js_code}\n</script>\n</head>'
                        new_content = content.replace("</head>", injection, 1)

                        with open(tpath, "w", encoding="utf-8") as tf:
                            tf.write(new_content)
                        patched_files.append(tpath)
                    except Exception as inner_e:
                        print(f"⚠️ [Injector] Không thể vá {tpath}: {inner_e}")

        print(f"🎉 [Injector] Đã tích hợp thành công Single-Screen & FPS/Delay Controller vào {len(patched_files)} Gradio templates: {patched_files}")
        return len(patched_files) > 0
    except Exception as e:
        print(f"❌ [Injector] Lỗi tích hợp: {e}")
        return False


def start_legacy_fork_keepalive():
    """Nếu người dùng chạy bản fork cũ của Notebook (chưa gọi kaggle_backend_launcher.py),
    tự động dọn port 7860 cũ và chạy ngầm luồng đồng bộ Vercel + chống tắt 15 phút."""
    if os.environ.get("SYNEXAN_LAUNCHER_ACTIVE") == "1":
        return
    try:
        import subprocess
        import signal
        my_pid = os.getpid()
        parent_pid = os.getppid()
        out = subprocess.check_output(["ps", "-ef"], text=True)
        for line in out.splitlines():
            if "app.py" in line and "inject_smooth_stream" not in line:
                parts = line.split()
                if len(parts) > 1 and parts[1].isdigit():
                    pid = int(parts[1])
                    if pid not in (my_pid, parent_pid):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except Exception:
                            pass
    except Exception:
        pass

    daemon_code = (
        "import time, re, os, json, urllib.request\n"
        "webhooks = ['https://frontend-flood.vercel.app/api/backend-url']\n"
        "while True:\n"
        "    time.sleep(15)\n"
        "    try:\n"
        "        urllib.request.urlopen('http://127.0.0.1:7860/heartbeat', timeout=3).read()\n"
        "    except Exception:\n"
        "        continue\n"
        "    url = None\n"
        "    if os.path.exists('/tmp/cloudflared.log'):\n"
        "        txt = open('/tmp/cloudflared.log', errors='ignore').read()\n"
        "        m = re.findall(r'https://(?!api\\.)[a-zA-Z0-9]+-[a-zA-Z0-9-]+\\.trycloudflare\\.com', txt)\n"
        "        if m: url = m[-1]\n"
        "    if url:\n"
        "        for ep in webhooks:\n"
        "            try:\n"
        "                req = urllib.request.Request(ep, data=json.dumps({'url': url}).encode(), headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'})\n"
        "                urllib.request.urlopen(req, timeout=8)\n"
        "            except Exception: pass\n"
    )
    try:
        import subprocess
        subprocess.Popen(
            [sys.executable, "-c", daemon_code],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception:
        pass


if __name__ == "__main__":
    inject_patch()
    start_legacy_fork_keepalive()
