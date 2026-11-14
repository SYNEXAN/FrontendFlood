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
import urllib.request
import urllib.error
from pathlib import Path

# Ngưỡng an toàn tối thiểu (giờ GPU) trước khi quyết định xoay sang tài khoản mới
MIN_GPU_HOURS_THRESHOLD = float(os.environ.get("MIN_GPU_HOURS_THRESHOLD", "1.0"))
FORCE_PUSH = os.environ.get("FORCE_PUSH", "false").lower() in ("true", "1", "yes")
KERNEL_SLUG = "synexan-flood-backend-gpu"


def resolve_username_from_token(token):
    """Tự động nhận diện username từ Kaggle API khi người dùng chỉ cung cấp Token (KGAT_...)."""
    try:
        url = "https://api.kaggle.com/v1/kernels.KernelsApiService/ListKernels"
        req = urllib.request.Request(
            url,
            data=json.dumps({"group": "PROFILE", "pageSize": 5}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data and isinstance(data.get("kernels"), list) and len(data["kernels"]) > 0:
                ref = data["kernels"][0].get("ref", "")
                if "/" in ref:
                    return ref.split("/")[0]
    except Exception as e:
        print(f"   [Auto-Username] Không thể tự nhận diện username từ Token: {e}")
    return None


def load_accounts():
    """Tải danh sách tài khoản từ KAGGLE_ACCOUNTS_JSON (hỗ trợ file json, token KGAT, hoặc key đơn lẻ)."""
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
            elif isinstance(parsed, str):
                accounts = [{"token": parsed}]
        except Exception:
            pass

        # 2. Thử parse bằng ast.literal_eval
        if not accounts:
            try:
                parsed = ast.literal_eval(raw_json)
                if isinstance(parsed, list):
                    accounts = parsed
                elif isinstance(parsed, dict):
                    accounts = [parsed]
            except Exception:
                pass

        # 3. Thử trích xuất Token KGAT_... đơn lẻ hoặc danh sách
        if not accounts:
            kgat_tokens = re.findall(r'KGAT_[a-zA-Z0-9_\-]+', raw_json)
            if kgat_tokens:
                accounts = [{"token": tok} for tok in kgat_tokens]

        # 4. Thử trích xuất {username: xxx, key: yyy} bằng Regex
        if not accounts:
            matches = re.findall(
                r'username["\']?\s*:\s*["\']?([^,"\';\s}]+)["\']?.*?key["\']?\s*:\s*["\']?([^,"\';\s}]+)["\']?',
                raw_json,
                re.DOTALL
            )
            if matches:
                accounts = [{"username": u.strip(), "key": k.strip()} for u, k in matches]

    if not accounts:
        user = os.environ.get("KAGGLE_USERNAME", "").strip()
        key = os.environ.get("KAGGLE_KEY", "").strip()
        if user and key:
            accounts = [{"username": user, "key": key}]

    # Xử lý tự động phát hiện Username nếu người dùng chỉ cung cấp Key / Token
    resolved = []
    for item in accounts:
        if isinstance(item, str):
            item = {"token": item} if item.startswith("KGAT_") else {"key": item}

        u = item.get("username", "").strip()
        k = item.get("key", "").strip()
        tok = item.get("token", "").strip() or (k if k.startswith("KGAT_") else "")

        if not u and tok:
            print(f"ℹ️ Phát hiện Token không có Username. Đang tự động nhận diện username từ Kaggle API...")
            discovered_user = resolve_username_from_token(tok)
            if discovered_user:
                print(f"✅ Đã tự động nhận diện Username: {discovered_user}")
                u = discovered_user

        if u and (k or tok):
            resolved.append({"username": u, "key": k or tok, "token": tok})

    return resolved


def setup_kaggle_credentials(username, key, token=None):
    """Cấu hình credentials vào ~/.kaggle/kaggle.json an toàn."""
    kaggle_dir = Path.home() / ".kaggle"
    kaggle_dir.mkdir(parents=True, exist_ok=True)
    kaggle_file = kaggle_dir / "kaggle.json"

    data = {"username": username}
    if token:
        data["token"] = token
        os.environ["KAGGLE_API_TOKEN"] = token
    if key:
        data["key"] = key
        os.environ["KAGGLE_KEY"] = key

    with open(kaggle_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    try:
        os.chmod(kaggle_file, 0o600)
    except Exception:
        pass

    os.environ["KAGGLE_USERNAME"] = username


def get_remaining_gpu_hours():
    """Truy vấn quota Kaggle hiện tại (nếu Kaggle API hỗ trợ)."""
    # Kaggle CLI hiện tại không cung cấp lệnh `kaggle quota`, trả về None an toàn
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
            return True, (proc.stdout + proc.stderr).strip()
        return False, (proc.stdout + proc.stderr).strip()
    except Exception as e:
        return False, str(e)


def cancel_kernel_if_stuck(username, token=None):
    """Cố gắng hủy kernel nếu bị kẹt hàng đợi qua Kaggle API."""
    target_id = f"{username}/{KERNEL_SLUG}"
    endpoints = [
        ("https://api.kaggle.com/v1/kernels.KernelsApiService/CancelKernel", {"kernel": target_id}),
        ("https://api.kaggle.com/v1/kernels.KernelsApiService/CancelKernel", {"kernelRef": target_id}),
        ("https://api.kaggle.com/v1/kernels.KernelsApiService/CancelKernel", {"userName": username, "kernelSlug": KERNEL_SLUG})
    ]
    if token:
        for ep, body in endpoints:
            try:
                req = urllib.request.Request(
                    ep,
                    data=json.dumps(body).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json"
                    }
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    print(f"   [Cancel Kernel] Đã gọi {ep} thành công: {resp.status}")
                    return True
            except Exception as e:
                pass
    return False


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
        setup_kaggle_credentials(username, key, acc.get("token"))

        # 2. Kiểm tra phiên đang chạy (tránh push trùng lặp trừ khi FORCE_PUSH)
        is_running, status_msg = check_kernel_running(username)
        print(f"   [Kernel Status] Trạng thái hiện tại của {username}/{KERNEL_SLUG}: {status_msg}")
        
        # In logs gần nhất của kernel để chẩn đoán
        try:
            log_proc = subprocess.run(
                ["kaggle", "kernels", "logs", f"{username}/{KERNEL_SLUG}"],
                capture_output=True,
                text=True,
                timeout=15
            )
            print(f"   [Kernel Logs] 📜 10 dòng log mới nhất:\n{log_proc.stdout.strip()[-500:] or log_proc.stderr.strip()[:300] or '(Không có log)'}")
        except Exception as e:
            print(f"   [Kernel Logs] Không lấy được log: {e}")

        if is_running and not FORCE_PUSH:
            print(f"⚡ [TIẾT KIỆM QUOTA] Kernel của '{username}' ĐANG CHẠY ({status_msg})!")
            print("👉 Giữ nguyên phiên hiện tại, không cần push lại.")
            print("=" * 70)
            sys.exit(0)
        elif is_running and FORCE_PUSH:
            # 🛡️ TRIPLE-LOCK SAFETY: Nếu đã được cấp GPU và đang chạy code -> TUYỆT ĐỐI KHÔNG NGẮT
            if "running" in status_msg.lower():
                print(f"🛡️ [TRIPLE-LOCK SAFETY] Kernel của '{username}' ĐÃ ĐƯỢC CẤP GPU VÀ ĐANG CHẠY ({status_msg})!")
                print("👉 Bảo vệ tiến trình đang chạy, tuyệt đối không ngắt, để hệ thống hoàn tất nạp mô hình.")
                print("=" * 70)
                sys.exit(0)

            # Chỉ can thiệp nếu thực sự bị kẹt cứng ở hàng đợi (QUEUED)
            if len(accounts) > 1 and "queued" in status_msg.lower() and idx == 1:
                print(f"🔄 [QUEUE ROTATE] Hàng đợi của '{username}' bị nghẽn ({status_msg}). Tự động xoay sang tài khoản dự phòng tiếp theo...")
                continue
            print(f"⚠️ [FORCE PUSH] Kernel của '{username}' bị kẹt ở hàng đợi ({status_msg}). Đang hủy hàng đợi & đẩy lại phiên mới...")
            cancel_kernel_if_stuck(username, acc.get("token"))

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
            if "maximum batch gpu session count" in lower_msg or "session count of 2 reached" in lower_msg:
                print(f"⚠️ [CONCURRENT LIMIT] Tài khoản '{username}' đã đạt giới hạn tối đa 2 GPU sessions trên Kaggle!")
                print("   👉 Kaggle đang thực thi 2 GPU session trước đó.")
                print("   👉 Đang giữ nguyên để các phiên này hoàn thành khởi động...")
                print("=" * 70)
                sys.exit(0)
            elif "quota" in lower_msg or "exceeded" in lower_msg or "limit" in lower_msg:
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
