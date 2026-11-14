#!/usr/bin/env python3
"""
scripts/rotate_kaggle.py
Tự động xoay vòng tài khoản Kaggle thông minh (Smart Low Quota Rotation)
- Đọc danh sách tài khoản từ secret KAGGLE_ACCOUNTS_JSON
- Chỉ xoay khi tài khoản hiện tại gần hết giờ (< 1.0h) hoặc bị Quota Exceeded
- Giữ nguyên phiên nếu kernel đang RUNNING hoặc QUEUED
- An toàn tuyệt đối, không lộ credentials
"""

import os
import sys
import json
import subprocess
import ast
import re
from pathlib import Path

# Ngưỡng an toàn tối thiểu (giờ GPU) trước khi quyết định xoay sang tài khoản mới
MIN_GPU_HOURS_THRESHOLD = float(os.environ.get("MIN_GPU_HOURS_THRESHOLD", "1.0"))
KERNEL_SLUG = "synexan-flood-backend-gpu"


def load_accounts():
    """Tải danh sách tài khoản từ KAGGLE_ACCOUNTS_JSON (hỗ trợ nhiều format) hoặc biến môi trường fallback."""
    raw_json = os.environ.get("KAGGLE_ACCOUNTS_JSON", "").strip()
    accounts = []

    if raw_json:
        # 1. Thử parse JSON chuẩn
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                accounts = parsed
            elif isinstance(parsed, dict):
                accounts = [parsed]
        except Exception:
            pass

        # 2. Thử parse bằng ast.literal_eval (dành cho single-quote format)
        if not accounts:
            try:
                parsed = ast.literal_eval(raw_json)
                if isinstance(parsed, list):
                    accounts = parsed
                elif isinstance(parsed, dict):
                    accounts = [parsed]
            except Exception:
                pass

        # 3. Thử trích xuất bằng Regex (dành cho unquoted format {username: xxx, key: yyy})
        if not accounts:
            matches = re.findall(
                r'username["\']?\s*:\s*["\']?([^,"\';\s}]+)["\']?.*?key["\']?\s*:\s*["\']?([^,"\';\s}]+)["\']?',
                raw_json,
                re.DOTALL
            )
            if matches:
                accounts = [{"username": u.strip(), "key": k.strip()} for u, k in matches]

        if not accounts:
            print("⚠️ Không thể trích xuất tài khoản từ KAGGLE_ACCOUNTS_JSON. Thử fallback về biến đơn lẻ...")

    if not accounts:
        user = os.environ.get("KAGGLE_USERNAME", "").strip()
        key = os.environ.get("KAGGLE_KEY", "").strip()
        if user and key:
            accounts = [{"username": user, "key": key}]

    return [acc for acc in accounts if acc.get("username") and acc.get("key")]


