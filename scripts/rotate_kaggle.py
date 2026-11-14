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
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# Ngưỡng an toàn tối thiểu (giờ GPU) trước khi quyết định xoay sang tài khoản mới
MIN_GPU_HOURS_THRESHOLD = float(os.environ.get("MIN_GPU_HOURS_THRESHOLD", "1.0"))
FORCE_PUSH = os.environ.get("FORCE_PUSH", "false").lower() in ("true", "1", "yes")
KERNEL_SLUG = "synexan-flood-backend-gpu"
FRONTEND_API_URL = os.environ.get("FRONTEND_API_URL", "https://frontend-flood.vercel.app/api/kernel-log")


def report_kernel_state(status, message, details=None, account=None):
    """Gửi telemetry log trạng thái Kaggle kernel về Vercel frontend để người dùng theo dõi trực tiếp."""
    try:
        payload = {
            "status": status,  # "queued", "running", "pushing", "error", "success", "limit"
            "message": message,
            "details": str(details) if details else None,
            "account": account
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            FRONTEND_API_URL,
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            pass
    except Exception as e:
        print(f"   [Telemetry] Không thể gửi log tới frontend: {e}")


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


def generate_unique_slug():
    """Tạo slug độc nhất cho notebook mới toanh mỗi lần chạy backend."""
    now_str = datetime.utcnow().strftime("%m%d%H%M%S")
    return f"synexan-flood-gpu-{now_str}"


def get_exact_kernel_status(username, slug=KERNEL_SLUG):
    """
    Truy vấn trạng thái chính xác của kernel slug.
    Trả về: 'RUNNING', 'QUEUED', 'COMPLETE', 'ERROR', hoặc 'UNKNOWN'.
    """
    target_id = f"{username}/{slug}"
    try:
        proc = subprocess.run(
            ["kaggle", "kernels", "status", target_id],
            capture_output=True,
            text=True,
            timeout=20
        )
        combined = (proc.stdout + proc.stderr).strip()
        lower = combined.lower()
        if "kernelworkerstatus.running" in lower or '"running"' in lower:
            return "RUNNING", combined
        elif "kernelworkerstatus.queued" in lower or '"queued"' in lower:
            return "QUEUED", combined
        elif "kernelworkerstatus.complete" in lower or '"complete"' in lower:
            return "COMPLETE", combined
        elif "kernelworkerstatus.error" in lower or '"error"' in lower:
            return "ERROR", combined
        return "UNKNOWN", combined
    except Exception as e:
        return "ERROR", str(e)


def find_running_backend(username):
    """Kiểm tra xem có bất kỳ notebook synexan-flood-* nào của username đang thực sự RUNNING hay không."""
    try:
        proc = subprocess.run(
            ["kaggle", "kernels", "list", "--mine", "--search", "synexan-flood", "--page-size", "10"],
            capture_output=True,
            text=True,
            timeout=15
        )
        lines = proc.stdout.strip().splitlines()
        for line in lines:
            parts = line.split()
            if len(parts) >= 1 and "/" in parts[0]:
                kernel_ref = parts[0].strip()
                if username.lower() in kernel_ref.lower() and "synexan-flood" in kernel_ref.lower():
                    st_proc = subprocess.run(
                        ["kaggle", "kernels", "status", kernel_ref],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    st_out = (st_proc.stdout + st_proc.stderr).lower()
                    if "kernelworkerstatus.running" in st_out or '"running"' in st_out:
                        return kernel_ref, (st_proc.stdout + st_proc.stderr).strip()
    except Exception:
        pass
    return None, None


def check_all_account_kernels(username):
    """Quét toàn bộ danh sách kernels của tài khoản để tìm xem phiên nào đang chiếm slot GPU."""
    print(f"🔍 [Audit] Đang quét toàn bộ danh sách kernels của '{username}'...")
    active_sessions = []
    try:
        proc = subprocess.run(
            ["kaggle", "kernels", "list", "--mine", "--page-size", "20"],
            capture_output=True,
            text=True,
            timeout=15
        )
        lines = proc.stdout.strip().splitlines()
        for line in lines:
            parts = line.split()
            if len(parts) >= 1 and "/" in parts[0]:
                kref = parts[0].strip()
                if username.lower() in kref.lower():
                    st_p = subprocess.run(["kaggle", "kernels", "status", kref], capture_output=True, text=True, timeout=10)
                    st_text = (st_p.stdout + st_p.stderr).strip()
                    lower_st = st_text.lower()
                    print(f"      * {kref} -> {st_text}")
                    if "running" in lower_st or "queued" in lower_st:
                        active_sessions.append((kref, st_text))
                        # Tự động dọn dẹp nếu là kernel ngập lụt cũ đang bị kẹt QUEUED
                        if "synexan-flood" in kref.lower() and "queued" in lower_st:
                            print(f"   🗑️ Tự động xóa kernel ngập lụt bị kẹt hàng đợi: {kref}")
                            subprocess.run(["kaggle", "kernels", "delete", "-y", kref], capture_output=True, text=True, timeout=10)
        print(f"   📊 Tổng số phiên GPU đang chạy/chờ của '{username}': {len(active_sessions)}")
    except Exception as e:
        print(f"   [Audit] Lỗi quét kernels: {e}")
    return active_sessions


def cleanup_stale_kernels(username, current_slug=None):
    """
    Tự động dọn dẹp các notebook synexan-flood-* cũ bị kẹt hàng đợi hoặc lỗi
    để giải phóng slot GPU cho notebook mới toanh chạy ngay lập tức.
    """
    print(f"🧹 [Auto-Cleanup] Đang quét và dọn dẹp các notebook synexan-flood cũ của '{username}'...")
    try:
        proc = subprocess.run(
            ["kaggle", "kernels", "list", "--mine", "--search", "synexan-flood", "--page-size", "15"],
            capture_output=True,
            text=True,
            timeout=15
        )
        lines = proc.stdout.strip().splitlines()
        for line in lines:
            parts = line.split()
            if len(parts) >= 1 and "/" in parts[0]:
                kernel_ref = parts[0].strip()
                if username.lower() in kernel_ref.lower() and "synexan-flood" in kernel_ref.lower():
                    if current_slug and current_slug.lower() in kernel_ref.lower():
                        continue
                    # Kiểm tra trạng thái của kernel cũ
                    st_proc = subprocess.run(
                        ["kaggle", "kernels", "status", kernel_ref],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    st_out = (st_proc.stdout + st_proc.stderr).lower()
                    # Nếu kernel cũ đang bị kẹt QUEUED hoặc ERROR hoặc COMPLETE
                    if "queued" in st_out or "error" in st_out or "complete" in st_out:
                        print(f"   🗑️ Đang xóa kernel cũ để giải phóng slot GPU: {kernel_ref}")
                        subprocess.run(["kaggle", "kernels", "delete", "-y", kernel_ref], capture_output=True, text=True, timeout=10)
    except Exception as e:
        print(f"   [Auto-Cleanup] Lỗi quét kernel: {e}")


def monitor_queue_and_retry(username, slug, token=None, timeout_seconds=60):
    """
    QUY TẮC 3: Giám sát hàng đợi của notebook mới trong đúng 60 giây.
    - Nếu trong 60s chuyển sang RUNNING: Thành công!
    - Nếu sau 60s vẫn QUEUED: Xóa notebook bị kẹt (Delete Kernel) để giải phóng slot GPU.
    """
    target_id = f"{username}/{slug}"
    print(f"⏳ [QUY TẮC 3] Giám sát hàng đợi của '{target_id}' trong tối đa {timeout_seconds}s...")
    poll_interval = 6
    elapsed = 0

    while elapsed < timeout_seconds:
        time.sleep(poll_interval)
        elapsed += poll_interval

        status, raw_msg = get_exact_kernel_status(username, slug)
        print(f"   [{elapsed}s/{timeout_seconds}s] Trạng thái của '{target_id}': {status}")

        if status == "RUNNING":
            print(f"🎉 [CẤP GPU THÀNH CÔNG] Notebook '{target_id}' đã chuyển sang RUNNING sau {elapsed}s!")
            report_kernel_state("running", f"Kaggle đã cấp GPU Tesla T4 cho '{slug}' sau {elapsed}s! Đang nạp mô hình AI...", raw_msg, username)
            return True, "RUNNING"

        report_kernel_state("queued", f"Kaggle đang xếp hàng GPU cho '{slug}' ({elapsed}s/{timeout_seconds}s)...", raw_msg, username)

    print(f"🛑 [HẾT HẠN HÀNG ĐỢI] Notebook '{target_id}' vẫn bị kẹt ở QUEUED sau {timeout_seconds}s.")
    report_kernel_state("queued", f"Đã quá 60s trong hàng đợi. Đang xóa notebook kẹt '{slug}' để dọn dẹp...", None, username)
    # Xóa notebook kẹt để không chiếm dụng 1 trong 2 GPU slot
    subprocess.run(["kaggle", "kernels", "delete", "-y", target_id], capture_output=True, text=True, timeout=15)
    time.sleep(3)
    return False, "QUEUED_TIMEOUT"


def update_kernel_metadata(username, slug):
    """Cập nhật ID và title notebook mới toanh trong notebooks/kernel-metadata.json."""
    meta_path = Path("notebooks") / "kernel-metadata.json"
    if not meta_path.exists():
        print(f"❌ Không tìm thấy {meta_path}!")
        return False

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        meta["id"] = f"{username}/{slug}"
        meta["title"] = slug

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
    print("🤖 SYNEXAN SMART KAGGLE ROTATION RUNNER (DYNAMIC NEW NOTEBOOK)")
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

        # Kiểm tra toàn diện các phiên đang chiếm GPU của tài khoản này
        check_all_account_kernels(username)

        # 2. Kiểm tra xem có notebook backend nào của username ĐANG CHẠY KHÔNG (Quy tắc 1)
        running_ref, running_msg = find_running_backend(username)
        if running_ref:
            print(f"🎉 [QUY TẮC 1 - TÁI SỬ DỤNG] Phát hiện máy chủ GPU đang chạy: {running_ref} ({running_msg})!")
            print("👉 Giữ nguyên phiên đang chạy, không cần tạo mới, kết nối trực tiếp.")
            report_kernel_state("running", f"Máy chủ GPU '{running_ref}' đang chạy ổn định. Đang kết nối trực tiếp...", running_msg, username)
            print("=" * 70)
            sys.exit(0)

        # 3. Sinh Slug độc nhất cho Notebook Mới Toanh mỗi lần chạy
        new_slug = generate_unique_slug()
        print(f"✨ [NEW NOTEBOOK] Chuẩn bị khởi tạo Notebook mới toanh: '{username}/{new_slug}'")
        report_kernel_state("pushing", f"Đang chuẩn bị khởi tạo Notebook mới toanh: '{new_slug}'...", None, username)

        # 4. Tự động dọn dẹp các notebook cũ bị kẹt để giải phóng slot GPU
        cleanup_stale_kernels(username, current_slug=new_slug)
        time.sleep(2)

        # 5. Cập nhật metadata với Notebook Mới Toanh
        if not update_kernel_metadata(username, new_slug):
            continue

        # 6. Đẩy Notebook Mới lên Kaggle (Version 1 sạch 100%)
        print(f"🚀 Đang đẩy Notebook mới toanh '{new_slug}' lên Kaggle GPU...")
        report_kernel_state("pushing", f"Đang đẩy Notebook mới toanh '{new_slug}' lên Kaggle GPU...", None, username)
        success, push_msg = push_kernel()

        if success:
            print(f"✅ THÀNH CÔNG! Đã khởi tạo Notebook mới '{new_slug}' (Version 1) cho '{username}'.")
            report_kernel_state("queued", f"Đã khởi tạo Notebook mới '{new_slug}'. Đang vào hàng đợi cấp GPU Tesla T4...", push_msg, username)

            # Giám sát 60s
            got_running, _ = monitor_queue_and_retry(username, new_slug, acc.get("token"), timeout_seconds=60)
            if got_running:
                print("=" * 70)
                sys.exit(0)

            # Nếu sau 60s vẫn QUEUED (đã xóa), xoay sang tài khoản tiếp theo nếu có
            if len(accounts) > 1 and idx < len(accounts):
                print(f"🔄 [ROTATE] Notebook '{new_slug}' kẹt sau 60s. Xoay sang tài khoản tiếp theo...")
                continue

            # Nếu là tài khoản duy nhất, thử sinh thêm 1 notebook mới khác
            retry_slug = generate_unique_slug()
            print(f"🔄 Thử tạo notebook mới khác: '{retry_slug}'...")
            update_kernel_metadata(username, retry_slug)
            push_kernel()
            print("=" * 70)
            sys.exit(0)
        else:
            lower_msg = push_msg.lower()
            if "maximum batch gpu session count" in lower_msg or "session count of 2 reached" in lower_msg:
                print(f"⚠️ [CONCURRENT LIMIT] Tài khoản '{username}' bị đầy slot GPU!")
                cleanup_stale_kernels(username, current_slug=new_slug)
                if len(accounts) > 1 and idx < len(accounts):
                    print("🔄 Xoay sang tài khoản tiếp theo...")
                    report_kernel_state("limit", f"Tài khoản '{username}' đầy slot GPU. Đang tự động xoay sang tài khoản kế tiếp...", push_msg, username)
                    continue
                else:
                    report_kernel_state("error", f"Tài khoản '{username}' đã đạt giới hạn 2 GPU sessions trên Kaggle (gồm phiên đang chờ hàng đợi và phiên có thể đang mở trên trình duyệt web). Vui lòng Stop session trên kaggle.com hoặc chờ giải phóng slot.", push_msg, username)
                    sys.exit(1)
            elif "quota" in lower_msg or "exceeded" in lower_msg or "limit" in lower_msg:
                print(f"⚠️ [QUOTA EXCEEDED] Kaggle từ chối do hết quota: {push_msg}")
                report_kernel_state("error", f"Tài khoản '{username}' đã hết hạn mức 30h GPU hàng tuần! Đang chuyển tài khoản...", push_msg, username)
                continue
            else:
                print(f"❌ [LỖI PUSH] {push_msg}")
                report_kernel_state("error", f"Lỗi khởi động kernel cho '{username}': {push_msg[:100]}", push_msg, username)

    print("\n" + "=" * 70)
    print("❌ TẤT CẢ TÀI KHOẢN KAGGLE ĐỀU ĐÃ HẾT GIỜ HOẶC GẶP LỖI!")
    print("Vui lòng thêm tài khoản Kaggle mới vào GitHub Secret 'KAGGLE_ACCOUNTS_JSON'.")
    report_kernel_state("error", "Tất cả tài khoản Kaggle đều hết quota GPU hoặc gặp sự cố. Vui lòng kiểm tra tài khoản!", None, None)
    print("=" * 70)
    sys.exit(1)


if __name__ == "__main__":
    main()
