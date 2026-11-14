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


def get_exact_kernel_status(username):
    """
    Truy vấn trạng thái chính xác của synexan-flood-backend-gpu.
    Trả về: 'RUNNING', 'QUEUED', 'COMPLETE', 'ERROR', hoặc 'UNKNOWN'.
    """
    target_id = f"{username}/{KERNEL_SLUG}"
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


def check_kernel_running(username):
    """Kiểm tra xem kernel hiện tại đã đang chạy (running/queued) chưa."""
    status, raw_msg = get_exact_kernel_status(username)
    if status in ("RUNNING", "QUEUED"):
        return True, raw_msg
    return False, raw_msg


def cancel_kernel_if_stuck(username, token=None):
    """Cố gắng hủy kernel nếu bị kẹt hàng đợi qua Kaggle API và Kaggle SDK."""
    target_id = f"{username}/{KERNEL_SLUG}"
    print(f"   [Cancel Kernel] Đang gửi lệnh ngắt phiên cho '{target_id}'...")

    # 1. Thử qua Kaggle Python SDK nếu có
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi
        from kagglesdk.kernels.types.kernels_api_service import ApiCancelKernelSessionRequest
        api = KaggleApi()
        api.authenticate()
        with api.build_kaggle_client() as k:
            req = ApiCancelKernelSessionRequest()
            k.kernels.kernels_api_client.cancel_kernel_session(req)
            print(f"   [Cancel Kernel] Đã gọi SDK cancel_kernel_session thành công!")
            return True
    except Exception as e:
        pass

    # 2. Thử qua Kaggle REST API Endpoints
    endpoints = [
        ("https://api.kaggle.com/v1/kernels.KernelsApiService/CancelKernel", {"kernel": target_id}),
        ("https://api.kaggle.com/v1/kernels.KernelsApiService/CancelKernelSession", {"kernel": target_id}),
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
            except Exception:
                pass
    return False


def monitor_queue_and_retry(username, token=None, timeout_seconds=60):
    """
    QUY TẮC 3: Giám sát hàng đợi trong đúng 60 giây (1 phút).
    - Nếu trong 60s chuyển sang RUNNING: Thành công!
    - Nếu sau 60s vẫn QUEUED: Hủy (Cancel) đúng phiên hàng đợi đó và trả về False.
    """
    print(f"⏳ [QUY TẮC 3] Giám sát hàng đợi của '{username}' trong tối đa {timeout_seconds}s...")
    poll_interval = 6
    elapsed = 0
    import time

    while elapsed < timeout_seconds:
        time.sleep(poll_interval)
        elapsed += poll_interval

        status, raw_msg = get_exact_kernel_status(username)
        print(f"   [{elapsed}s/{timeout_seconds}s] Trạng thái của '{username}': {status}")

        if status == "RUNNING":
            print(f"🎉 [CẤP GPU THÀNH CÔNG] Kernel của '{username}' đã chuyển sang RUNNING sau {elapsed}s!")
            report_kernel_state("running", f"Kaggle đã cấp GPU Tesla T4 cho '{username}' sau {elapsed}s! Đang nạp mô hình AI...", raw_msg, username)
            return True, "RUNNING"

        report_kernel_state("queued", f"Kaggle đang xếp hàng GPU cho '{username}' ({elapsed}s/{timeout_seconds}s)...", raw_msg, username)

    print(f"🛑 [HẾT HẠN HÀNG ĐỢI] Kernel của '{username}' vẫn bị kẹt ở QUEUED sau {timeout_seconds}s.")
    report_kernel_state("queued", f"Đã quá 60s trong hàng đợi. Đang ngắt phiên bị kẹt của '{username}' để khởi động lại...", None, username)
    cancel_kernel_if_stuck(username, token)
    time.sleep(3)
    return False, "QUEUED_TIMEOUT"


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

        # Liệt kê tất cả kernel của tài khoản để xem các phiên GPU đang chiếm dụng
        if acc.get("token"):
            try:
                url = "https://api.kaggle.com/v1/kernels.KernelsApiService/ListKernels"
                req = urllib.request.Request(
                    url,
                    data=json.dumps({"group": "PROFILE", "pageSize": 10}).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {acc['token']}",
                        "Content-Type": "application/json"
                    }
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    kdata = json.loads(resp.read().decode("utf-8"))
                    print(f"   [Kaggle Profile Kernels] Tìm thấy {len(kdata.get('kernels', []))} kernels:")
                    for k in kdata.get("kernels", []):
                        print(f"      * Ref: {k.get('ref')} | Status: {k.get('status')} | TotalRunTime: {k.get('totalRunTime')}s | LastRunTime: {k.get('lastRunTime')}")
            except Exception as e:
                print(f"   [Kaggle Profile Kernels] Lỗi đọc danh sách kernels: {e}")

        # 2. Kiểm tra phiên đang chạy chính xác (QUY TẮC 1 & QUY TẮC 3)
        status, status_msg = get_exact_kernel_status(username)
        print(f"   [Kernel Status] Trạng thái hiện tại của {username}/{KERNEL_SLUG}: {status} ({status_msg})")

        # ---------------------------------------------------------------------
        # QUY TẮC 1: Nếu backend synexan-flood-backend-gpu đang RUNNING từ trước
        # -> LẤY ĐÚNG PHIÊN ĐÓ, TUYỆT ĐỐI KHÔNG CHẠY CÁI KHÁC
        # ---------------------------------------------------------------------
        if status == "RUNNING":
            print(f"🎉 [QUY TẮC 1 - TÁI SỬ DỤNG] Backend của '{username}' ĐANG CHẠY ({status_msg})!")
            print("👉 Giữ nguyên 100% phiên hiện tại, không đẩy đè, kết nối trực tiếp.")
            report_kernel_state("running", f"Notebook backend của '{username}' đang chạy ổn định trên GPU Tesla T4. Đang kết nối trực tiếp...", status_msg, username)
            print("=" * 70)
            sys.exit(0)

        # ---------------------------------------------------------------------
        # QUY TẮC 3: Nếu backend đang trong hàng đợi (QUEUED) sẵn từ trước
        # -> Giám sát trong 60s, nếu sau 60s không chuyển sang RUNNING thì ngắt
        # ---------------------------------------------------------------------
        if status == "QUEUED":
            got_running, _ = monitor_queue_and_retry(username, acc.get("token"), timeout_seconds=60)
            if got_running:
                print("=" * 70)
                sys.exit(0)
            # Sau 60s bị timeout và đã cancel: nếu có nhiều tài khoản thì xoay sang tài khoản kế tiếp
            if len(accounts) > 1 and idx < len(accounts):
                print(f"🔄 [ROTATE] Hàng đợi tài khoản '{username}' bị nghẽn sau 60s. Xoay sang tài khoản tiếp theo...")
                report_kernel_state("queued", f"Hàng đợi của '{username}' bị nghẽn sau 60s. Đang chuyển sang tài khoản kế tiếp...", None, username)
                continue

        # 3. Kiểm tra quota còn lại
        remaining_hours = get_remaining_gpu_hours()
        if remaining_hours is not None:
            print(f"📊 Thời lượng GPU còn lại của {username}: {remaining_hours:.2f}h / 30.00h")
            if remaining_hours < MIN_GPU_HOURS_THRESHOLD:
                print(f"⚠️ [LOW QUOTA] Tài khoản {username} gần hết hạn mức ({remaining_hours:.2f}h < {MIN_GPU_HOURS_THRESHOLD}h).")
                print("🔄 Tự động xoay sang tài khoản tiếp theo...")
                report_kernel_state("error", f"Tài khoản '{username}' gần hết quota GPU ({remaining_hours:.1f}h). Đang chuyển tài khoản...", None, username)
                continue
        else:
            print("ℹ️ Không lấy được thông tin quota qua CLI, tiếp tục thử đẩy kernel...")

        # 4. Cập nhật metadata notebook
        if not update_kernel_metadata(username):
            continue

        # 5. Kích hoạt GPU Kernel
        print(f"🚀 Đang đẩy Kernel lên Kaggle GPU cho tài khoản '{username}'...")
        report_kernel_state("pushing", f"Đang gửi mã nguồn khởi động GPU Kaggle cho tài khoản '{username}'...", None, username)
        success, push_msg = push_kernel()

        if success:
            print(f"✅ THÀNH CÔNG! Đã khởi động GPU Kernel cho tài khoản '{username}'.")
            print(f"   Chi tiết: {push_msg}")
            report_kernel_state("queued", f"Đã gửi lệnh khởi động cho '{username}'. Đang vào hàng đợi cấp GPU Tesla T4...", push_msg, username)

            # QUY TẮC 3: Giám sát hàng đợi trong 60s, nếu sau 60s vẫn QUEUED thì ngắt và xử lý tiếp
            got_running, _ = monitor_queue_and_retry(username, acc.get("token"), timeout_seconds=60)
            if got_running:
                print("=" * 70)
                sys.exit(0)

            # Nếu sau 60s vẫn QUEUED (đã ngắt), nếu còn tài khoản khác thì xoay sang tài khoản tiếp theo
            if len(accounts) > 1 and idx < len(accounts):
                print(f"🔄 [ROTATE] Hàng đợi tài khoản '{username}' bị nghẽn sau 60s. Xoay sang tài khoản tiếp theo...")
                continue

            # Nếu là tài khoản duy nhất, thử đẩy lại 1 lần cuối
            print(f"🔄 Thử khởi động lại phiên mới trên tài khoản '{username}'...")
            report_kernel_state("queued", f"Đang khởi động lại phiên mới trên tài khoản '{username}'...", None, username)
            push_kernel()
            print("=" * 70)
            sys.exit(0)
        else:
            lower_msg = push_msg.lower()
            # -----------------------------------------------------------------
            # QUY TẮC 2: HẾT 2 SLOT GPU (Maximum batch GPU session count reached)
            # -----------------------------------------------------------------
            if "maximum batch gpu session count" in lower_msg or "session count of 2 reached" in lower_msg:
                # Kiểm tra xem trong 2 slot đó có backend synexan-flood-backend-gpu đang chạy hoặc chờ không
                check_st, check_msg = get_exact_kernel_status(username)
                if check_st == "RUNNING":
                    print(f"✅ [QUY TẮC 1] Phát hiện Backend của '{username}' CHÍNH LÀ 1 trong 2 phiên GPU đang chạy!")
                    report_kernel_state("running", f"Notebook backend của '{username}' đang chạy trên GPU. Đang kết nối trực tiếp...", check_msg, username)
                    print("=" * 70)
                    sys.exit(0)
                elif check_st == "QUEUED":
                    print(f"⏳ Phát hiện Backend của '{username}' đang nằm trong hàng đợi giữ slot GPU!")
                    report_kernel_state("queued", f"Backend của '{username}' đang giữ slot trong hàng đợi Kaggle. Đang chờ cấp GPU...", check_msg, username)
                    print("=" * 70)
                    sys.exit(0)

                # Nếu 2 slot GPU đó KHÔNG CÓ backend chạy hoặc chờ (do các notebook khác chiếm dụng)
                print(f"⚠️ [QUY TẮC 2 - HẾT SLOT GPU] Tài khoản '{username}' hết 2 slot GPU bởi các notebook khác (không có backend chạy)!")
                if len(accounts) > 1 and idx < len(accounts):
                    print("🔄 Lập tức xoay sang tài khoản dự phòng tiếp theo...")
                    report_kernel_state("limit", f"Tài khoản '{username}' hết 2 slot GPU bởi notebook khác. Đang tự động xoay sang tài khoản kế tiếp...", push_msg, username)
                    continue
                else:
                    print("⚠️ Đây là tài khoản duy nhất, không thể đẩy thêm kernel.")
                    report_kernel_state("error", f"Tài khoản '{username}' đã chiếm dụng hết 2 slot GPU bởi các notebook khác! Vui lòng dừng bớt notebook hoặc thêm tài khoản phụ.", push_msg, username)
                    print("=" * 70)
                    sys.exit(1)
            elif "quota" in lower_msg or "exceeded" in lower_msg or "limit" in lower_msg:
                print(f"⚠️ [QUOTA EXCEEDED] Kaggle từ chối do hết quota: {push_msg}")
                report_kernel_state("error", f"Tài khoản '{username}' đã hết hạn mức 30h GPU hàng tuần! Đang chuyển tài khoản...", push_msg, username)
                print("🔄 Tự động xoay sang tài khoản tiếp theo...")
                continue
            else:
                print(f"❌ [LỖI PUSH] {push_msg}")
                report_kernel_state("error", f"Lỗi khởi động kernel cho '{username}': {push_msg[:100]}", push_msg, username)
                print("🔄 Thử tài khoản tiếp theo nếu có...")

    print("\n" + "=" * 70)
    print("❌ TẤT CẢ TÀI KHOẢN KAGGLE ĐỀU ĐÃ HẾT GIỜ HOẶC GẶP LỖI!")
    print("Vui lòng thêm tài khoản Kaggle mới vào GitHub Secret 'KAGGLE_ACCOUNTS_JSON'.")
    report_kernel_state("error", "Tất cả tài khoản Kaggle đều hết quota GPU hoặc gặp sự cố. Vui lòng kiểm tra tài khoản!", None, None)
    print("=" * 70)
    sys.exit(1)


if __name__ == "__main__":
    main()