def setup_kaggle_credentials(username, key):
    """Cấu hình credentials vào ~/.kaggle/kaggle.json an toàn."""
    kaggle_dir = Path.home() / ".kaggle"
    kaggle_dir.mkdir(parents=True, exist_ok=True)
    kaggle_file = kaggle_dir / "kaggle.json"

    data = {"username": username, "key": key}
    with open(kaggle_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    try:
        os.chmod(kaggle_file, 0o600)
    except Exception:
        pass

    os.environ["KAGGLE_USERNAME"] = username
    os.environ["KAGGLE_KEY"] = key


def get_remaining_gpu_hours():
    """Truy vấn quota Kaggle hiện tại bằng CLI 'kaggle quota --format json'."""
    try:
        proc = subprocess.run(
            ["kaggle", "quota", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=25
        )
        if proc.returncode != 0:
            print(f"   [Quota Warning] Lỗi kiểm tra quota: {proc.stderr.strip()[:150]}")
            return None

        quota_list = json.loads(proc.stdout)
        for item in quota_list:
            if item.get("resource", "").upper() == "GPU":
                rem_str = item.get("remaining", "0h").lower().replace("h", "").strip()
                return float(rem_str)
    except Exception as e:
        print(f"   [Quota Warning] Không thể đọc quota JSON: {e}")
    return None


def check_kernel_running(username):
    """Kiểm tra xem kernel hiện tại đã đang chạy (running/queued) chưa."""
    target_id = f"{username}/{KERNEL_SLUG}"
    try:
        proc = subprocess.run(
            ["kaggle", "kernels", "status", target_id],
            capture_output=True,
            text=True,
            timeout=20
        )
        out = (proc.stdout + proc.stderr).lower()
        if "running" in out or "queued" in out:
            return True, out.strip()
        return False, out.strip()
    except Exception as e:
        return False, str(e)


def update_kernel_metadata(username):
    """Cập nhật username trong notebooks/kernel-metadata.json."""
    meta_path = Path("notebooks") / "kernel-metadata.json"
    if not meta_path.exists():
        print(f"❌ Không tìm thấy {meta_path}!")
        return False

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        meta["id"] = f"{username}/{KERNEL_SLUG}"
        meta["title"] = KERNEL_SLUG

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        return True
    except Exception as e:
        print(f"❌ Lỗi cập nhật kernel-metadata.json: {e}")
        return False


def push_kernel():
    """Thực thi đẩy notebook lên Kaggle GPU."""
    try:
        proc = subprocess.run(
            ["kaggle", "kernels", "push", "-p", "notebooks"],
            capture_output=True,
            text=True,
            timeout=45
        )
        combined_output = proc.stdout + proc.stderr
        lower_out = combined_output.lower()
        is_success = proc.returncode == 0 and ("successfully pushed" in lower_out or "configuration pushed" in lower_out)
        return is_success, combined_output.strip()
    except Exception as e:
        return False, str(e)


def main():
    print("=" * 70)
    print("🤖 SYNEXAN SMART KAGGLE ROTATION RUNNER")
    print("=" * 70)

    accounts = load_accounts()
    if not accounts:
        print("❌ LỖI: Không tìm thấy bất kỳ tài khoản Kaggle nào trong KAGGLE_ACCOUNTS_JSON!")
        print("Vui lòng cấu hình Secret KAGGLE_ACCOUNTS_JSON trong Settings -> Secrets của GitHub.")
        sys.exit(1)

    print(f"📋 Tìm thấy {len(accounts)} tài khoản trong danh sách dự phòng.")
    print(f"🎯 Ngưỡng an toàn tối thiểu: {MIN_GPU_HOURS_THRESHOLD}h GPU/tuần.")

    for idx, acc in enumerate(accounts, start=1):
        username = acc["username"]
        key = acc["key"]
        masked_key = key[:4] + "****" if len(key) > 4 else "****"
        print(f"\n🔍 [Tài khoản {idx}/{len(accounts)}] Đang kiểm tra: {username} (Key: {masked_key})...")

        # 1. Cấu hình credentials
        setup_kaggle_credentials(username, key)

        # 2. Kiểm tra phiên đang chạy (tránh push trùng lặp)
        is_running, status_msg = check_kernel_running(username)
        if is_running:
            print(f"⚡ [TIẾT KIỆM QUOTA] Kernel của '{username}' ĐANG CHẠY ({status_msg})!")
            print("👉 Giữ nguyên phiên hiện tại, không cần push lại.")
            print("=" * 70)
            sys.exit(0)

        # 3. Kiểm tra quota còn lại
        remaining_hours = get_remaining_gpu_hours()
        if remaining_hours is not None:
            print(f"📊 Thời lượng GPU còn lại của {username}: {remaining_hours:.2f}h / 30.00h")
            if remaining_hours < MIN_GPU_HOURS_THRESHOLD:
                print(f"⚠️ [LOW QUOTA] Tài khoản {username} gần hết hạn mức ({remaining_hours:.2f}h < {MIN_GPU_HOURS_THRESHOLD}h).")
                print("🔄 Tự động xoay sang tài khoản tiếp theo...")
                continue
        else:
            print("ℹ️ Không lấy được thông tin quota qua CLI, tiếp tục thử đẩy kernel...")

        # 4. Cập nhật metadata notebook
        if not update_kernel_metadata(username):
            continue

        # 5. Kích hoạt GPU Kernel
        print(f"🚀 Đang đẩy Kernel lên Kaggle GPU cho tài khoản '{username}'...")
        success, push_msg = push_kernel()

        if success:
            print(f"✅ THÀNH CÔNG! Đã khởi động GPU Kernel cho tài khoản '{username}'.")
            print(f"   Chi tiết: {push_msg}")
            print("=" * 70)
            sys.exit(0)
        else:
            lower_msg = push_msg.lower()
            if "quota" in lower_msg or "exceeded" in lower_msg or "limit" in lower_msg:
                print(f"⚠️ [QUOTA EXCEEDED] Kaggle từ chối do hết quota: {push_msg}")
                print("🔄 Tự động xoay sang tài khoản tiếp theo...")
                continue
            else:
                print(f"❌ [LỖI PUSH] {push_msg}")
                print("🔄 Thử tài khoản tiếp theo nếu có...")

    print("\n" + "=" * 70)
    print("❌ TẤT CẢ TÀI KHOẢN KAGGLE ĐỀU ĐÃ HẾT GIỜ HOẶC GẶP LỖI!")
    print("Vui lòng thêm tài khoản Kaggle mới vào GitHub Secret 'KAGGLE_ACCOUNTS_JSON'.")
    print("=" * 70)
    sys.exit(1)


if __name__ == "__main__":
    main()
