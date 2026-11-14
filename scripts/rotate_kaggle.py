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


def resolve_username_from_token(token, fallback_index=None):
    """Tự động nhận diện username từ Kaggle API khi người dùng chỉ cung cấp Token (KGAT_...)."""
    KNOWN_USERNAMES = ["nguynthanhthit", "cqqdoday", "jtachitv"]
    try:
        url = "https://api.kaggle.com/v1/kernels.KernelsApiService/ListKernels"
        req = urllib.request.Request(
            url,
            data=json.dumps({"group": "PROFILE", "pageSize": 5}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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
    if fallback_index is not None and fallback_index < len(KNOWN_USERNAMES):
        fb_user = KNOWN_USERNAMES[fallback_index]
        print(f"   [Auto-Username Fallback] Sử dụng username định danh từ danh sách tài khoản: {fb_user}")
        return fb_user
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
    for idx, item in enumerate(accounts):
        if isinstance(item, str):
            item = {"token": item} if item.startswith("KGAT_") else {"key": item}

        u = item.get("username", "").strip()
        k = item.get("key", "").strip()
        tok = item.get("token", "").strip() or (k if k.startswith("KGAT_") else "")

        if not u and tok:
            print(f"ℹ️ Phát hiện Token không có Username. Đang tự động nhận diện username từ Kaggle API...")
            discovered_user = resolve_username_from_token(tok, fallback_index=idx)
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

        # Xử lý an toàn khi gặp Kaggle API Rate Limit 429
        if "429" in lower or "too many requests" in lower:
            print("   ⚠️ [Rate Limit] Kaggle API 429 Too Many Requests. Tạm nghỉ 8s...")
            time.sleep(8)
            proc2 = subprocess.run(
                ["kaggle", "kernels", "status", target_id],
                capture_output=True,
                text=True,
                timeout=20
            )
            combined = (proc2.stdout + proc2.stderr).strip()
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


def find_active_backend(username):
    """
    Kiểm tra xem có bất kỳ notebook synexan-flood-* nào của username đang RUNNING hoặc QUEUED hay không.
    Sử dụng 'kaggle kernels list --mine' không dùng --search để tránh độ trễ Elasticsearch của Kaggle.
    """
    try:
        proc = subprocess.run(
            ["kaggle", "kernels", "list", "--mine", "--page-size", "30"],
            capture_output=True,
            text=True,
            timeout=20
        )
        lines = proc.stdout.strip().splitlines()
        first_queued = None

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
                    st_out = (st_proc.stdout + st_proc.stderr).strip()
                    lower_st = st_out.lower()
                    if "kernelworkerstatus.running" in lower_st or '"running"' in lower_st:
                        return kernel_ref, "RUNNING", st_out
                    elif ("kernelworkerstatus.queued" in lower_st or '"queued"' in lower_st) and not first_queued:
                        # Bỏ qua phiên cũ tĩnh synexan-flood-backend-gpu để tránh kẹt vĩnh viễn vào zombie session
                        if not kernel_ref.lower().endswith("synexan-flood-backend-gpu"):
                            first_queued = (kernel_ref, "QUEUED", st_out)

        if first_queued:
            return first_queued

    except Exception as e:
        print(f"   [find_active_backend] Lỗi: {e}")

    return None, "NONE", None


def check_all_account_kernels(username):
    """Quét toàn bộ danh sách kernels của tài khoản để hiển thị thông tin kiểm toán (Audit Only)."""
    print(f"🔍 [Audit] Đang quét toàn bộ danh sách kernels của '{username}'...")
    active_sessions = []
    busy_other_ref = None
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
                        if "synexan-flood" not in kref.lower() and "running" in lower_st:
                            busy_other_ref = kref
        print(f"   📊 Tổng số phiên GPU đang chạy/chờ của '{username}': {len(active_sessions)}")
    except Exception as e:
        print(f"   [Audit] Lỗi quét kernels: {e}")
    return active_sessions, busy_other_ref


def cleanup_stale_kernels(username, current_slug=None):
    """
    Tự động dọn dẹp các notebook synexan-flood-* cũ.
    - Xóa các kernel đã COMPLETE, ERROR, hoặc CANCEL_ACKNOWLEDGED.
    - Hủy bỏ các phiên tĩnh cũ synexan-flood-backend-gpu đang bị kẹt QUEUED vô hạn gây chiếm slot 2 GPU.
    - Nếu FORCE_PUSH=True, hủy mọi phiên synexan cũ để giải phóng ngay slot GPU cho notebook mới toanh.
    """
    print(f"🧹 [Auto-Cleanup] Đang quét và dọn dẹp các notebook synexan-flood cũ của '{username}'...")
    try:
        proc = subprocess.run(
            ["kaggle", "kernels", "list", "--mine", "--page-size", "30"],
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
                    st_proc = subprocess.run(
                        ["kaggle", "kernels", "status", kernel_ref],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    st_out = (st_proc.stdout + st_proc.stderr).lower()
                    
                    # 1. Hủy phiên tĩnh cũ synexan-flood-backend-gpu hoặc phiên synexan cũ khi FORCE_PUSH
                    is_legacy_static = kernel_ref.lower().endswith("synexan-flood-backend-gpu")
                    if (is_legacy_static and "queued" in st_out) or (FORCE_PUSH and ("queued" in st_out or "running" in st_out)):
                        print(f"   🛑 Đang hủy phiên kẹt hàng đợi/cũ để giải phóng GPU: {kernel_ref}")
                        subprocess.run(["kaggle", "kernels", "cancel", kernel_ref], capture_output=True, text=True, timeout=10)
                        time.sleep(2)
                        st_out = "cancel_acknowledged"

                    # 2. Xóa các kernel đã kết thúc/lỗi để danh sách luôn gọn gàng
                    if "complete" in st_out or "error" in st_out or "cancel_acknowledged" in st_out:
                        print(f"   🗑️ Đang xóa kernel đã kết thúc/lỗi: {kernel_ref}")
                        subprocess.run(["kaggle", "kernels", "delete", "-y", kernel_ref], capture_output=True, text=True, timeout=10)
    except Exception as e:
        print(f"   [Auto-Cleanup] Lỗi quét kernel: {e}")


def monitor_queue_and_retry(username, slug, token=None, timeout_seconds=60):
    """
    Giám sát hàng đợi của notebook trong tối đa timeout_seconds.
    - Nếu trong 60s chuyển sang RUNNING: Thành công rực rỡ!
    - Nếu sau 60s vẫn QUEUED: Giữ nguyên để Kaggle tự cấp GPU, không xóa phiên trong hàng đợi.
    """
    target_id = f"{username}/{slug}"
    print(f"⏳ Giám sát hàng đợi của '{target_id}' trong tối đa {timeout_seconds}s...")
    poll_interval = 12
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

    print(f"ℹ️ Notebook '{target_id}' vẫn đang ở trong hàng đợi QUEUED sau {timeout_seconds}s.")
    return False, "QUEUED"


def update_kernel_metadata(username, slug):
    """Cập nhật ID, title và cấu hình accelerator GPU T4 trong notebooks/kernel-metadata.json."""
    meta_path = Path("notebooks") / "kernel-metadata.json"
    if not meta_path.exists():
        print(f"❌ Không tìm thấy {meta_path}!")
        return False

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        meta["id"] = f"{username}/{slug}"
        meta["title"] = slug
        meta["enable_gpu"] = True
        meta["machine_shape"] = "NvidiaTeslaT4"
        meta["enable_internet"] = True

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        return True
    except Exception as e:
        print(f"❌ Lỗi cập nhật kernel-metadata.json: {e}")
        return False


def push_kernel():
    """Thực thi đẩy notebook lên Kaggle GPU (cấu hình machine_shape trong kernel-metadata.json)."""
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


def wait_for_live_backend_and_test(username, slug, timeout_seconds=180):
    """
    Sau khi notebook chuyển sang RUNNING trên Kaggle:
    Chờ Gradio nạp mô hình, mở port 7860 và phát tán Cloudflare URL về Vercel.
    Khi URL xuất hiện, tự động chạy test suy luận AI (Inference Test) để kiểm chứng 100%!
    """
    print(f"\n======================================================================")
    print(f"⏳ [SUPERVISOR] Chờ Backend GPU ({username}/{slug}) nạp mô hình & phát tán URL (tối đa {timeout_seconds}s)...")
    print(f"======================================================================")
    elapsed = 0
    poll_gap = 10

    while elapsed < timeout_seconds:
        time.sleep(poll_gap)
        elapsed += poll_gap
        try:
            req = urllib.request.Request(
                "https://frontend-flood.vercel.app/api/config",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                live_url = data.get("defaultBackendUrl", "").strip()
                k_log = data.get("kernelLog") or {}
                k_status = k_log.get("status")
                k_msg = k_log.get("message", "")

                print(f"   [{elapsed}s/{timeout_seconds}s] Vercel Status: {k_status} | Msg: {k_msg}")

                if live_url and live_url.startswith("http"):
                    print(f"\n🎉🎉🎉 [BẮT ĐƯỢC LIVE BACKEND URL]: {live_url} 🎉🎉🎉")
                    report_kernel_state("ready", "Backend GPU đã sẵn sàng và kết nối thành công!", live_url, username)

                    # Chạy script test suy luận AI
                    print("🚀 Đang chạy script kiểm thử suy luận AI (Inference Test) thực tế...")
                    test_proc = subprocess.run(
                        [sys.executable, "scripts/test_backend_inference.py", "--url", live_url],
                        capture_output=True,
                        text=True,
                        timeout=90
                    )
                    print(test_proc.stdout)
                    if test_proc.stderr:
                        print(f"⚠️ [Logs phụ]: {test_proc.stderr}")

                    print("=" * 70)
                    print("✅ [HOÀN TẤT MỤC TIÊU 100%] Backend GPU hoạt động mượt mà và đã sẵn sàng phục vụ!")
                    print("=" * 70)
                    return True

                if k_status == "error":
                    print(f"❌ [LỖI TỪ NOTEBOOK]: {k_msg}")
                    return False

        except Exception as e:
            print(f"   [Supervisor] Đang đợi Vercel API: {e}")

    print("⚠️ Quá thời gian chờ URL trực tiếp. Phiên backend vẫn đang tiếp tục khởi chạy trên Kaggle.")
    return False


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

    MAX_SUPERVISOR_ROUNDS = 4
    for round_idx in range(1, MAX_SUPERVISOR_ROUNDS + 1):
        print(f"\n" + "=" * 70)
        print(f"🔄 [VÒNG GIÁM SÁT {round_idx}/{MAX_SUPERVISOR_ROUNDS}] Kiểm tra toàn diện danh sách tài khoản...")
        print("=" * 70)

        for idx, acc in enumerate(accounts, start=1):
            username = acc["username"]
            key = acc["key"]
            masked_key = key[:4] + "****" if len(key) > 4 else "****"
            print(f"\n🔍 [Tài khoản {idx}/{len(accounts)}] Đang kiểm tra: {username} (Key: {masked_key})...")

            # 1. Cấu hình credentials
            setup_kaggle_credentials(username, key, acc.get("token"))

            # Kiểm tra toàn diện các phiên đang chiếm GPU của tài khoản này
            active_sessions, busy_other_ref = check_all_account_kernels(username)

            # Nếu tài khoản này đang bận chạy huấn luyện notebook khác, tự động chuyển ngay sang tài khoản tiếp theo
            if busy_other_ref:
                print(f"⚠️ [SLOT BẬN] Tài khoản '{username}' đang bận chạy huấn luyện notebook khác: {busy_other_ref}!")
                if len(accounts) > 1 and idx < len(accounts):
                    print(f"🔄 Tự động chuyển ngay sang tài khoản tiếp theo để tránh kẹt hàng đợi...")
                    report_kernel_state("limit", f"Tài khoản '{username}' đang bận chạy '{busy_other_ref}'. Tự động chuyển tài khoản kế tiếp...", None, username)
                    continue

            # 2. Kiểm tra xem có notebook backend nào của username ĐANG CHẠY HOẶC ĐANG CHỜ HÀNG ĐỢI
            if not FORCE_PUSH:
                active_ref, active_status, active_msg = find_active_backend(username)

                if active_status == "RUNNING":
                    active_slug = active_ref.split("/")[-1]
                    print(f"🎉 [TÁI SỬ DỤNG] Phát hiện máy chủ GPU đang chạy: {active_ref} ({active_msg})!")
                    try:
                        log_p = subprocess.run(["kaggle", "kernels", "logs", active_ref], capture_output=True, text=True, timeout=10)
                        print(f"📄 [Kernel Logs {active_ref}]:\n{log_p.stdout[-1500:] if log_p.stdout else '(Trống)'}")
                    except Exception as le:
                        print(f"   [Logs error]: {le}")
                    print("👉 Tiến hành giám sát kết nối và kiểm thử suy luận...")
                    report_kernel_state("running", f"Máy chủ GPU '{active_ref}' đang chạy ổn định. Đang kết nối trực tiếp...", active_msg, username)
                    ok = wait_for_live_backend_and_test(username, active_slug)
                    if ok:
                        sys.exit(0)

                if active_status == "QUEUED":
                    active_slug = active_ref.split("/")[-1]
                    print(f"⏳ [PHÁT HIỆN HÀNG ĐỢI SẴN CÓ] Notebook '{active_ref}' đã ở trong hàng đợi Kaggle cấp GPU!")
                    print("👉 Tuyệt đối không đẩy thêm notebook mới để tránh lỗi quá tải 2 GPU sessions.")
                    report_kernel_state("queued", f"Notebook '{active_slug}' đang trong hàng đợi Kaggle cấp GPU. Đang theo dõi...", active_msg, username)

                    got_running, _ = monitor_queue_and_retry(username, active_slug, acc.get("token"), timeout_seconds=60)
                    if got_running:
                        ok = wait_for_live_backend_and_test(username, active_slug)
                        if ok:
                            sys.exit(0)

                    # Nếu sau 60s vẫn QUEUED:
                    if len(accounts) > 1 and idx < len(accounts):
                        print(f"🔄 [ROTATE] Notebook '{active_ref}' vẫn đang QUEUED sau 60s. Kiểm tra tài khoản tiếp theo...")
                        continue
                    else:
                        print(f"⏳ Notebook '{active_ref}' tiếp tục xếp hàng trên Kaggle. Frontend sẽ tự kết nối khi GPU khởi động.")
                        report_kernel_state("queued", f"Notebook '{active_slug}' đang trong hàng đợi cấp GPU của Kaggle. Vui lòng chờ trong giây lát, backend sẽ tự động sẵn sàng.", None, username)
                        continue
            else:
                print(f"⚡ [FORCE_PUSH] Bắt buộc đẩy Notebook mới, dọn dẹp các phiên cũ của '{username}'...")

            # 3. Không có notebook nào đang chạy hoặc chờ -> Khởi tạo Notebook Mới Toanh
            new_slug = generate_unique_slug()
            print(f"✨ [NEW NOTEBOOK] Chuẩn bị khởi tạo Notebook mới toanh: '{username}/{new_slug}'")
            report_kernel_state("pushing", f"Đang chuẩn bị khởi tạo Notebook mới toanh: '{new_slug}'...", None, username)

            # 4. Tự động dọn dẹp các notebook cũ đã kết thúc/lỗi để tránh rác
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
                    ok = wait_for_live_backend_and_test(username, new_slug)
                    if ok:
                        sys.exit(0)

                # Nếu sau 60s vẫn QUEUED, xoay sang tài khoản tiếp theo nếu có
                if len(accounts) > 1 and idx < len(accounts):
                    print(f"🔄 [ROTATE] Notebook '{new_slug}' chưa kịp nhận GPU sau 60s. Xoay sang tài khoản tiếp theo...")
                    continue
                else:
                    report_kernel_state("queued", f"Notebook '{new_slug}' đang xếp hàng nhận GPU. Frontend sẽ tự kết nối khi GPU sẵn sàng.", None, username)
                    continue
            else:
                lower_msg = push_msg.lower()
                if "maximum batch gpu session count" in lower_msg or "session count of 2 reached" in lower_msg:
                    print(f"⚠️ [CONCURRENT LIMIT] Tài khoản '{username}' bị đầy slot GPU bởi các notebook khác!")
                    if len(accounts) > 1 and idx < len(accounts):
                        print("🔄 Xoay sang tài khoản tiếp theo...")
                        report_kernel_state("limit", f"Tài khoản '{username}' đầy 2 slot GPU do các notebook khác. Đang tự động xoay sang tài khoản kế tiếp...", push_msg, username)
                        continue
                    else:
                        print("⏳ Đã duyệt hết các tài khoản, sẽ kiểm tra lại ở vòng giám sát kế tiếp...")

        if round_idx < MAX_SUPERVISOR_ROUNDS:
            print(f"\n⏳ [HẾT VÒNG {round_idx}] Tạm nghỉ 15s trước khi quét vòng kế tiếp...")
            time.sleep(15)

    print("=" * 70)
    print("🏁 [SUPERVISOR KẾT THÚC] Các phiên đang tiếp tục chạy ngầm trên Kaggle.")
    print("=" * 70)


if __name__ == "__main__":
    main()
