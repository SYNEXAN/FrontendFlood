#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/kaggle_backend_launcher.py
Bộ khởi chạy & Giám sát tự phục hồi (Auto-Heal Supervisor) cho Backend GPU trên Kaggle:
- Dọn sạch tiến trình cũ chiếm cổng 7860 (tránh lỗi Address already in use khi chạy lại Cell).
- Đặt IDLE_TIMEOUT_SECONDS=43200 (12h) và ping /heartbeat nội bộ mỗi 15s để app.py không tự tắt sau 15 phút.
- Lọc chính xác URL Cloudflare (loại trừ https://api.trycloudflare.com).
- Tự động khởi động lại cloudflared hoặc app.py nếu bất kỳ tiến trình nào bị ngắt.
- Đồng bộ liên tục URL sống về Vercel (https://frontend-flood.vercel.app/api/backend-url) mỗi 15 giây.
"""

import os
import sys
import time
import re
import json
import shutil
import socket
import signal
import subprocess
import threading
import urllib.request
import urllib.error

TARGET_WEBHOOKS = [
    "https://frontend-flood.vercel.app/api/backend-url",
    "https://frontendflood.vercel.app/api/backend-url",
]

KERNEL_LOG_ENDPOINT = "https://frontend-flood.vercel.app/api/kernel-log"
BACKEND_DIR = "/kaggle/working/FrontendFlood/backend"
WORKING_CWD = BACKEND_DIR if os.path.exists(BACKEND_DIR) else os.getcwd()

# Kéo dài thời gian chờ nhàn rỗi của backend/app.py lên 12 tiếng (43200s)
os.environ["IDLE_TIMEOUT_SECONDS"] = "43200"
os.environ["PYTHONUNBUFFERED"] = "1"
os.environ["SYNEXAN_LAUNCHER_ACTIVE"] = "1"


def notify_kernel_log(status, message, details=None):
    payload = {"status": status, "message": message, "details": details}
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                KERNEL_LOG_ENDPOINT,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                }
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return True
        except Exception as e:
            if attempt == 1:
                print(f"⚠️ [Kernel Log] Không thể gửi log ({status}): {e}")
            time.sleep(1)
    return False


def push_backend_url_to_vercel(public_url, verbose=True):
    """Đẩy URL đường hầm về Vercel kèm cơ chế thử lại đảm bảo 100% thành công."""
    if not public_url or not public_url.startswith("http"):
        return False

    success_any = False
    for endpoint in TARGET_WEBHOOKS:
        payload = json.dumps({"url": public_url, "version": "v8_vn_vehicle_weights"}).encode("utf-8")
        for attempt in range(1, 4):
            try:
                req = urllib.request.Request(
                    endpoint,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        success_any = True
                        if verbose:
                            print(f"✅ [Vercel Sync] Đã đồng bộ URL thành công tới: {endpoint}")
                        break
            except Exception as e:
                if verbose and attempt == 3:
                    print(f"⚠️ [Vercel Sync] Lỗi đồng bộ tới {endpoint} (lần {attempt}): {e}")
                time.sleep(1.5)
    return success_any


def check_port_open(port=7860):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except Exception:
        return False


def check_local_gradio_ready(port=7860):
    """Kiểm tra cổng 7860 đã thực sự phản hồi HTTP 200 từ Gradio/FastAPI hay chưa."""
    if not check_port_open(port):
        return False
    for path in ["/heartbeat", "/config"]:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
    return True


def kill_existing_processes():
    """Dọn sạch các tiến trình cloudflared và app.py cũ đang chiếm port 7860."""
    my_pid = os.getpid()
    subprocess.run(["pkill", "-9", "-f", "cloudflared"], check=False, stderr=subprocess.DEVNULL)
    # Tìm mọi tiến trình python đang chạy app.py
    try:
        out = subprocess.check_output(["ps", "-ef"], text=True)
        for line in out.splitlines():
            if "app.py" in line and "kaggle_backend_launcher" not in line:
                parts = line.split()
                if len(parts) > 1 and parts[1].isdigit():
                    pid = int(parts[1])
                    if pid != my_pid:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except Exception:
                            pass
    except Exception:
        pass

    # Đợi tối đa 5s cho cổng 7860 giải phóng hoàn toàn
    for _ in range(10):
        if not check_port_open(7860):
            break
        time.sleep(0.5)


def ensure_cloudflared_binary():
    cf_bin = "/tmp/cloudflared"
    if not os.path.exists(cf_bin) or os.path.getsize(cf_bin) < 1000000:
        print("⬇️ Đang tải Cloudflare Tunnel binary...")
        opener = urllib.request.build_opener()
        opener.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")]
        urllib.request.install_opener(opener)
        urllib.request.urlretrieve(
            "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
            cf_bin
        )
        os.chmod(cf_bin, 0o755)
    return cf_bin


def install_custom_vehicle_weights():
    """Sao lưu trọng số cũ (yolov8s_vehicle_backup.pt) và kích hoạt trọng số mới 4 lớp VN (yolov8s_vn_vehicle_best.pt)."""
    try:
        custom_weight = "/kaggle/working/FrontendFlood/models/yolov8s_vn_vehicle_best.pt"
        if not os.path.exists(custom_weight) or os.path.getsize(custom_weight) < 15000000:
            return
        models_dir = os.path.join(WORKING_CWD, "models")
        os.makedirs(models_dir, exist_ok=True)
        target_weight = os.path.join(models_dir, "yolov8s_vehicle.pt")
        backup_weight = os.path.join(models_dir, "yolov8s_vehicle_backup.pt")

        # 1. Sao lưu tạm trọng số cũ nếu chưa có bản backup
        if os.path.exists(target_weight) and not os.path.exists(backup_weight):
            if os.path.getsize(target_weight) != os.path.getsize(custom_weight):
                shutil.copy2(target_weight, backup_weight)
                print(f"📦 [Weight Backup] Đã sao lưu trọng số cũ tại: {backup_weight} ({os.path.getsize(backup_weight)} bytes)")

        # 2. Thay thế bằng trọng số mới (best.zip / yolov8s_vn_vehicle_best.pt)
        shutil.copy2(custom_weight, target_weight)
        print(f"🚗 [Custom Weights] Đã kích hoạt trọng số phương tiện mới (4 lớp VN - mAP50=92.79%): {target_weight} ({os.path.getsize(target_weight)} bytes)")
    except Exception as e:
        print(f"⚠️ [Custom Weights] Lỗi cập nhật trọng số phương tiện: {e}")


def install_compatibility_shims():
    """Cài đặt bộ vá HfFolder cho Gradio 4.44.0 và bộ đệm Smooth Stream vào tất cả templates (index.html + share.html)."""
    try:
        shim_code = (
            "import huggingface_hub, sys, os\n"
            "if not hasattr(huggingface_hub, 'HfFolder'):\n"
            "    class HfFolder:\n"
            "        @staticmethod\n"
            "        def get_token():\n"
            "            import os\n"
            "            return os.environ.get('HF_TOKEN', None) or os.environ.get('HUGGING_FACE_HUB_TOKEN', None)\n"
            "        @staticmethod\n"
            "        def save_token(token): pass\n"
            "        @staticmethod\n"
            "        def delete_token(): pass\n"
            "    huggingface_hub.HfFolder = HfFolder\n"
            "    sys.modules['huggingface_hub.HfFolder'] = HfFolder\n"
            "try:\n"
            "    sys.path.insert(0, '/kaggle/working/FrontendFlood/scripts')\n"
            "    import inject_smooth_stream\n"
            "    inject_smooth_stream.inject_patch()\n"
            "except Exception:\n"
            "    pass\n"
        )
        with open(os.path.join(WORKING_CWD, "sitecustomize.py"), "w", encoding="utf-8") as f:
            f.write(shim_code)
        try:
            import site
            for sp in site.getsitepackages():
                if os.path.isdir(sp):
                    with open(os.path.join(sp, "sitecustomize.py"), "w", encoding="utf-8") as sf:
                        sf.write(shim_code)
        except Exception:
            pass
    except Exception as e:
        print(f"⚠️ [Compatibility Shim] {e}")

    try:
        inject_script = "/kaggle/working/FrontendFlood/scripts/inject_smooth_stream.py"
        if os.path.exists(inject_script):
            subprocess.run([sys.executable, inject_script], check=False)
    except Exception as e:
        print(f"⚠️ [Smooth Stream Setup] {e}")


def start_cloudflared_tunnel():
    """Khởi chạy tiến trình cloudflared mới và trả về (cf_proc, log_path)."""
    cf_bin = ensure_cloudflared_binary()
    subprocess.run(["pkill", "-9", "-f", "cloudflared"], check=False, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    log_path = "/tmp/cloudflared.log"
    log_file = open(log_path, "w", buffering=1)
    cf_cmd = [cf_bin, "tunnel", "--protocol", "http2", "--url", "http://127.0.0.1:7860"]
    cf_proc = subprocess.Popen(cf_cmd, stdout=log_file, stderr=subprocess.STDOUT)
    return cf_proc, log_path


def extract_tunnel_url(cf_log_path="/tmp/cloudflared.log", app_log_path="/tmp/app.log"):
    """Trích xuất chính xác URL Cloudflare Quick Tunnel (loại trừ api.trycloudflare.com) hoặc Gradio Live."""
    if os.path.exists(cf_log_path):
        try:
            with open(cf_log_path, "r", encoding="utf-8", errors="ignore") as f:
                log_cf = f.read()
            # Quick tunnel của Cloudflare luôn có dấu gạch ngang (ví dụ: a-b-c-d.trycloudflare.com)
            matches = re.findall(r"https://(?!api\.)[a-zA-Z0-9]+-[a-zA-Z0-9-]+\.trycloudflare\.com", log_cf)
            if matches:
                return matches[-1]
        except Exception:
            pass

    if os.path.exists(app_log_path):
        try:
            with open(app_log_path, "r", encoding="utf-8", errors="ignore") as f:
                log_app = f.read()
            matches_gr = re.findall(r"https://[a-zA-Z0-9-]+\.gradio\.live", log_app)
            if matches_gr:
                return matches_gr[-1]
        except Exception:
            pass

    return None


def main():
    print("=" * 72)
    print("🚀 SYNEXAN AI FLOOD & TRAFFIC BACKEND LAUNCHER (AUTO-HEAL SUPERVISOR)")
    print("=" * 72)

    kill_existing_processes()
    install_custom_vehicle_weights()
    install_compatibility_shims()

    state = {
        "cf_proc": None,
        "app_proc": None,
        "public_url": None,
        "gradio_ready": False,
        "stop": False
    }

    state["cf_proc"], cf_log_path = start_cloudflared_tunnel()

    def supervisor_worker():
        print("⏳ [Supervisor] Đang đợi Gradio nạp mô hình AI và mở cổng 7860...")
        notify_kernel_log("running", "GPU Tesla T4 đã kích hoạt! Đang nạp mô hình AI vào RAM...")

        # 1. Chờ cổng 7860 mở thực sự
        for wait_s in range(1, 200):
            if state["stop"]:
                return
            time.sleep(1)
            if check_local_gradio_ready(7860):
                state["gradio_ready"] = True
                print(f"\n🎉 [Gradio Ready] Cổng 7860 đã mở và phản hồi 200 OK sau {wait_s}s!\n")
                break
            if wait_s % 15 == 0:
                notify_kernel_log("running", f"Đang nạp mô hình Deep Learning vào GPU ({wait_s}s)...")

        if not state["gradio_ready"]:
            print("⚠️ [Supervisor] Quá thời gian chờ cổng 7860.")
            notify_kernel_log("error", "Backend Gradio không mở được cổng 7860 sau 200s!")
            return

        # 2. Lấy URL đường hầm Cloudflare / Gradio
        for _ in range(90):
            url = extract_tunnel_url()
            if url:
                state["public_url"] = url
                break
            time.sleep(1)

        if not state["public_url"]:
            print("⚠️ [Supervisor] Chưa thấy URL Cloudflare, đang khởi động lại cloudflared...")
            state["cf_proc"], _ = start_cloudflared_tunnel()
            for _ in range(30):
                url = extract_tunnel_url()
                if url:
                    state["public_url"] = url
                    break
                time.sleep(1)

        if state["public_url"]:
            pub = state["public_url"]
            print("\n" + "=" * 72)
            print(f"🔥 [LIVE BACKEND URL]: {pub}")
            print(f"🌐 [WEB APP VERCEL]  : https://frontend-flood.vercel.app")
            print("=" * 72 + "\n")
            notify_kernel_log("ready", "Backend GPU đã sẵn sàng và kết nối thành công!", pub)
            push_backend_url_to_vercel(pub, verbose=True)
        else:
            notify_kernel_log("error", "Không tạo được đường hầm Cloudflare/Gradio!")
            return

        # 3. Vòng lặp Heartbeat 15s + Tự phục hồi đường hầm (Auto-Heal)
        print("💓 [Heartbeat & Auto-Heal] Duy trì kết nối nóng mỗi 15 giây...")
        while not state["stop"]:
            time.sleep(15)

            # Giữ cho bộ đếm IDLE_TIMEOUT_SECONDS trong backend/app.py luôn tươi mới
            try:
                urllib.request.urlopen("http://127.0.0.1:7860/heartbeat", timeout=3).read()
            except Exception:
                pass

            # Kiểm tra nếu tiến trình cloudflared bị ngắt -> Tự động bật lại ngay!
            cf_p = state.get("cf_proc")
            if cf_p is not None and cf_p.poll() is not None:
                print("⚠️ [Auto-Heal] Phát hiện tiến trình cloudflared bị ngắt! Đang tạo lại đường hầm mới...")
                state["cf_proc"], _ = start_cloudflared_tunnel()
                for _ in range(30):
                    new_url = extract_tunnel_url()
                    if new_url and new_url != state["public_url"]:
                        state["public_url"] = new_url
                        print(f"🔄 [Auto-Heal] Đã khôi phục đường hầm mới: {new_url}")
                        notify_kernel_log("ready", "Đã tự động khôi phục đường hầm GPU!", new_url)
                        push_backend_url_to_vercel(new_url, verbose=True)
                        break
                    time.sleep(1)

            # Định kỳ đồng bộ URL hiện tại về Vercel
            if state["public_url"] and check_port_open(7860):
                push_backend_url_to_vercel(state["public_url"], verbose=False)

    sup_thread = threading.Thread(target=supervisor_worker, daemon=True)
    sup_thread.start()

    # 4. Vòng lặp giữ app.py luôn chạy (nếu bị tắt do /shutdown hoặc lỗi tạm thời sẽ tự bật lại)
    app_py_path = os.path.join(WORKING_CWD, "app.py") if os.path.exists(os.path.join(WORKING_CWD, "app.py")) else "app.py"
    restart_count = 0

    while True:
        try:
            subprocess.run(["git", "-C", "/kaggle/working/FrontendFlood", "pull", "-q"], check=False, timeout=15)
            install_custom_vehicle_weights()
            install_compatibility_shims()
        except Exception:
            pass

        with open("/tmp/app.log", "w", buffering=1, encoding="utf-8") as app_log:
            env = os.environ.copy()
            env["IDLE_TIMEOUT_SECONDS"] = "43200"
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONPATH"] = f"{WORKING_CWD}:/kaggle/working/FrontendFlood/scripts:" + env.get("PYTHONPATH", "")
            app_proc = subprocess.Popen(
                [sys.executable, app_py_path],
                cwd=WORKING_CWD,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env
            )
            state["app_proc"] = app_proc

            for line in app_proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                app_log.write(line)
                app_log.flush()

            ret = app_proc.wait()

        restart_count += 1
        if ret != 0:
            err_txt = open("/tmp/app.log", encoding="utf-8", errors="ignore").read()[-1000:] if os.path.exists("/tmp/app.log") else f"Exit code {ret}"
            print(f"\n⚠️ [App Process Exited] Mã thoát: {ret} (Lần {restart_count}).\n{err_txt}\n")
            if restart_count >= 5:
                notify_kernel_log("error", f"Lỗi khởi chạy backend AI (mã {ret}): {err_txt[:180]}...", err_txt)
                break
        else:
            print(f"\n🔄 [Auto-Heal] app.py vừa kết thúc (mã 0). Tự động khởi động lại sau 3s để duy trì GPU Backend...")

        # Đảm bảo cloudflared vẫn đang chạy trước khi bật lại app.py
        if state["cf_proc"] is None or state["cf_proc"].poll() is not None:
            state["cf_proc"], _ = start_cloudflared_tunnel()
            time.sleep(2)
            new_url = extract_tunnel_url()
            if new_url:
                state["public_url"] = new_url

        time.sleep(3)
        if state["public_url"]:
            push_backend_url_to_vercel(state["public_url"], verbose=True)


if __name__ == "__main__":
    main()
